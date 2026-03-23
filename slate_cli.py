#!/usr/bin/env python3
"""
CLI for Slate tools — called by Hermes agent via terminal.

This is the lightweight CLI interface that the Hermes AI agent invokes
to query Slate data. Unlike checker.py (which uses Rich for interactive
display), this module produces plain-text output suitable for LLM consumption.

Usage:
  python slate_cli.py assignments [--days-ahead N] [--refresh]
  python slate_cli.py details <id>
  python slate_cli.py announcements [--days-back N]
  python slate_cli.py grades [--days-back N]
  python slate_cli.py messages
  python slate_cli.py refresh

Architecture:
  - Uses the cache module to avoid redundant D2L API calls within a conversation
  - Merges calendar events with known deliverables to catch items that only
    appear in the D2L calendar (not in the dropbox/quiz/discussion APIs)
  - Formats output with emoji urgency indicators for quick scanning
  - All data flows through get_data() which handles cache read/write
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Add the project root to the Python path so 'slate' package can be imported
sys.path.insert(0, os.path.dirname(__file__))

# Load environment variables from the Hermes .env file
load_dotenv(os.path.expanduser("~/.hermes/.env"))

# Path to the saved Slate session cookies (from slate.auth)
SLATE_SESSION = Path(os.path.expanduser("~/.hermes/slate_session.json"))

# Don't show items overdue by more than this many days — they're likely
# from previous semesters or abandoned courses
MAX_OVERDUE_DAYS = 30

# Path to the JSON cache file (mirrors slate.cache.CACHE_FILE)
CACHE_FILE = Path(os.path.expanduser("~/.hermes/slate_cache.json"))


def cache_age():
    """
    Return the age of the cache in seconds, or None if no valid cache exists.

    Reads only the _meta.fetched_at field from the cache file to determine
    freshness without deserializing the full payload.
    """
    import json

    if not CACHE_FILE.exists():
        return None
    try:
        meta = json.loads(CACHE_FILE.read_text()).get("_meta", {})
        ts = meta.get("fetched_at")
        if not ts:
            return None
        return (datetime.now(tz=timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return None


def pull_time_str():
    """
    Return a human-readable string describing cache freshness.
    Examples: "no cache", "fetched 42s ago", "fetched 3m 15s ago".
    Displayed alongside results so the agent knows how fresh the data is.
    """
    age = cache_age()
    if age is None:
        return "no cache"
    if age < 60:
        return f"fetched {int(age)}s ago"
    return f"fetched {int(age // 60)}m {int(age % 60)}s ago"


def cache_save(data):
    """Delegate to slate.cache.save to persist fetched data."""
    from slate.cache import save

    save(data)


def cache_load():
    """Delegate to slate.cache.load to retrieve cached data (returns None if stale)."""
    from slate.cache import load

    return load()


def cache_invalidate():
    """Delete the cache file, forcing the next get_data() call to fetch fresh data."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()


def get_data(force_refresh=False):
    """
    Get Slate data, using cache when available.

    Returns a tuple of (data_dict, pull_time_string). If force_refresh is True
    or the cache is stale, performs a fresh API fetch via SlateClient.
    """
    if not force_refresh:
        cached = cache_load()
        if cached is not None:
            return cached, pull_time_str()

    # Fresh fetch — create a SlateClient, fetch everything, and cache the result
    async def _fetch():
        from slate.client import SlateClient

        async with SlateClient() as c:
            return await c.get_everything()

    data = asyncio.run(_fetch())
    cache_save(data)
    return data, "just fetched"


def filter_deliverables(items, days_ahead=None):
    """
    Filter a list of deliverable items (assignments, quizzes, discussions).

    Removes:
      - Already-submitted items
      - Items overdue by more than MAX_OVERDUE_DAYS (stale/old-semester items)
      - Items beyond the days_ahead window (if specified)

    Returns the filtered list, preserving the original order.
    """
    result = []
    for item in items:
        # Skip submitted items
        if getattr(item, "is_submitted", False):
            continue
        days = item.days_until_due()
        # Skip items that are extremely overdue (likely from past semesters)
        if days is not None and days < -MAX_OVERDUE_DAYS:
            continue
        # If a days_ahead filter is set, skip items beyond that window
        if days_ahead is not None:
            if days is None or days > days_ahead:
                continue
        result.append(item)
    return result


