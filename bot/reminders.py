"""
Reminder system -- APScheduler with SQLite persistence.
Survives restarts. Delivers via Telegram (+ optional Apple Reminders).

Architecture notes:
  - APScheduler is used with a SQLAlchemy/SQLite job store so that
    scheduled reminders persist across bot restarts.
  - The scheduler runs in UTC internally; all user-facing times are
    converted to/from Toronto local time.
  - Two types of reminders coexist:
    1. User-created reminders (job IDs prefixed with "reminder_")
    2. Slate-synced reminders (job IDs prefixed with "slate_") that
       fire when assignments/quizzes are due. These are managed
       automatically by sync_slate_reminders().
  - The _fire callback is a module-level function (not a lambda or
    nested function) because APScheduler needs to pickle job callbacks
    for SQLite persistence.
  - Slate reminder state is tracked in a JSON file so the system can
    detect when due dates change and notify the user.
"""

import os
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

load_dotenv()

# SQLite database file for persisting scheduled jobs across restarts
REMINDERS_DB = Path(os.path.expanduser("~/.hermes/reminders.db"))
# JSON file tracking Slate reminder state for change detection
SLATE_REMINDER_STATE = Path(os.path.expanduser("~/.hermes/slate_reminders.json"))
# Default Telegram chat ID for delivering reminders
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LOCAL_TZ = ZoneInfo("America/Toronto")

_scheduler: Optional[AsyncIOScheduler] = None
_send_fn: Optional[Callable] = None          # set by telegram_bot at startup


def init_scheduler(send_fn: Callable) -> AsyncIOScheduler:
    """
    Initialise the APScheduler with SQLite persistence.

    Called once at bot startup from telegram_bot._post_init.
    The send_fn callback is used by _fire() to deliver reminder messages
    to Telegram.
    """
    global _scheduler, _send_fn
    _send_fn = send_fn
    if _scheduler is not None:
        return _scheduler
    REMINDERS_DB.parent.mkdir(parents=True, exist_ok=True)
    _scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{REMINDERS_DB}")},
        timezone="UTC",
    )
    return _scheduler


def get_scheduler() -> Optional[AsyncIOScheduler]:
    """Return the scheduler instance, or None if not yet initialised."""
    return _scheduler


# ── time parsing ──────────────────────────────────────────────────────────────

# Mapping of time unit names to timedelta objects for "in X units" parsing
_UNITS = {
    "second": timedelta(seconds=1), "seconds": timedelta(seconds=1),
    "minute": timedelta(minutes=1), "minutes": timedelta(minutes=1),
    "hour":   timedelta(hours=1),   "hours":   timedelta(hours=1),
    "day":    timedelta(days=1),    "days":    timedelta(days=1),
    "week":   timedelta(weeks=1),   "weeks":   timedelta(weeks=1),
}

