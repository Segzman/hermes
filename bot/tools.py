"""
All agent tools -- Slate (cached), reminders, memory, web search, utilities.

This module is the central tool registry for the Hermes agent. It contains:
  1. Tool implementation functions that the LLM calls via function-calling
  2. The TOOLS list: OpenAI-format JSON schemas describing each tool's name,
     description, and parameters -- this is sent to the LLM so it knows what
     tools are available and how to call them
  3. The TOOL_CALLABLES dispatch map: maps tool name strings to the actual
     Python functions, used by the agent loop to execute tool calls

Architecture notes:
  - Slate data is cached for 5 minutes to avoid hammering the D2L API.
    Most tool functions call _get_data() which returns cached data when fresh.
  - Calendar events from D2L are merged with dropbox/quiz/discussion items
    via _merge_calendar() to create a unified view of all deliverables.
    This handles the case where D2L reports due dates only in the calendar
    API but not in the assignment API (common with certain D2L setups).
  - Items overdue by >14 days are always discarded as stale to avoid
    cluttering the view with ancient assignments.
  - Many tool functions use deferred imports (importing inside the function
    body) to avoid circular imports and to keep startup fast -- the Slate
    client, Apple integrations, and browser modules are only loaded when
    actually needed.
  - Tool functions that accept a "when" parameter delegate to
    reminders.parse_when() for natural-language time parsing.
  - All tool functions return plain strings (not dicts or objects) because
    the agent loop expects string responses to include in the conversation.
  - Web search falls back from Serper (Google) to DuckDuckGo if no API key
    is configured or if Serper fails.
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Path to the Slate session cookie file -- created by `python -m slate.sync`
# on the user's Mac and synced to the server. Without this file, all Slate
# tools return an auth-help message.
SLATE_SESSION = Path(os.path.expanduser("~/.hermes/slate_session.json"))
# Discard assignments overdue by more than this many days to keep the
# pending list focused on actionable items.
MAX_OVERDUE_DAYS = 14
# All user-facing times are displayed in Toronto timezone.
LOCAL_TZ = ZoneInfo("America/Toronto")


def _slate_auth_help() -> str:
    """Return a user-facing message explaining how to fix Slate auth issues."""
    return (
        "Slate session on the server is missing or expired.\n"
        "On your Mac, run `python -m slate.sync` from this repo, then try again."
    )


# ── async helper ──────────────────────────────────────────────────────────────

def _run(coro):
    """
    Run an async coroutine from synchronous code.

    This is needed because tool functions are synchronous (called from the
    agent's tool dispatch) but the Slate client is async. If an event loop
    is already running (e.g. inside the Telegram bot's async context), we
    can't just call asyncio.run() -- that would raise "cannot run nested
    event loop". Instead, we submit the coroutine to a thread pool where
    it gets its own event loop.
    """
    try:
        # If there's already a running loop (e.g. inside the Telegram handler),
        # we need a separate thread with its own loop to avoid nesting.
        loop = asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        # No running loop -- safe to use asyncio.run() directly.
        return asyncio.run(coro)


# ── cache fetch ───────────────────────────────────────────────────────────────

def _get_data(force_refresh: bool = False) -> tuple[dict, str]:
    """
    Return (data_dict, pull_time_str).

    Uses the Slate cache if fresh (<5 min), otherwise fetches from D2L.
    The pull_time_str is included in tool responses so the user knows
    how recent the data is (e.g. "2 min ago" vs "just fetched").
    """
    # Deferred import to avoid circular imports at module load time.
    from slate import cache

    if not force_refresh:
        cached = cache.load()
        if cached is not None:
            return cached, cache.get_pull_time_str()

    # Cache is stale or force_refresh requested -- fetch fresh data.
    async def _fetch():
        from slate.client import SlateClient
        async with SlateClient() as c:
            return await c.get_everything()

    data = _run(_fetch())
    cache.save(data)
    return data, "just fetched"


# ── time-range filter ─────────────────────────────────────────────────────────

def _filter_deliverables(items: list, days_ahead: Optional[int] = None, include_no_deadline: bool = False) -> list:
    """
    Filter a list of Assignment/Quiz/Discussion objects for display.

    Filtering rules applied in order:
      1. Drop items already submitted/done
      2. Drop items from irrelevant courses (e.g. dropped courses)
      3. Drop items with no deadline (unless include_no_deadline=True)
      4. Drop items overdue by more than MAX_OVERDUE_DAYS (stale)
      5. If days_ahead is set, only keep items due within that window

    The days_ahead parameter enables time-scoped queries like "what's due
    this week" (days_ahead=7) or "what's due today" (days_ahead=0).
    """
    result = []
    from slate.client import _is_relevant_course

    for item in items:
        # Skip already-submitted items -- no need to show them.
        if getattr(item, "is_submitted", False):
            continue
        # Skip items from courses the user isn't actively taking.
        course = getattr(item, "course", None)
        if course and not _is_relevant_course(getattr(course, "code", ""), getattr(course, "name", "")):
            continue
        # days_until_due() returns None for items with no deadline,
        # negative values for overdue items, and positive for future items.
        days = item.days_until_due()
        if days is None and not include_no_deadline:
            continue
        # Drop ancient overdue items -- they're likely no longer relevant.
        if days is not None and days < -MAX_OVERDUE_DAYS:
            continue
        # Apply future window filter if the caller wants a time-scoped view.
        if days_ahead is not None:
            if days is None or days > days_ahead:
                continue
        result.append(item)
    return result


def _fmt_deliverables(items: list, pull_time: str, label: str = "Pending items") -> str:
    """
    Format a list of deliverable items into a Telegram-friendly multi-line string.

    Each item gets an urgency emoji (colour-coded) and a kind icon (assignment,
    quiz, discussion, group work). Items are sorted by due date (soonest first)
    so the most urgent items appear at the top.
    """
    if not items:
        return f"✅ Nothing pending. _{pull_time}_"

    # Urgency colours: red=overdue, orange=urgent (due soon), yellow=upcoming, green=future
    EMOJI = {"overdue": "🔴", "due_today": "🚨", "urgent": "🟠",
              "upcoming": "🟡", "future": "🟢", "no_deadline": "⚪"}
    # Item type icons for visual scanning.
    KIND  = {"assignment": "📝", "group": "👥", "quiz": "📋", "discussion": "💬"}

    def sort_key(i):
        """Sort by due date, putting items with no deadline at the end."""
        d = getattr(i, "due_date", None) or getattr(i, "end_date", None)
        return d if d else datetime.max.replace(tzinfo=timezone.utc)

    lines = [f"*{label}* _{pull_time}_\n"]
    for item in sorted(items, key=sort_key):
        k = getattr(item, "kind", item.__class__.__name__.lower())
        icon = KIND.get(k, "📌")
        urg = EMOJI.get(item.urgency(), "")
        lines.append(f"{urg}{icon} *[{item.id}]* {item.name}")
        lines.append(f"   {item.course.code} — {item.due_str()}")
    return "\n".join(lines)


# ── Calendar event → Assignment merge ─────────────────────────────────────────

def _merge_calendar(data: dict) -> list:
    """
    Return a unified list of deliverables combining dropbox/quiz/discussion
    items with calendar due-date events not already covered.

    This is necessary because D2L sometimes reports due dates only through
    the calendar API, not in the assignment/quiz/discussion APIs. Without
    merging, items would appear without deadlines or be missed entirely.

    Calendar event types used:
      - EventType 2: AvailabilityEnds (item becomes unavailable)
      - EventType 3: Due (explicit due date)
      - EventType 4: varies by D2L version
    We also catch events with "Due" in the title as a fallback.
    """
    from slate.models import Assignment, Course

    existing = data["assignments"] + data["quizzes"] + data["discussions"]

    # Build lookup keys for deduplication: (course_id, normalised_name).
    # This lets us match calendar events to existing items even when the
    # names have minor formatting differences.
    def _key(course_id: str, name: str) -> tuple:
        return (course_id, name.lower().strip())

    known: set[tuple] = set()
    # Two lookup paths: by name (fuzzy) and by D2L entity ID (exact).
    by_key: dict[tuple, object] = {}
    by_id: dict[tuple, object] = {}
    for item in existing:
        item_key = _key(item.course.id, item.name)
        known.add(item_key)
        by_key[item_key] = item
        by_id[(item.course.id, str(item.id))] = item

    # Course lookup by org unit ID so we can attach courses to calendar items.
    course_map = {c.id: c for c in data.get("courses", [])}

    extras: list[Assignment] = []
    for ev in data.get("calendar_events", []):
        etype = ev.get("EventType", -1)
        title: str = ev.get("Title", "")
        assoc = ev.get("AssociatedEntity") or {}
        assoc_type = str(assoc.get("AssociatedEntityType") or "")
        # Check if the calendar event is linked to a deliverable type.
        is_deliverable_assoc = any(
            token in assoc_type for token in ("Dropbox", "Quizzing", "Discussion")
        )
        # Only care about due-date-related calendar events. Skip class
        # sessions, holidays, and other non-deliverable events.
        if etype not in (2, 3, 4) and " - Due" not in title and "Due" not in title and not is_deliverable_assoc:
            continue
        # Strip D2L-generated suffixes like " - Due" or " - Availability Ends"
        # to get the clean assignment name for matching.
        clean = title
        for suffix in (" - Due", " - Availability Ends", " - Availability Starts"):
            if clean.endswith(suffix):
                clean = clean[: -len(suffix)]
                break

        org_id = str(ev.get("AssociatedOrgUnitId") or ev.get("OrgUnitId") or "")
        course = course_map.get(org_id)
        if not course:
            continue

        # Parse the due date from the calendar event. D2L uses two possible
        # datetime formats depending on the API version.
        due_str = ev.get("EndDateTime") or ev.get("StartDateTime")
        due_date = None
        if due_str:
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
                try:
                    due_date = datetime.strptime(due_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

        # Try to find the existing item this calendar event corresponds to.
        # First try exact ID match, then fall back to name-based matching.
        assoc_id = str(assoc.get("AssociatedEntityId") or "")
        existing_item = by_id.get((org_id, assoc_id)) if assoc_id else None
        if existing_item is None:
            existing_item = by_key.get(_key(org_id, clean))

        if existing_item is not None:
            # Calendar is the source of truth for due dates when the tool API
            # returns an assignment shell without a deadline. This backfills
            # the missing due_date from the calendar event.
            if due_date and getattr(existing_item, "due_date", None) is None:
                existing_item.due_date = due_date
            if due_date and hasattr(existing_item, "end_date") and getattr(existing_item, "end_date", None) is None:
                existing_item.end_date = due_date
            continue

        if _key(org_id, clean) in known:
            continue  # already in dropbox/quiz/discussion list

        # This calendar event doesn't match any existing item -- create a
        # synthetic Assignment object for it so it appears in the unified list.
        ev_id = assoc_id or str(ev.get("CalendarEventId") or ev.get("Id", f"cal_{org_id}_{clean[:20]}"))
        extras.append(Assignment(
            id=ev_id,
            name=clean,
            course=course,
            due_date=due_date,
            instructions="",
            attachments=[],
            is_submitted=False,
            kind="assignment",
        ))
        known.add(_key(org_id, clean))

    return existing + extras


# ── Slate tools ───────────────────────────────────────────────────────────────

def slate_check_assignments(days_ahead: int = None, refresh: bool = False) -> str:
    """
    Check Slate for pending assignments, quizzes, and discussions.

    days_ahead: only show items due within this many days (e.g. 7 = this week).
                Omit to show all non-ancient pending items.
    refresh: force a fresh fetch even if cache is recent.

    Uses _merge_calendar() to include calendar-only due dates, ensuring
    nothing is missed even if the assignment API omits deadlines.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, pull_time = _get_data(force_refresh=refresh)
    except Exception as e:
        # Check for auth failures (HTTP 403) and provide specific guidance.
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error fetching Slate: {e}"

    pending = _filter_deliverables(
        _merge_calendar(data),
        days_ahead=days_ahead,
        include_no_deadline=False,
    )
    label = f"Due in next {days_ahead}d" if days_ahead is not None else "Pending deliverables"
    return _fmt_deliverables(pending, pull_time, label)


def slate_get_assignment_details(assignment_id: str) -> str:
    """
    Get full instructions and details for a specific assignment by ID.

    Searches across all merged items (assignments + quizzes + discussions +
    calendar-only items) by ID. Returns instructions, description,
    attachments, and time limits if available.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, pull_time = _get_data()
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error: {e}"

    all_items = _merge_calendar(data)
    for item in all_items:
        if item.id == assignment_id:
            lines = [
                f"*{item.name}* _{pull_time}_",
                f"Course: {item.course.name} ({item.course.code})",
                f"Status: {item.due_str()}",
                "",
            ]
            # Include instructions, description, attachments, and time
            # limits when available -- different item types have different
            # attributes.
            if hasattr(item, "instructions") and item.instructions:
                lines += ["*Instructions:*", item.instructions]
            if hasattr(item, "description") and item.description:
                lines += ["*Description:*", item.description]
            if hasattr(item, "attachments") and item.attachments:
                lines += ["", "*Attachments:*"]
                for a in item.attachments:
                    lines.append(f"• {a.name}")
            if hasattr(item, "time_limit_minutes") and item.time_limit_minutes:
                lines.append(f"Time limit: {item.time_limit_minutes} min")
            return "\n".join(lines)

    return f"ID `{assignment_id}` not found. Use `slate_check_assignments` to get current IDs."


def slate_download_docs(assignment_id: str) -> str:
    """
    Download all documents for an assignment and zip them.

    Uses the Slate client's download_assignment_docs() which fetches all
    attachments from D2L and packages them into a zip file on the server.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, _ = _get_data()
        target = next((a for a in data["assignments"] if a.id == assignment_id), None)
        if not target:
            return f"Assignment `{assignment_id}` not found."

        async def _dl():
            from slate.client import SlateClient
            async with SlateClient() as c:
                return await c.download_assignment_docs(target)

        path = _run(_dl())
        return f"Downloaded to: `{path}`"
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error: {e}"


def slate_action_plan(assignment_id: str) -> str:
    """
    Generate a step-by-step action plan for a specific assignment.

    This is a "meta-tool" -- it fetches the assignment details and then
    returns a prompt instructing the LLM to generate an action plan.
    The LLM sees the instructions as part of the tool response and
    produces the plan in its next message.
    """
    details = slate_get_assignment_details(assignment_id)
    if "not found" in details or "Error" in details:
        return details
    return (
        "Use the assignment details below to answer with:\n"
        "1. A plain-language summary\n"
        "2. Concrete deliverables\n"
        "3. A step-by-step action plan broken into subtasks with rough time estimates\n"
        "4. Risks, assumptions, and what to do first\n"
        "Do not repeat these instructions in the final answer.\n\n"
        f"---\n{details}"
    )


def slate_check_announcements(days_back: int = 7) -> str:
    """
    Check for new course announcements.

    days_back: only show announcements from the last N days (default 7).
    Only includes announcements marked as "new" (unread) by D2L to avoid
    repeating old announcements the user has already seen.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, pull_time = _get_data()
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error: {e}"

    now = datetime.now(tz=timezone.utc)
    items = []
    for a in data["announcements"]:
        if not a.is_new:
            continue
        # Ensure timezone-aware comparison even if D2L returns naive datetimes.
        if a.posted_at:
            posted = a.posted_at if a.posted_at.tzinfo else a.posted_at.replace(tzinfo=timezone.utc)
            if (now - posted).days > days_back:
                continue
        items.append(a)

    if not items:
        return f"No new announcements in the last {days_back} days. _{pull_time}_"
    lines = [f"*Announcements (last {days_back}d)* _{pull_time}_\n"]
    for a in items:
        when = a.posted_at.strftime("%b %d") if a.posted_at else ""
        lines.append(f"📢 *[{a.course.code}]* {a.title} ({when})")
        # Truncate long announcement bodies to keep the message manageable.
        if a.body:
            lines.append(f"   {a.body[:200]}")
    return "\n".join(lines)


def slate_check_grades(days_back: int = 30) -> str:
    """
    Check for recent grade updates.

    days_back: only show grades from the last N days (default 30).
    Sorted by grading date (most recent first) so the user sees new
    grades at the top.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, pull_time = _get_data()
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error: {e}"

    now = datetime.now(tz=timezone.utc)
    items = []
    for g in data["grades"]:
        if g.graded_at:
            at = g.graded_at if g.graded_at.tzinfo else g.graded_at.replace(tzinfo=timezone.utc)
            if (now - at).days > days_back:
                continue
        items.append(g)

    if not items:
        return f"No grades in the last {days_back} days. _{pull_time}_"
    lines = [f"*Recent Grades (last {days_back}d)* _{pull_time}_\n"]
    # Sort by grading date, most recent first. Items without a grading
    # date go to the end.
    for g in sorted(items, key=lambda x: x.graded_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        lines.append(f"🎓 {g.summary()}")
    return "\n".join(lines)


def slate_check_messages() -> str:
    """
    Check for unread Slate messages.

    Only shows messages marked as unread by D2L. Message bodies are
    truncated to 200 characters to keep the response compact.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, pull_time = _get_data()
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error: {e}"

    unread = [m for m in data["messages"] if not m.is_read]
    if not unread:
        return f"No unread messages. _{pull_time}_"
    lines = [f"*Unread Messages* _{pull_time}_\n"]
    for m in unread:
        lines.append(f"✉️ *[{m.id}]* {m.subject}")
        lines.append(f"   From: {m.sender_name}")
        if m.body:
            lines.append(f"   {m.body[:200]}")
    return "\n".join(lines)


def slate_refresh() -> str:
    """
    Force a fresh fetch from Slate, bypassing the cache.

    Invalidates the cache first, then fetches new data. Useful when the
    user knows something changed and wants the latest data immediately.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    from slate import cache
    cache.invalidate()
    try:
        _, pull_time = _get_data(force_refresh=True)
        return f"✅ Slate data refreshed. _{pull_time}_"
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error refreshing: {e}"


# ── Reminder tools ────────────────────────────────────────────────────────────
# These are thin wrappers around bot.reminders functions. They exist as
# separate functions so they can be referenced in TOOL_CALLABLES and
# used directly by the skills module.

def set_reminder(when: str, message: str) -> str:
    """Schedule a Telegram reminder at the specified time."""
    from bot.reminders import set_reminder as _set
    return _set(when, message)

def set_apple_reminder(
    title: str,
    when: str = None,
    notes: str = "",
    priority: str = "",
    urgent: bool = False,
    location: str = "",
    people: list[str] = None,
    subtasks: list[str] = None,
    list_name: str = "",
    alert_minutes_before: int = 60,
) -> str:
    """
    Create a rich Apple Reminder in iCloud via CalDAV.

    Supports all the extended features of Apple Reminders: priority levels,
    location text, people tags, subtasks, choosing a specific list, and
    configurable alert timing. The 'when' parameter uses the same natural-
    language time parsing as regular reminders.
    """
    from bot.apple import create_rich_reminder
    from bot.reminders import parse_when

    due_at = None
    if when:
        due_at = parse_when(when)
        if not due_at:
            return (
                f"Couldn't understand reminder time '{when}'.\n"
                "Try: 'tomorrow at 9am', 'in 2 hours', '2026-03-25 14:00'"
            )
    try:
        result = create_rich_reminder(
            title=title,
            due=due_at,
            notes=notes,
            priority=priority,
            urgent=urgent,
            location=location,
            people=people or [],
            subtasks=subtasks or [],
            list_name=list_name,
            alert_minutes_before=alert_minutes_before,
        )
    except Exception as e:
        return f"Apple Reminders error: {e}"
    # Build a details line summarising what was created, so the user
    # can confirm the reminder has all the right attributes.
    calendar_name = result["calendar_name"]
    extras = []
    uid = result.get("uid", "")
    if urgent or priority:
        extras.append(f"priority={('high' if urgent else priority or 'default')}")
    if location:
        extras.append(f"place={location}")
    if people:
        extras.append(f"people={', '.join(people)}")
    if result.get("subtasks_created"):
        extras.append(f"subtasks={result['subtasks_created']}")
    if uid:
        # Show only the first 12 chars of the UID for brevity.
        extras.append(f"id={uid[:12]}")
    extra_line = f"\nDetails: {', '.join(extras)}" if extras else ""
    if due_at:
        due_label = due_at.astimezone(LOCAL_TZ).strftime("%A, %b %d at %I:%M %p")
        return f'✅ Added to Apple Reminders ({calendar_name}) for {due_label} Toronto:\n"{title}"{extra_line}'
    return f'✅ Added to Apple Reminders ({calendar_name}):\n"{title}"{extra_line}'


def list_apple_reminders(limit: int = 10, include_completed: bool = False, list_name: str = "") -> str:
    """
    List Apple Reminders from iCloud.

    Displays each reminder with its due date (in Toronto time), completion
    status, location, and truncated UID for reference.
    """
    from bot.apple import list_apple_reminders as _list_apple

    try:
        items = _list_apple(limit=limit, include_completed=include_completed, list_name=list_name)
    except Exception as e:
        return f"Apple Reminders error: {e}"
    if not items:
        return "No Apple Reminders found."

    lines = ["*Apple Reminders*\n"]
    for item in items:
        status = "✓ " if item.completed else ""
        if item.due:
            when = item.due.astimezone(LOCAL_TZ).strftime("%a %b %d %I:%M %p Toronto")
        else:
            when = "No due date"
        extra = []
        if item.location:
            extra.append(item.location)
        if item.uid:
            extra.append(f"id={item.uid[:12]}")
        suffix = f" — {', '.join(extra)}" if extra else ""
        lines.append(f"• {status}{when} — {item.title}{suffix}")
    return "\n".join(lines)


def update_apple_reminder(
    ref: str,
    title: str = None,
    when: str = None,
    clear_when: bool = False,
    notes: str = None,
    clear_notes: bool = False,
    priority: str = None,
    urgent: bool = None,
    location: str = None,
    clear_location: bool = False,
    people: list[str] = None,
    clear_people: bool = False,
    list_name: str = "",
    alert_minutes_before: int = None,
    completed: bool = None,
) -> str:
    """
    Update an existing Apple Reminder by UID, exact title, or partial title.

    The 'ref' parameter is resolved by the Apple module -- it tries UID
    match first, then exact title, then partial title. The clear_* flags
    allow removing optional fields without replacing them.
    """
    from bot.apple import update_apple_reminder as _update_apple
    from bot.reminders import parse_when

    due_at = None
    if when:
        due_at = parse_when(when)
        if not due_at:
            return (
                f"Couldn't understand reminder time '{when}'.\n"
                "Try: 'tomorrow at 9am', 'in 2 hours', '2026-03-25 14:00'"
            )
    try:
        result = _update_apple(
            ref=ref,
            title=title,
            due=due_at,
            clear_due=clear_when,
            notes=notes,
            clear_notes=clear_notes,
            priority=priority,
            urgent=urgent,
            location=location,
            clear_location=clear_location,
            people=people,
            clear_people=clear_people,
            list_name=list_name,
            alert_minutes_before=alert_minutes_before,
            completed=completed,
        )
    except Exception as e:
        return f"Apple Reminders error: {e}"
    extras = []
    if result.get("uid"):
        extras.append(f"id={result['uid'][:12]}")
    if result.get("completed"):
        extras.append("completed")
    if result.get("renamed_subtasks"):
        extras.append(f"renamed_subtasks={result['renamed_subtasks']}")
    extra_line = f"\nDetails: {', '.join(extras)}" if extras else ""
    return f'✅ Updated Apple Reminder ({result["calendar_name"]}):\n"{result["title"]}"{extra_line}'


def delete_apple_reminder(ref: str, list_name: str = "", delete_subtasks: bool = True) -> str:
    """
    Delete an Apple Reminder by UID, exact title, or partial title.

    By default, also deletes any subtasks (child reminders) associated
    with the parent reminder.
    """
    from bot.apple import delete_apple_reminder as _delete_apple

    try:
        result = _delete_apple(ref=ref, list_name=list_name, delete_subtasks=delete_subtasks)
    except Exception as e:
        return f"Apple Reminders error: {e}"
    extras = []
    if result.get("uid"):
        extras.append(f"id={result['uid'][:12]}")
    if result.get("deleted_subtasks"):
        extras.append(f"deleted_subtasks={result['deleted_subtasks']}")
    extra_line = f"\nDetails: {', '.join(extras)}" if extras else ""
    return f'✅ Deleted Apple Reminder ({result["calendar_name"]}):\n"{result["title"]}"{extra_line}'

def list_reminders() -> str:
    """List all pending Telegram reminders (APScheduler jobs)."""
    from bot.reminders import list_reminders as _list
    return _list()

def cancel_reminder(ref: str) -> str:
    """Cancel a Telegram reminder by number or partial text match."""
    from bot.reminders import cancel_reminder as _cancel
    return _cancel(ref)


def add_apple_calendar_event(
    title: str,
    start: str,
    end: str = None,
    notes: str = "",
    location: str = "",
    alert_minutes_before: int = 30,
) -> str:
    """
    Create an Apple Calendar event in iCloud via CalDAV.

    The start and optional end times use natural-language parsing.
    If no end time is specified, the Apple module defaults to a 1-hour
    event. An alert is set to fire before the event starts.
    """
    from bot.apple import create_calendar_event
    from bot.reminders import parse_when

    start_at = parse_when(start)
    if not start_at:
        return (
            f"Couldn't understand event start '{start}'.\n"
            "Try: 'tomorrow at 9am', 'in 2 hours', '2026-03-25 14:00'"
        )
    end_at = None
    if end:
        end_at = parse_when(end)
        if not end_at:
            return (
                f"Couldn't understand event end '{end}'.\n"
                "Try: 'tomorrow at 11am', 'in 3 hours', '2026-03-25 16:00'"
            )
    try:
        result = create_calendar_event(
            title=title,
            start_at=start_at,
            end_at=end_at,
            notes=notes,
            location=location,
            alert_minutes_before=alert_minutes_before,
        )
    except Exception as e:
        return f"Apple Calendar error: {e}"
    start_label = start_at.astimezone(LOCAL_TZ).strftime("%A, %b %d at %I:%M %p")
    uid = result.get("uid", "")
    extra = f"\nDetails: id={uid[:12]}" if uid else ""
    return f'✅ Added to Apple Calendar ({result["calendar_name"]}) for {start_label} Toronto:\n"{title}"{extra}'


def list_apple_calendar_events(days: int = 7, limit: int = 10) -> str:
    """
    List upcoming Apple Calendar events within the given day window.

    Each event shows its start time in Toronto timezone, location, and
    truncated UID for reference in update/delete operations.
    """
    from bot.apple import list_upcoming_calendar_events

    try:
        items = list_upcoming_calendar_events(days=days, limit=limit)
    except Exception as e:
        return f"Apple Calendar error: {e}"
    if not items:
        return f"No Apple Calendar events in the next {days} days."

    lines = [f"*Apple Calendar* next {days}d\n"]
    for item in items:
        start_local = item.start_at.astimezone(LOCAL_TZ)
        when = start_local.strftime("%a %b %d %I:%M %p Toronto")
        extra = []
        if item.location:
            extra.append(item.location)
        if item.uid:
            extra.append(f"id={item.uid[:12]}")
        suffix = f" — {', '.join(extra)}" if extra else ""
        lines.append(f"• {when} — {item.title}{suffix}")
    return "\n".join(lines)


def update_apple_calendar_event(
    ref: str,
    title: str = None,
    start: str = None,
    end: str = None,
    clear_end: bool = False,
    notes: str = None,
    clear_notes: bool = False,
    location: str = None,
    clear_location: bool = False,
    alert_minutes_before: int = None,
) -> str:
    """
    Update an Apple Calendar event by UID, exact title, or partial title.

    Similar to update_apple_reminder, the clear_* flags allow removing
    optional fields. Time parameters use the same natural-language parsing.
    """
    from bot.apple import update_apple_calendar_event as _update_event
    from bot.reminders import parse_when

    start_at = None
    if start:
        start_at = parse_when(start)
        if not start_at:
            return (
                f"Couldn't understand event start '{start}'.\n"
                "Try: 'tomorrow at 9am', 'in 2 hours', '2026-03-25 14:00'"
            )
    end_at = None
    if end:
        end_at = parse_when(end)
        if not end_at:
            return (
                f"Couldn't understand event end '{end}'.\n"
                "Try: 'tomorrow at 11am', 'in 3 hours', '2026-03-25 16:00'"
            )
    try:
        result = _update_event(
            ref=ref,
            title=title,
            start_at=start_at,
            end_at=end_at,
            clear_end=clear_end,
            notes=notes,
            clear_notes=clear_notes,
            location=location,
            clear_location=clear_location,
            alert_minutes_before=alert_minutes_before,
        )
    except Exception as e:
        return f"Apple Calendar error: {e}"
    start_label = result["start_at"].astimezone(LOCAL_TZ).strftime("%A, %b %d at %I:%M %p")
    extras = []
    if result.get("uid"):
        extras.append(f"id={result['uid'][:12]}")
    extra_line = f"\nDetails: {', '.join(extras)}" if extras else ""
    return f'✅ Updated Apple Calendar ({result["calendar_name"]}) for {start_label} Toronto:\n"{result["title"]}"{extra_line}'


def delete_apple_calendar_event(ref: str) -> str:
    """Delete an Apple Calendar event by UID, exact title, or partial title."""
    from bot.apple import delete_apple_calendar_event as _delete_event

    try:
        result = _delete_event(ref=ref)
    except Exception as e:
        return f"Apple Calendar error: {e}"
    extra = f'\nDetails: id={result["uid"][:12]}' if result.get("uid") else ""
    return f'✅ Deleted Apple Calendar event ({result["calendar_name"]}):\n"{result["title"]}"{extra}'


# ── Computer use / browser tools ─────────────────────────────────────────────
# Thin wrappers around bot.computer functions. Each function delegates to
# the computer module which manages the Playwright browser session with
# automatic backend selection (local -> Browser Use -> Browserbase).

def browser_open(url: str) -> str:
    """Open a web page in the persistent browser session."""
    from bot import computer
    return computer.open_url(url)


def browser_current_page() -> str:
    """Show the current browser page title and URL."""
    from bot import computer
    return computer.current_page()


def browser_interactives(max_items: int = 25) -> str:
    """List visible interactive elements (inputs, buttons, links) with CSS selectors."""
    from bot import computer
    return computer.list_interactives(max_items=max_items)


def browser_click(selector: str) -> str:
    """Click the first matching element using a CSS selector."""
    from bot import computer
    return computer.click(selector)


def browser_type(selector: str, text: str, press_enter: bool = False) -> str:
    """
    Type text into an input element. Supports 'env:VAR_NAME' syntax
    to securely inject environment variable values without the LLM
    ever seeing the actual secret.
    """
    from bot import computer
    return computer.type_text(selector, text, press_enter=press_enter)


def browser_create_context(save_to_env: bool = True) -> str:
    """
    Create a reusable Browserbase context (persistent browser profile).

    When save_to_env=True, the context ID is written to .env so it
    persists across restarts and the browser retains cookies/state.
    """
    from bot import computer

    try:
        context_id = computer.create_browserbase_context(save_to_env=save_to_env)
    except Exception as e:
        return f"Browser context error: {e}"
    if save_to_env:
        return f"Browserbase context created and saved to `.env`: `{context_id}`"
    return f"Browserbase context created: `{context_id}`"


def browser_read(selector: str = "", max_items: int = 20) -> str:
    """
    Read text from the current page. Without a selector, returns a
    summary of the full page text. With a selector, extracts matching
    element text.
    """
    from bot import computer
    return computer.read_page(selector=selector, max_items=max_items)


def browser_screenshot(full_page: bool = True) -> str:
    """Save a screenshot of the current browser page to disk."""
    from bot import computer
    path = computer.take_screenshot(full_page=full_page)
    return f"Screenshot saved to: `{path}`"


def browser_upload_file(selector: str, file_path: str) -> str:
    """Upload a local file into a file input element on the page."""
    from bot import computer

    try:
        return computer.upload_file(selector=selector, file_path=file_path)
    except Exception as e:
        return f"Browser upload error: {e}"


def browser_download(selector: str = "", url: str = "") -> str:
    """
    Download a file through the browser. Either click a selector that
    triggers a download, or navigate to a direct download URL.
    """
    from bot import computer

    try:
        path = computer.download(selector=selector, url=url)
    except Exception as e:
        return f"Browser download error: {e}"
    return f"Download saved to: `{path}`"


def browser_login_status() -> str:
    """Report the current browser page URL, title, cookie count, and login-state guess."""
    from bot import computer

    try:
        return computer.login_status()
    except Exception as e:
        return f"Browser status error: {e}"


def browser_reset() -> str:
    """Force-close the browser session and kill any leftover browser processes."""
    from bot import computer

    computer.reset_browser(force_kill=True)
    return "Browser session closed and leftover browser processes were cleaned up."


# ── Terminal tools ────────────────────────────────────────────────────────────

def terminal_run(command: str, cwd: str = "", timeout_seconds: int = 20) -> str:
    """
    Run a bounded, non-interactive shell command on the host.

    Delegates to terminal.run_command() which runs the command in a new
    process group for clean killing on timeout. Returns the command output
    formatted with exit status and working directory for context.
    """
    from bot import terminal

    try:
        result = terminal.run_command(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:
        return f"Terminal error: {e}"

    status = f"timed out after {timeout_seconds}s" if result["timed_out"] else f'exit {result["exit_code"]}'
    lines = [
        f"$ {result['command']}",
        f"Cwd: {result['cwd']}",
        f"Status: {status}",
    ]
    if result["output"]:
        lines.append("")
        lines.append(result["output"])
    return "\n".join(lines)


# ── Background job tools ─────────────────────────────────────────────────────
# These tools let the user inspect and manage background sub-agent jobs.
# The chat_id parameter is injected by the agent loop (not provided by the LLM)
# to scope jobs to the current conversation.

def list_background_jobs(chat_id: str, include_done: bool = False, limit: int = 10) -> str:
    """List running or recent background sub-agent jobs for the current chat."""
    from bot import jobs

    return jobs.list_jobs_text(chat_id=chat_id, include_done=include_done, limit=limit)


def background_job_status(chat_id: str, ref: str = "") -> str:
    """Show status for the latest background job, or match one by ID or prompt text."""
    from bot import jobs

    return jobs.job_status_text(chat_id=chat_id, ref=ref)


def cancel_background_job(chat_id: str, ref: str = "") -> str:
    """Cancel a running background sub-agent job by ID or prompt text."""
    from bot import jobs

    return jobs.cancel_job(chat_id=chat_id, ref=ref)


# ── Service management tools ─────────────────────────────────────────────────
# Wrappers around terminal.service_* functions for managing systemd services
# on the EC2 host. These use well-known systemctl/journalctl commands with
# fixed timeouts.

def service_status(service: str) -> str:
    """Check systemd status for a service on the host."""
    from bot import terminal

    try:
        result = terminal.service_status(service)
    except Exception as e:
        return f"Service status error: {e}"
    lines = [f"Service: {service}", f"Status: exit {result['exit_code']}"]
    if result["output"]:
        lines.extend(["", result["output"]])
    return "\n".join(lines)


def service_restart(service: str) -> str:
    """Restart a systemd service and report whether it came back up."""
    from bot import terminal

    try:
        result = terminal.service_restart(service)
    except Exception as e:
        return f"Service restart error: {e}"
    status = f"timed out after 30s" if result["timed_out"] else result["output"] or f"exit {result['exit_code']}"
    return f"Restarted service `{service}`.\n{status}"


def service_logs(service: str, lines: int = 100) -> str:
    """Fetch recent journal logs for a systemd service."""
    from bot import terminal

    try:
        result = terminal.tail_logs(service, lines=lines)
    except Exception as e:
        return f"Service logs error: {e}"
    # Clamp the line count for the header display to match the actual limit.
    header = f"Logs for `{service}` (last {max(1, min(int(lines), 400))} lines)"
    if result["output"]:
        return f"{header}\n\n{result['output']}"
    return f"{header}\n\nNo log output."


# ── Task tools ────────────────────────────────────────────────────────────────
# Wrappers around bot.tasks functions. Tasks are persistent to-do items
# stored in SQLite, as opposed to reminders which fire at a specific time
# and then disappear.

def add_task(title: str, due: str = None, priority: str = "medium", notes: str = "") -> str:
    """Create a task with optional due time, priority, and notes."""
    from bot import tasks

    due_at = None
    if due:
        # Reuse the same natural-language time parser used by reminders.
        from bot.reminders import parse_when
        due_at = parse_when(due)
        if not due_at:
            return (
                f"Couldn't understand due time '{due}'.\n"
                "Try: 'tomorrow at 9am', 'in 2 hours', '2026-03-25 14:00'"
            )
    task = tasks.add_task(title=title, due_at=due_at, priority=priority, notes=notes)
    return f"✅ Task added.\n{task.summary()}"


def list_tasks(status: str = "open", limit: int = 20) -> str:
    """List tasks filtered by status (open or done)."""
    from bot import tasks
    return tasks.format_task_list(status=status, limit=limit)


def complete_task(ref: str) -> str:
    """Mark a task done by numeric ID or partial title match."""
    from bot import tasks
    task = tasks.set_task_status(ref, "done")
    if not task:
        return f'No task found matching "{ref}".'
    return f"✅ Completed task.\n{task.summary()}"


def reopen_task(ref: str) -> str:
    """Reopen a completed task by numeric ID or partial title match."""
    from bot import tasks
    task = tasks.set_task_status(ref, "open")
    if not task:
        return f'No task found matching "{ref}".'
    return f"🔄 Reopened task.\n{task.summary()}"


def delete_task(ref: str) -> str:
    """Permanently delete a task by numeric ID or partial title match."""
    from bot import tasks
    task = tasks.delete_task(ref)
    if not task:
        return f'No task found matching "{ref}".'
    return f'🗑 Deleted task "{task.title}".'


def task_from_slate(assignment_id: str, priority: str = "high") -> str:
    """
    Create a task from a Slate assignment, quiz, or discussion ID.

    This bridges the Slate and task systems: it looks up the Slate item,
    extracts its name, due date, and course code, then creates a local
    task with source="slate" and source_id set to the Slate ID. This
    lets the user track Slate deliverables alongside manual tasks.
    """
    if not SLATE_SESSION.exists():
        return _slate_auth_help()
    try:
        data, _ = _get_data()
    except Exception as e:
        if "403" in str(e) or "Forbidden" in str(e):
            return _slate_auth_help()
        return f"Error: {e}"

    # Search across all merged items (including calendar-only ones).
    item = next((x for x in _merge_calendar(data) if str(x.id) == str(assignment_id)), None)
    if not item:
        return f"ID `{assignment_id}` not found. Use `slate_check_assignments` to get current IDs."

    from bot import tasks
    task = tasks.add_task(
        title=item.name,
        # Use due_date or end_date (quizzes use end_date for their deadline).
        due_at=getattr(item, "due_date", None) or getattr(item, "end_date", None),
        priority=priority,
        notes=f"{item.course.code} — imported from Slate",
        source="slate",
        source_id=str(item.id),
    )
    return f"✅ Task created from Slate.\n{task.summary()}"


# ── Memory tools ──────────────────────────────────────────────────────────────
# Wrappers around bot.memory functions. The memory system stores persistent
# facts as markdown files with YAML frontmatter in ~/.hermes/memory/.

def remember(
    name: str,
    content: str,
    memory_type: str = "note",
    description: str = "",
    tags: list[str] = None,
) -> str:
    """Save something to persistent memory for future recall."""
    from bot.memory import save
    return save(name, content, memory_type, description=description, tags=tags or [])

def recall(query: str, memory_type: str = "", limit: int = 5) -> str:
    """Search stored memories by keyword using scored matching."""
    from bot.memory import recall as _recall
    return _recall(query, memory_type=memory_type, limit=limit)

def list_memories(memory_type: str = "", limit: int = 30) -> str:
    """List all stored memories, optionally filtered by type."""
    from bot.memory import list_all
    return list_all(memory_type=memory_type, limit=limit)

def forget(name: str) -> str:
    """Delete a memory by name."""
    from bot.memory import delete
    return delete(name)


# ── Web search ────────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web and return formatted results.

    Uses the Serper (Google) API if SERPER_API_KEY is set, otherwise
    falls back to DuckDuckGo. Returns titles, snippets, and URLs.
    """
    items = _search_items(query, max_results)
    return _format_search_results(query, items)


def hybrid_web_lookup(
    query: str,
    page_url: str = "",
    preferred_domain: str = "",
    browser_selector: str = "",
    max_results: int = 5,
) -> str:
    """
    Try both web search and a direct browser check, then return both.

    This "hybrid" approach is designed for current, site-specific lookups
    like grocery prices, menus, hours, flyers, or product availability
    where search snippets alone may be stale or incomplete. The browser
    opens the actual page for fresh data.

    If no page_url is provided, the best search result matching the
    preferred_domain is opened instead.
    """
    items = _search_items(query, max_results)
    lines = ["Search results:", _format_search_results(query, items)]

    # Determine which URL to open in the browser.
    target_url = page_url.strip()
    chosen = None
    if not target_url:
        # No explicit URL -- pick the best search result, preferring
        # results from the specified domain (e.g. foodbasics.ca).
        chosen = _pick_best_search_result(items, preferred_domain=preferred_domain)
        if chosen:
            target_url = chosen.get("link", "").strip()

    if not target_url:
        lines.extend(["", "Direct browser check: no suitable URL found from search results."])
        return "\n".join(lines)

    if chosen is None:
        chosen = {"title": "", "snippet": "", "link": target_url}

    lines.extend(
        [
            "",
            "Direct browser check target:",
            f"{chosen.get('title') or '(untitled)'}",
            f"_{target_url}_",
        ]
    )

    # Open the page in the browser and read its content.
    from bot import computer

    try:
        page_summary = computer.open_url(target_url)
        if browser_selector:
            # Read specific elements if a selector was provided.
            browser_text = computer.read_page(selector=browser_selector, max_items=5, max_chars=2000)
        else:
            browser_text = computer.read_page(max_chars=2000)
        lines.extend(["", "Direct browser check:", page_summary, "", browser_text])
    except Exception as e:
        lines.extend(["", f"Direct browser check failed: {e}"])

    return "\n".join(lines)


def _search_items(query: str, max_results: int = 5) -> list[dict]:
    """
    Search the web using the best available provider.

    Falls back from Serper (Google) to DuckDuckGo if no API key is set
    or if the Serper request fails. Returns a list of dicts with
    title, snippet, and link keys.
    """
    serper_key = os.getenv("SERPER_API_KEY", "")
    if serper_key:
        items = _search_serper_items(query, max_results, serper_key)
        if items:
            return items
    # Fallback to DuckDuckGo (no API key required).
    return _search_ddgs_items(query, max_results)


def _search_serper_items(query: str, max_results: int, api_key: str) -> list[dict]:
    """
    Search via Serper.dev (Google search API).

    Returns organic results with title, snippet (capped at 300 chars),
    and link. Returns an empty list on any error so the caller can
    fall back to DuckDuckGo.
    """
    import httpx
    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic", [])
        return [
            {
                "title": r.get("title", ""),
                "snippet": (r.get("snippet", "") or "")[:300],
                "link": r.get("link", ""),
            }
            for r in results[:max_results]
            if r.get("link")
        ]
    except Exception:
        return []


def _search_ddgs_items(query: str, max_results: int) -> list[dict]:
    """
    Search via DuckDuckGo (no API key required).

    Tries the newer 'ddgs' package first, then falls back to the older
    'duckduckgo_search' package name. This handles both package naming
    conventions across different versions.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [
            {
                "title": r.get("title", ""),
                # DuckDuckGo uses "body" instead of "snippet" for the excerpt.
                "snippet": (r.get("body", "") or "")[:300],
                # DuckDuckGo uses "href" instead of "link" for the URL.
                "link": r.get("href", ""),
            }
            for r in results[:max_results]
            if r.get("href")
        ]
    except Exception:
        return []


def _format_search_results(query: str, items: list[dict]) -> str:
    """Format a list of search result dicts into a Telegram-friendly string."""
    if not items:
        return f"No results for: {query}"
    lines = []
    for item in items:
        lines.append(f"*{item.get('title', '')}*")
        lines.append((item.get("snippet", "") or "")[:300])
        lines.append(f"_{item.get('link', '')}_\n")
    return "\n".join(lines)


def _normalize_host(value: str) -> str:
    """
    Normalize a hostname or URL to a bare domain for comparison.

    Strips protocol, "www." prefix, and extracts the netloc if given
    a full URL. This enables matching "www.foodbasics.ca" to "foodbasics.ca".
    """
    host = (value or "").strip().lower()
    if "://" in host:
        host = urlparse(host).netloc.lower()
    return host.removeprefix("www.")


def _pick_best_search_result(items: list[dict], preferred_domain: str = "") -> Optional[dict]:
    """
    Pick the best search result, preferring results from the specified domain.

    If a preferred_domain is given (e.g. "foodbasics.ca"), returns the first
    result matching that domain. Otherwise returns the top result.
    The preferred_domain can be comma- or space-separated for multiple domains.
    """
    if not items:
        return None

    # Parse preferred domains from the comma/space-separated string.
    preferred = [
        _normalize_host(token)
        for token in preferred_domain.replace(",", " ").split()
        if _normalize_host(token)
    ]
    if preferred:
        for item in items:
            host = _normalize_host(item.get("link", ""))
            # Match exact domain or subdomain (e.g. "m.foodbasics.ca" matches "foodbasics.ca").
            if any(host == domain or host.endswith(f".{domain}") for domain in preferred):
                return item
    # No preferred domain match -- return the top result.
    return items[0]


# ── Utilities ─────────────────────────────────────────────────────────────────

def get_current_time() -> str:
    """Return the current date and time in Toronto timezone."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Toronto"))
    return f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M %p ET (Toronto)')}"


# ── Tool registry ─────────────────────────────────────────────────────────────
# This list defines every tool available to the LLM agent in OpenAI
# function-calling format. Each entry specifies the tool's name, description,
# and parameter schema. The agent loop sends this list to the LLM so it knows
# what tools exist and how to call them.
#
# The descriptions are carefully worded to guide the LLM toward correct usage:
#   - They mention caching behaviour so the LLM doesn't force-refresh needlessly
#   - They suggest parameter values (e.g. "0=due today, 7=this week")
#   - They note when tools are destructive (restart, delete) so the LLM
#     only uses them when explicitly asked

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "slate_check_assignments",
            "description": (
                "Check Sheridan Slate for pending assignments, quizzes, and discussions. "
                "Uses a 5-minute cache — fast if recently fetched. "
                "Use days_ahead to filter: 0=due today, 1=tomorrow, 7=this week. "
                "Omit days_ahead to see everything pending. "
                "Items overdue >30 days are automatically hidden."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "Only show items due within this many days. Omit for all pending items.",
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "Force a fresh fetch from Slate, bypassing the cache.",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_get_assignment_details",
            "description": "Get full instructions, due date, and attachments for a specific item by ID. Get the ID from slate_check_assignments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assignment_id": {"type": "string", "description": "Item ID from slate_check_assignments"},
                },
                "required": ["assignment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_download_docs",
            "description": "Download all attached documents for an assignment and zip them.",
            "parameters": {
                "type": "object",
                "properties": {"assignment_id": {"type": "string"}},
                "required": ["assignment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_action_plan",
            "description": "Generate a step-by-step action plan for completing a specific assignment.",
            "parameters": {
                "type": "object",
                "properties": {"assignment_id": {"type": "string"}},
                "required": ["assignment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_check_announcements",
            "description": "Check for new course announcements. Uses cache.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer", "description": "Show announcements from the last N days (default 7)", "default": 7},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_check_grades",
            "description": "Check for recent grade updates. Uses cache.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer", "description": "Show grades from the last N days (default 30)", "default": 30},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_check_messages",
            "description": "Check for unread Slate messages. Uses cache.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "slate_refresh",
            "description": "Force a fresh fetch from Slate right now, bypassing the 5-minute cache.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a Telegram reminder at a future time. Examples: 'in 30 minutes', 'tomorrow at 9am', '2026-03-25 14:00'",
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {"type": "string", "description": "When: 'in 30 minutes', 'tomorrow at 9am', '2026-03-25 14:00'"},
                    "message": {"type": "string", "description": "What to remind about"},
                },
                "required": ["when", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_apple_reminder",
            "description": "Create a rich Apple Reminder in iCloud. Supports priority/urgent reminders, place text, people tags, subtasks, and choosing a reminder list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Reminder title"},
                    "when": {"type": "string", "description": "Optional due time like 'tomorrow at 9am'"},
                    "notes": {"type": "string", "description": "Optional notes"},
                    "priority": {"type": "string", "description": "low, medium, or high"},
                    "urgent": {"type": "boolean", "description": "Use true for a clearly urgent reminder", "default": False},
                    "location": {"type": "string", "description": "Optional place/location text"},
                    "people": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional people associated with the reminder. Email addresses are preferred.",
                    },
                    "subtasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional child reminders / subtasks to create under this reminder.",
                    },
                    "list_name": {"type": "string", "description": "Optional Apple Reminders list name"},
                    "alert_minutes_before": {"type": "integer", "default": 60},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_apple_reminders",
            "description": "List Apple Reminders from iCloud.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10},
                    "include_completed": {"type": "boolean", "default": False},
                    "list_name": {"type": "string", "description": "Optional Apple Reminders list name"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_apple_reminder",
            "description": "Update an existing Apple Reminder by UID, exact title, or unique partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "UID, exact title, or unique partial title"},
                    "title": {"type": "string"},
                    "when": {"type": "string", "description": "Optional new due time like 'tomorrow at 9am'"},
                    "clear_when": {"type": "boolean", "default": False},
                    "notes": {"type": "string"},
                    "clear_notes": {"type": "boolean", "default": False},
                    "priority": {"type": "string", "description": "low, medium, or high"},
                    "urgent": {"type": "boolean"},
                    "location": {"type": "string"},
                    "clear_location": {"type": "boolean", "default": False},
                    "people": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "clear_people": {"type": "boolean", "default": False},
                    "list_name": {"type": "string"},
                    "alert_minutes_before": {"type": "integer"},
                    "completed": {"type": "boolean"},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_apple_reminder",
            "description": "Delete an Apple Reminder by UID, exact title, or unique partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "list_name": {"type": "string"},
                    "delete_subtasks": {"type": "boolean", "default": True},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "List all pending reminders.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "Cancel a reminder by number (from list_reminders) or partial text.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_apple_calendar_event",
            "description": "Create an Apple Calendar event in iCloud. Use when the user asks to add something to Apple Calendar, iPhone Calendar, or iCloud Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "Start time like 'tomorrow at 9am'"},
                    "end": {"type": "string", "description": "Optional end time"},
                    "notes": {"type": "string", "description": "Optional notes"},
                    "location": {"type": "string", "description": "Optional location"},
                    "alert_minutes_before": {"type": "integer", "default": 30},
                },
                "required": ["title", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_apple_calendar_events",
            "description": "List upcoming Apple Calendar events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 7},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_apple_calendar_event",
            "description": "Update an Apple Calendar event by UID, exact title, or unique partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "Optional new start time"},
                    "end": {"type": "string", "description": "Optional new end time"},
                    "clear_end": {"type": "boolean", "default": False},
                    "notes": {"type": "string"},
                    "clear_notes": {"type": "boolean", "default": False},
                    "location": {"type": "string"},
                    "clear_location": {"type": "boolean", "default": False},
                    "alert_minutes_before": {"type": "integer"},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_apple_calendar_event",
            "description": "Delete an Apple Calendar event by UID, exact title, or unique partial title.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "Open a web page in the persistent browser session.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http or https URL to open"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_current_page",
            "description": "Show the current browser page title and URL.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_interactives",
            "description": "List visible inputs, buttons, links, and other interactive elements with suggested selectors for the current page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_items": {"type": "integer", "default": 25},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click the first matching element on the current page using a CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an input on the current page using a CSS selector. For secrets already stored in the environment, pass text as env:VAR_NAME.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "text": {"type": "string", "description": "Text to type, or env:VAR_NAME to read a secret from the environment"},
                    "press_enter": {"type": "boolean", "default": False},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_create_context",
            "description": "Create a reusable Browserbase context and optionally save its ID to .env as BROWSERBASE_CONTEXT_ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "save_to_env": {"type": "boolean", "default": True},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": "Read text from the current page. If selector is empty, summarize page text; otherwise extract matching element text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Optional CSS selector"},
                    "max_items": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Save a screenshot of the current browser page on the server.",
            "parameters": {
                "type": "object",
                "properties": {"full_page": {"type": "boolean", "default": True}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_upload_file",
            "description": "Upload a local server file into a file input on the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the file input"},
                    "file_path": {"type": "string", "description": "Absolute or repo-relative file path on the server"},
                },
                "required": ["selector", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_download",
            "description": "Download a file through the browser by clicking a selector or opening a direct URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Optional CSS selector that triggers a download"},
                    "url": {"type": "string", "description": "Optional direct download URL"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_login_status",
            "description": "Report the current browser page URL, title, cookie count, and a best-effort login-state guess.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_reset",
            "description": "Force-close the browser session and aggressively clean up any leftover browser processes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hybrid_web_lookup",
            "description": "Try both web search and a direct browser check, then return both evidence streams. Use this for current, site-specific requests like prices, flyers, menus, hours, retailer pages, or product availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query for fresh web results"},
                    "page_url": {"type": "string", "description": "Optional page URL to open directly in the browser"},
                    "preferred_domain": {"type": "string", "description": "Optional preferred domain like foodbasics.ca"},
                    "browser_selector": {"type": "string", "description": "Optional selector to read from the browser page after opening it"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_run",
            "description": "Run a bounded non-interactive shell command on the EC2 host. Hermes kills child processes after completion or timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Optional working directory"},
                    "timeout_seconds": {"type": "integer", "default": 20},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_background_jobs",
            "description": "List running or recent background sub-agent jobs for the current chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_done": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "background_job_status",
            "description": "Show status for the latest background sub-agent job, or match one by ID or prompt text.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_background_job",
            "description": "Cancel a running background sub-agent job by ID or prompt text.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_status",
            "description": "Check systemd status for a service on the EC2 host.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string", "description": "Systemd service name"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_restart",
            "description": "Restart a systemd service on the EC2 host. Only use when the user explicitly asks to restart/fix a service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string", "description": "Systemd service name"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_logs",
            "description": "Fetch recent journal logs for a systemd service on the EC2 host.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Systemd service name"},
                    "lines": {"type": "integer", "default": 100},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Create a task with optional due time, priority, and notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due": {"type": "string", "description": "Optional due time like 'tomorrow at 9am'"},
                    "priority": {"type": "string", "description": "low, medium, or high", "default": "medium"},
                    "notes": {"type": "string", "description": "Optional notes"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List tasks by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "open or done", "default": "open"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a task done by ID or partial title.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reopen_task",
            "description": "Reopen a completed task by ID or partial title.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Delete a task by ID or partial title.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_from_slate",
            "description": "Create a task from a Slate assignment, quiz, or discussion ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assignment_id": {"type": "string"},
                    "priority": {"type": "string", "default": "high"},
                },
                "required": ["assignment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save something to persistent Hermes memory. Use for preferences, facts, or context about the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "memory_type": {"type": "string", "enum": ["note", "user", "project", "feedback", "routine", "contact"], "default": "note"},
                    "description": {"type": "string", "description": "Short summary for future recall"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search stored memories by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "memory_type": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List all stored memories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete a memory by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current info, news, or anything the model may not know.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# ── Tool dispatch map ─────────────────────────────────────────────────────────
# Maps tool name strings (matching the "name" fields in TOOLS above) to
# callable Python functions. The agent loop uses this to execute tool calls
# returned by the LLM.
#
# Most entries use lambda wrappers (lambda **kw: func(**kw)) so that keyword
# arguments from the LLM's JSON are forwarded correctly. Functions that take
# no parameters (like slate_check_messages, browser_current_page) are
# referenced directly without a lambda.
#
# Note: list_background_jobs, background_job_status, and cancel_background_job
# require a chat_id parameter that the agent loop injects -- it's not provided
# by the LLM in its tool call arguments.

TOOL_CALLABLES = {
    "slate_check_assignments":    lambda **kw: slate_check_assignments(**kw),
    "slate_get_assignment_details": lambda **kw: slate_get_assignment_details(**kw),
    "slate_download_docs":        lambda **kw: slate_download_docs(**kw),
    "slate_action_plan":          lambda **kw: slate_action_plan(**kw),
    "slate_check_announcements":  lambda **kw: slate_check_announcements(**kw),
    "slate_check_grades":         lambda **kw: slate_check_grades(**kw),
    "slate_check_messages":       slate_check_messages,
    "slate_refresh":              slate_refresh,
    "set_reminder":               lambda **kw: set_reminder(**kw),
    "set_apple_reminder":         lambda **kw: set_apple_reminder(**kw),
    "list_apple_reminders":       lambda **kw: list_apple_reminders(**kw),
    "update_apple_reminder":      lambda **kw: update_apple_reminder(**kw),
    "delete_apple_reminder":      lambda **kw: delete_apple_reminder(**kw),
    "list_reminders":             list_reminders,
    "cancel_reminder":            lambda **kw: cancel_reminder(**kw),
    "add_apple_calendar_event":   lambda **kw: add_apple_calendar_event(**kw),
    "list_apple_calendar_events": lambda **kw: list_apple_calendar_events(**kw),
    "update_apple_calendar_event": lambda **kw: update_apple_calendar_event(**kw),
    "delete_apple_calendar_event": lambda **kw: delete_apple_calendar_event(**kw),
    "browser_open":               lambda **kw: browser_open(**kw),
    "browser_current_page":       browser_current_page,
    "browser_interactives":       lambda **kw: browser_interactives(**kw),
    "browser_click":              lambda **kw: browser_click(**kw),
    "browser_type":               lambda **kw: browser_type(**kw),
    "browser_create_context":     lambda **kw: browser_create_context(**kw),
    "browser_read":               lambda **kw: browser_read(**kw),
    "browser_screenshot":         lambda **kw: browser_screenshot(**kw),
    "browser_upload_file":        lambda **kw: browser_upload_file(**kw),
    "browser_download":           lambda **kw: browser_download(**kw),
    "browser_login_status":       browser_login_status,
    "browser_reset":              browser_reset,
    "hybrid_web_lookup":          lambda **kw: hybrid_web_lookup(**kw),
    "terminal_run":               lambda **kw: terminal_run(**kw),
    "list_background_jobs":       list_background_jobs,
    "background_job_status":      background_job_status,
    "cancel_background_job":      cancel_background_job,
    "service_status":             lambda **kw: service_status(**kw),
    "service_restart":            lambda **kw: service_restart(**kw),
    "service_logs":               lambda **kw: service_logs(**kw),
    "add_task":                   lambda **kw: add_task(**kw),
    "list_tasks":                 lambda **kw: list_tasks(**kw),
    "complete_task":              lambda **kw: complete_task(**kw),
    "reopen_task":                lambda **kw: reopen_task(**kw),
    "delete_task":                lambda **kw: delete_task(**kw),
    "task_from_slate":            lambda **kw: task_from_slate(**kw),
    "remember":                   lambda **kw: remember(**kw),
    "recall":                     lambda **kw: recall(**kw),
    "list_memories":              list_memories,
    "forget":                     lambda **kw: forget(**kw),
    "web_search":                 lambda **kw: web_search(**kw),
    "get_current_time":           get_current_time,
}