def merge_calendar(data):
    """
    Merge calendar events with existing deliverables to catch items that
    only appear in the D2L calendar.

    Some instructors create due dates via calendar events without corresponding
    dropbox folders or quiz entries. This function:
      1. Builds a lookup of known deliverables by (course_id, name) and
         (course_id, associated_entity_id)
      2. Scans calendar events for deliverable-like entries (EventType 2/3/4,
         or titles containing "Due", or associated with Dropbox/Quiz/Discussion)
      3. For events matching existing items: fills in missing due dates
      4. For truly new events: creates synthetic Assignment objects

    Returns the combined list of existing + calendar-derived deliverables.
    """
    from slate.models import Assignment

    # Collect all existing deliverables from the three API sources
    existing = data["assignments"] + data["quizzes"] + data["discussions"]

    def _key(course_id, name):
        """Normalize lookup key: (course_id, lowercase stripped name)."""
        return (course_id, name.lower().strip())

    # Build lookup structures for deduplication
    known = set()       # Set of normalized keys for quick membership check
    by_key = {}         # Map from normalized key to item (for due date backfill)
    by_id = {}          # Map from (course_id, entity_id) to item
    for item in existing:
        item_key = _key(item.course.id, item.name)
        known.add(item_key)
        by_key[item_key] = item
        by_id[(item.course.id, str(item.id))] = item

    # Build a course lookup for resolving calendar event org unit IDs
    course_map = {c.id: c for c in data.get("courses", [])}
    extras = []

    for ev in data.get("calendar_events", []):
        # D2L calendar EventType: 2=assignment, 3=quiz, 4=discussion (approx.)
        etype = ev.get("EventType", -1)
        title = ev.get("Title", "")
        assoc = ev.get("AssociatedEntity") or {}
        assoc_type = str(assoc.get("AssociatedEntityType") or "")
        # Check if the associated entity is a deliverable type
        is_deliverable_assoc = any(
            token in assoc_type for token in ("Dropbox", "Quizzing", "Discussion")
        )
        # Skip events that don't look like deliverables
        if etype not in (2, 3, 4) and " - Due" not in title and "Due" not in title and not is_deliverable_assoc:
            continue

        # Clean the title by removing D2L-appended suffixes like " - Due"
        clean = title
        for suffix in (" - Due", " - Availability Ends", " - Availability Starts"):
            if clean.endswith(suffix):
                clean = clean[: -len(suffix)]
                break

        # Resolve the course from the event's org unit ID
        org_id = str(ev.get("AssociatedOrgUnitId") or ev.get("OrgUnitId") or "")
        course = course_map.get(org_id)
        if not course:
            continue  # Skip events from unknown/filtered courses

        # Parse the due date from the event's end or start time
        due_str = ev.get("EndDateTime") or ev.get("StartDateTime")
        due_date = None
        if due_str:
            # Try both with and without milliseconds (D2L is inconsistent)
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
                try:
                    due_date = datetime.strptime(due_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

        # Try to match this event to an existing deliverable
        assoc_id = str(assoc.get("AssociatedEntityId") or "")
        existing_item = by_id.get((org_id, assoc_id)) if assoc_id else None
        if existing_item is None:
            existing_item = by_key.get(_key(org_id, clean))

        # If we found a match, backfill missing due dates from the calendar
        if existing_item is not None:
            if due_date and getattr(existing_item, "due_date", None) is None:
                existing_item.due_date = due_date
            if due_date and hasattr(existing_item, "end_date") and getattr(existing_item, "end_date", None) is None:
                existing_item.end_date = due_date
            continue

        # Skip if we already know about this item by name
        if _key(org_id, clean) in known:
            continue

        # Create a synthetic Assignment for this calendar-only deliverable
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


def fmt_items(items, pull_time, label="Pending items"):
    """
    Format a list of deliverable items as plain text with emoji indicators.

    Output format per item:
      {urgency_emoji}{type_emoji} [{id}] {name}
         {course_code} — {due_status}

    Items are sorted by due date ascending (earliest first), with no-deadline
    items at the end.
    """
    if not items:
        return f"Nothing pending. ({pull_time})"

    # Urgency color indicators
    emoji = {
        "overdue": "🔴",
        "due_today": "🚨",
        "urgent": "🟠",
        "upcoming": "🟡",
        "future": "🟢",
        "no_deadline": "⚪",
    }
    # Item type indicators
    kind = {"assignment": "📝", "group": "👥", "quiz": "📋", "discussion": "💬"}

    def sort_key(item):
        """Sort by effective due date, with no-deadline items at the very end."""
        due = getattr(item, "due_date", None) or getattr(item, "end_date", None)
        return due if due else datetime.max.replace(tzinfo=timezone.utc)

    lines = [f"{label} ({pull_time})\n"]
    for item in sorted(items, key=sort_key):
        item_kind = getattr(item, "kind", item.__class__.__name__.lower())
        lines.append(f"{emoji.get(item.urgency(), '')}{kind.get(item_kind, '📌')} [{item.id}] {item.name}")
        lines.append(f"   {item.course.code} — {item.due_str()}")
    return "\n".join(lines)


# ── CLI command handlers ──────────────────────────────────────────────────────

def cmd_assignments(args):
    """List pending assignments/quizzes/discussions, optionally filtered by days ahead."""
    if not SLATE_SESSION.exists():
        print("Not logged into Slate. Run auth locally and scp session file.")
        return
    data, pull_time = get_data(force_refresh=args.refresh)
    pending = filter_deliverables(merge_calendar(data), days_ahead=args.days_ahead)
    label = f"Due in next {args.days_ahead}d" if args.days_ahead is not None else "Pending deliverables"
    print(fmt_items(pending, pull_time, label))


def cmd_details(args):
    """Show detailed information about a specific item by ID."""
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    data, pull_time = get_data()
    for item in merge_calendar(data):
        if str(item.id) != str(args.id):
            continue
        print(f"{item.name} ({pull_time})")
        print(f"Course: {item.course.name} ({item.course.code})")
        print(f"Status: {item.due_str()}")
        if hasattr(item, "instructions") and item.instructions:
            print(f"\nInstructions:\n{item.instructions}")
        if hasattr(item, "description") and item.description:
            print(f"\nDescription:\n{item.description}")
        if hasattr(item, "attachments") and item.attachments:
            print("\nAttachments:")
            for attachment in item.attachments:
                print(f"  - {attachment.name}")
        if hasattr(item, "time_limit_minutes") and item.time_limit_minutes:
            print(f"Time limit: {item.time_limit_minutes} min")
        return
    print(f"ID {args.id} not found. Run: python slate_cli.py assignments")


def cmd_announcements(args):
    """Show new announcements from the last N days (default 7)."""
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    data, pull_time = get_data()
    now = datetime.now(tz=timezone.utc)
    items = []
    for announcement in data["announcements"]:
        if not announcement.is_new:
            continue
        # Filter by age — only show announcements posted within the time window
        if announcement.posted_at:
            posted = announcement.posted_at if announcement.posted_at.tzinfo else announcement.posted_at.replace(tzinfo=timezone.utc)
            if (now - posted).days > args.days_back:
                continue
        items.append(announcement)
    if not items:
        print(f"No new announcements in the last {args.days_back} days. ({pull_time})")
        return
    print(f"Announcements (last {args.days_back}d) ({pull_time})\n")
    for announcement in items:
        when = announcement.posted_at.strftime("%b %d") if announcement.posted_at else ""
        print(f"📢 [{announcement.course.code}] {announcement.title} ({when})")
        if announcement.body:
            # Truncate long announcement bodies to keep output manageable
            print(f"   {announcement.body[:300]}")


def cmd_grades(args):
    """Show recent grade updates from the last N days (default 30)."""
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    data, pull_time = get_data()
    now = datetime.now(tz=timezone.utc)
    items = []
    for grade in data["grades"]:
        if grade.graded_at:
            graded_at = grade.graded_at if grade.graded_at.tzinfo else grade.graded_at.replace(tzinfo=timezone.utc)
            if (now - graded_at).days > args.days_back:
                continue
        items.append(grade)
    if not items:
        print(f"No grades in the last {args.days_back} days. ({pull_time})")
        return
    print(f"Recent Grades (last {args.days_back}d) ({pull_time})\n")
    for grade in sorted(items, key=lambda item: item.graded_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        print(grade.summary())


def cmd_messages(_args):
    """Show unread Slate messages."""
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    data, pull_time = get_data()
    unread = [message for message in data["messages"] if not message.is_read]
    if not unread:
        print(f"No unread messages. ({pull_time})")
        return
    print(f"Unread Messages ({pull_time})\n")
    for message in unread:
        print(f"✉️ [{message.id}] {message.subject}")
        print(f"   From: {message.sender_name}")
        if message.body:
            print(f"   {message.body[:200]}")


def cmd_refresh(_args):
    """Force a fresh fetch from D2L, bypassing the cache."""
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    cache_invalidate()
    _, pull_time = get_data(force_refresh=True)
    print(f"Slate data refreshed. ({pull_time})")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    """Parse CLI arguments and dispatch to the appropriate command handler."""
    parser = argparse.ArgumentParser(description="Slate CLI for Hermes")
    sub = parser.add_subparsers(dest="cmd")

    # Subcommand: assignments — list pending deliverables
    assignments = sub.add_parser("assignments", help="Check pending assignments/quizzes/discussions")
    assignments.add_argument("--days-ahead", type=int, default=None)
    assignments.add_argument("--refresh", action="store_true")

    # Subcommand: details — show details for a specific item
    details = sub.add_parser("details", help="Get details for a specific item")
    details.add_argument("id", type=str)

    # Subcommand: announcements — show recent announcements
    announcements = sub.add_parser("announcements", help="Check announcements")
    announcements.add_argument("--days-back", type=int, default=7)

    # Subcommand: grades — show recent grade updates
    grades = sub.add_parser("grades", help="Check grades")
    grades.add_argument("--days-back", type=int, default=30)

    # Subcommand: messages — show unread messages
    sub.add_parser("messages", help="Check unread messages")

    # Subcommand: refresh — force fresh fetch
    sub.add_parser("refresh", help="Force fresh fetch")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    # Dispatch to the appropriate command handler
    commands = {
        "assignments": cmd_assignments,
        "details": cmd_details,
        "announcements": cmd_announcements,
        "grades": cmd_grades,
        "messages": cmd_messages,
        "refresh": cmd_refresh,
    }
    commands[args.cmd](args)


if __name__ == "__main__":
    main()