def parse_when(when: str) -> Optional[datetime]:
    """
    Parse natural language time expressions into a UTC datetime.

    Supported formats:
      - "in 30 minutes", "in 2 hours", "in 3 days"
      - "tomorrow at 9am", "tomorrow at 14:00", "tomorrow" (defaults to 9:00 AM)
      - "today at 3pm"
      - ISO formats: "2026-03-25 14:30", "2026-03-25T14:30", "2026-03-25"

    All relative/local times are interpreted in Toronto timezone, then
    converted to UTC for storage.

    Returns None if the format is not recognised.
    """
    s = when.strip().lower()
    now_utc = datetime.now(tz=timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)

    # "in X unit" — relative time from now
    m = re.match(r"in\s+(\d+)\s+(\w+)", s)
    if m:
        # Normalise unit name: strip trailing 's' then add it back for lookup
        n, unit = int(m.group(1)), m.group(2).rstrip("s") + "s"
        delta = _UNITS.get(unit) or _UNITS.get(m.group(2))
        if delta:
            return now_utc + delta * n

    # "tomorrow at HH:MM" or just "tomorrow" (defaults to 9:00 AM Toronto)
    m = re.match(r"tomorrow(?:\s+at\s+(.+))?", s)
    if m:
        base = (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        if m.group(1):
            t = _parse_time_of_day(m.group(1).strip())
            if t:
                base = base.replace(hour=t[0], minute=t[1])
        return base.astimezone(timezone.utc)

    # "today at HH:MM" — if the time has already passed today, schedule for tomorrow
    m = re.match(r"today\s+at\s+(.+)", s)
    if m:
        t = _parse_time_of_day(m.group(1).strip())
        if t:
            dt = now_local.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
            if dt < now_local:
                dt += timedelta(days=1)
            return dt.astimezone(timezone.utc)

    # ISO datetime formats
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            pass

    return None


def _parse_time_of_day(s: str):
    """
    Parse a time-of-day string into (hour, minute) tuple.

    Supported formats:
      - "14:30" (24-hour)
      - "9:00" (24-hour)
      - "9am", "2:30pm" (12-hour with am/pm)

    Returns None if the format is not recognised.
    """
    # "14:30", "9:00", or "2:30pm"
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if m.group(3) == "pm" and h < 12:
            h += 12
        elif m.group(3) == "am" and h == 12:
            h = 0  # 12am = midnight
        return (h, mn)
    # "9am", "2pm" (no minutes)
    m = re.match(r"(\d{1,2})\s*(am|pm)", s)
    if m:
        h = int(m.group(1))
        if m.group(2) == "pm" and h < 12:
            h += 12
        elif m.group(2) == "am" and h == 12:
            h = 0
        return (h, 0)
    return None


# ── job callback (must be module-level for APScheduler pickling) ──────────────

async def _fire(message: str, chat_id: str) -> None:
    """
    Callback executed by APScheduler when a reminder fires.

    Must be a module-level function (not a closure) because APScheduler
    serialises job callbacks to the SQLite store using pickle, and closures
    are not picklable.
    """
    if _send_fn:
        await _send_fn(chat_id, f"⏰ *Reminder:* {message}")


def _slate_item_key(item) -> str:
    """
    Generate a stable key for a Slate deliverable item.

    Uses course_id:kind:item_id to uniquely identify each item across
    syncs, even if the name changes.
    """
    kind = getattr(item, "kind", item.__class__.__name__.lower())
    return f"{item.course.id}:{kind}:{item.id}"


def _slate_job_id(key: str) -> str:
    """
    Convert a Slate item key into an APScheduler job ID.

    Sanitises the key to be APScheduler-safe (alphanumeric + underscores)
    and caps the length at 180 characters.
    """
    safe = "".join(ch if ch.isalnum() else "_" for ch in key)
    return f"slate_{safe}"[:180]


def _load_slate_state() -> dict[str, dict]:
    """Load the previous Slate reminder sync state from the JSON file."""
    if not SLATE_REMINDER_STATE.exists():
        return {}
    try:
        return json.loads(SLATE_REMINDER_STATE.read_text())
    except Exception:
        return {}


def _save_slate_state(state: dict[str, dict]) -> None:
    """Persist the current Slate reminder sync state to the JSON file."""
    SLATE_REMINDER_STATE.parent.mkdir(parents=True, exist_ok=True)
    SLATE_REMINDER_STATE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ── public API ────────────────────────────────────────────────────────────────

def set_reminder(when: str, message: str, chat_id: str = None) -> str:
    """
    Schedule a one-time reminder.

    The job ID includes a timestamp and hash to avoid collisions.
    replace_existing=True means setting the same reminder twice updates it.
    misfire_grace_time=300 means if the bot was down when the reminder
    was supposed to fire, it will still fire within 5 minutes of restart.
    """
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not _scheduler:
        return "Scheduler not started yet."
    dt = parse_when(when)
    if not dt:
        return (
            f"Couldn't understand '{when}'.\n"
            "Try: 'in 30 minutes', 'in 2 hours', 'tomorrow at 9am', '2026-03-25 14:00'"
        )
    now = datetime.now(tz=timezone.utc)
    if dt <= now:
        return "That time is in the past. Please give a future time."

    job_id = f"reminder_{int(dt.timestamp())}_{hash(message) % 10000}"
    _scheduler.add_job(
        _fire,
        trigger=DateTrigger(run_date=dt),
        args=[message, chat_id],
        id=job_id,
        name=message[:60],
        replace_existing=True,
        misfire_grace_time=300,
    )
    when_fmt = dt.astimezone(LOCAL_TZ).strftime("%A, %b %d at %I:%M %p Toronto")
    return f"✅ Reminder set for {when_fmt}:\n\"{message}\""


def list_reminders() -> str:
    """
    List all pending user-created reminders with their fire times.

    Shows both the absolute time and a relative ETA (e.g. "in 2h 30m").
    Sorted by next fire time (soonest first).
    """
    if not _scheduler:
        return "Scheduler not running."
    # Only show user-created reminders (not Slate-synced ones)
    jobs = [j for j in _scheduler.get_jobs() if j.id.startswith("reminder_")]
    if not jobs:
        return "No pending reminders."
    now = datetime.now(tz=timezone.utc)
    lines = []
    for i, job in enumerate(sorted(jobs, key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=timezone.utc)), 1):
        when = job.next_run_time
        if when:
            # Calculate relative time remaining
            diff = when - now
            h, rem = divmod(int(diff.total_seconds()), 3600)
            m = rem // 60
            eta = f"{h}h {m}m" if h else f"{m}m"
            when_local = when.astimezone(LOCAL_TZ)
            when_str = f"{when_local.strftime('%a %b %d %I:%M%p Toronto')} (in {eta})"
        else:
            when_str = "unknown"
        lines.append(f"{i}. {when_str} — {job.name}")
    return "⏰ Pending reminders:\n" + "\n".join(lines)


def cancel_reminder(ref: str) -> str:
    """
    Cancel a reminder by 1-based index number or partial name match.

    The index corresponds to the order shown by list_reminders().
    """
    if not _scheduler:
        return "Scheduler not running."
    jobs = sorted(
        [j for j in _scheduler.get_jobs() if j.id.startswith("reminder_")],
        key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=timezone.utc),
    )
    if not jobs:
        return "No pending reminders to cancel."
    # Try matching by 1-based index number first
    try:
        idx = int(ref) - 1
        if 0 <= idx < len(jobs):
            name = jobs[idx].name
            jobs[idx].remove()
            return f'✅ Cancelled reminder: "{name}"'
    except ValueError:
        pass
    # Fall back to partial name match
    ref_l = ref.lower()
    for job in jobs:
        if ref_l in job.name.lower():
            job.remove()
            return f'✅ Cancelled reminder: "{job.name}"'
    return f'No reminder found matching "{ref}".'


def clear_slate_reminders() -> int:
    """
    Remove all Slate-synced reminders (called at startup to clean up
    legacy jobs from previous runs before re-syncing).

    Returns the number of jobs removed.
    """
    removed = 0
    if _scheduler:
        for job in list(_scheduler.get_jobs()):
            if job.id.startswith("slate_"):
                job.remove()
                removed += 1
    if SLATE_REMINDER_STATE.exists():
        try:
            previous = _load_slate_state()
            removed = max(removed, len(previous))
            SLATE_REMINDER_STATE.unlink()
        except Exception:
            pass
    return removed


async def sync_slate_reminders(items: list, chat_id: str = None) -> None:
    """
    Synchronise Slate assignment due-date reminders with APScheduler.

    This is called periodically after fetching Slate data. It:
      1. Creates/updates a reminder for each pending item with a due date
      2. Detects due-date changes and notifies the user
      3. Removes reminders for items that were submitted or deleted
      4. Persists the current state for the next sync comparison

    Items that are already submitted, have no due date, or are already
    past due are skipped.
    """
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not _scheduler or not chat_id:
        return

    now = datetime.now(tz=timezone.utc)
    previous = _load_slate_state()
    current: dict[str, dict] = {}

    for item in items:
        # Skip already-submitted items
        if getattr(item, "is_submitted", False):
            continue
        due_at = getattr(item, "due_date", None) or getattr(item, "end_date", None)
        if not due_at:
            continue
        due_at = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
        # Skip items that are already past due
        if due_at <= now:
            continue

        key = _slate_item_key(item)
        current[key] = {
            "id": str(item.id),
            "name": item.name,
            "course_code": item.course.code,
            "kind": getattr(item, "kind", item.__class__.__name__.lower()),
            "due_at": due_at.isoformat(),
        }

        # Schedule a reminder that fires at the due time
        job_id = _slate_job_id(key)
        message = f"{item.name} — {item.course.code} is due now."
        _scheduler.add_job(
            _fire,
            trigger=DateTrigger(run_date=due_at),
            args=[message, chat_id],
            id=job_id,
            name=f"{item.name} ({item.course.code})",
            replace_existing=True,
            # 15-minute grace period for misfired jobs (e.g. bot was restarting)
            misfire_grace_time=900,
        )

        # Check if the due date or name changed since last sync
        old = previous.get(key)
        if not old:
            continue

        changed = old.get("due_at") != current[key]["due_at"] or old.get("name") != item.name
        if changed and _send_fn:
            old_due = old.get("due_at")
            try:
                old_dt = datetime.fromisoformat(old_due).astimezone(LOCAL_TZ) if old_due else None
            except Exception:
                old_dt = None
            new_dt = due_at.astimezone(LOCAL_TZ)
            old_label = old_dt.strftime("%a %b %d %I:%M %p Toronto") if old_dt else "unknown time"
            new_label = new_dt.strftime("%a %b %d %I:%M %p Toronto")
            await _send_fn(
                chat_id,
                f"ℹ️ *Slate update:* `{item.name}` ({item.course.code}) changed from {old_label} to {new_label}.",
            )

    # Clean up reminders for items that were removed or submitted
    removed_keys = set(previous) - set(current)
    for key in removed_keys:
        job = _scheduler.get_job(_slate_job_id(key))
        if job:
            job.remove()
        old = previous.get(key, {})
        if _send_fn and old.get("name"):
            await _send_fn(
                chat_id,
                f"ℹ️ *Slate update:* stopped tracking `{old['name']}` ({old.get('course_code', '')}) because it was removed or submitted.",
            )

    _save_slate_state(current)
