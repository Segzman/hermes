"""
Reminder system — APScheduler with SQLite persistence.
Survives restarts. Delivers via Telegram (+ optional Apple Reminders).
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

REMINDERS_DB = Path(os.path.expanduser("~/.hermes/reminders.db"))
SLATE_REMINDER_STATE = Path(os.path.expanduser("~/.hermes/slate_reminders.json"))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LOCAL_TZ = ZoneInfo("America/Toronto")

_scheduler: Optional[AsyncIOScheduler] = None
_send_fn: Optional[Callable] = None          # set by telegram_bot at startup


def init_scheduler(send_fn: Callable) -> AsyncIOScheduler:
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
    return _scheduler


# ── time parsing ──────────────────────────────────────────────────────────────

_UNITS = {
    "second": timedelta(seconds=1), "seconds": timedelta(seconds=1),
    "minute": timedelta(minutes=1), "minutes": timedelta(minutes=1),
    "hour":   timedelta(hours=1),   "hours":   timedelta(hours=1),
    "day":    timedelta(days=1),    "days":    timedelta(days=1),
    "week":   timedelta(weeks=1),   "weeks":   timedelta(weeks=1),
}

def parse_when(when: str) -> Optional[datetime]:
    """
    Parse natural language:
      "in 30 minutes", "in 2 hours", "in 3 days"
      "tomorrow at 9am", "tomorrow at 14:00"
      "2026-03-25 14:30"
    Returns UTC datetime or None.
    """
    s = when.strip().lower()
    now_utc = datetime.now(tz=timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)

    # "in X unit"
    m = re.match(r"in\s+(\d+)\s+(\w+)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s") + "s"
        delta = _UNITS.get(unit) or _UNITS.get(m.group(2))
        if delta:
            return now_utc + delta * n

    # "tomorrow at HH:MM" or "tomorrow at H[am|pm]"
    m = re.match(r"tomorrow(?:\s+at\s+(.+))?", s)
    if m:
        base = (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        if m.group(1):
            t = _parse_time_of_day(m.group(1).strip())
            if t:
                base = base.replace(hour=t[0], minute=t[1])
        return base.astimezone(timezone.utc)

    # "today at HH:MM"
    m = re.match(r"today\s+at\s+(.+)", s)
    if m:
        t = _parse_time_of_day(m.group(1).strip())
        if t:
            dt = now_local.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
            if dt < now_local:
                dt += timedelta(days=1)
            return dt.astimezone(timezone.utc)

    # ISO datetime
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            pass

    return None


def _parse_time_of_day(s: str):
    # "14:30", "9:00", "9am", "2:30pm"
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if m.group(3) == "pm" and h < 12:
            h += 12
        elif m.group(3) == "am" and h == 12:
            h = 0
        return (h, mn)
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
    if _send_fn:
        await _send_fn(chat_id, f"⏰ *Reminder:* {message}")


def _slate_item_key(item) -> str:
    kind = getattr(item, "kind", item.__class__.__name__.lower())
    return f"{item.course.id}:{kind}:{item.id}"


def _slate_job_id(key: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in key)
    return f"slate_{safe}"[:180]


def _load_slate_state() -> dict[str, dict]:
    if not SLATE_REMINDER_STATE.exists():
        return {}
    try:
        return json.loads(SLATE_REMINDER_STATE.read_text())
    except Exception:
        return {}


def _save_slate_state(state: dict[str, dict]) -> None:
    SLATE_REMINDER_STATE.parent.mkdir(parents=True, exist_ok=True)
    SLATE_REMINDER_STATE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ── public API ────────────────────────────────────────────────────────────────

def set_reminder(when: str, message: str, chat_id: str = None) -> str:
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
    if not _scheduler:
        return "Scheduler not running."
    jobs = [j for j in _scheduler.get_jobs() if j.id.startswith("reminder_")]
    if not jobs:
        return "No pending reminders."
    now = datetime.now(tz=timezone.utc)
    lines = []
    for i, job in enumerate(sorted(jobs, key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=timezone.utc)), 1):
        when = job.next_run_time
        if when:
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
    """Cancel by 1-based index number or partial name match."""
    if not _scheduler:
        return "Scheduler not running."
    jobs = sorted(
        [j for j in _scheduler.get_jobs() if j.id.startswith("reminder_")],
        key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=timezone.utc),
    )
    if not jobs:
        return "No pending reminders to cancel."
    # by index
    try:
        idx = int(ref) - 1
        if 0 <= idx < len(jobs):
            name = jobs[idx].name
            jobs[idx].remove()
            return f'✅ Cancelled reminder: "{name}"'
    except ValueError:
        pass
    # by name
    ref_l = ref.lower()
    for job in jobs:
        if ref_l in job.name.lower():
            job.remove()
            return f'✅ Cancelled reminder: "{job.name}"'
    return f'No reminder found matching "{ref}".'


def clear_slate_reminders() -> int:
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
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not _scheduler or not chat_id:
        return

    now = datetime.now(tz=timezone.utc)
    previous = _load_slate_state()
    current: dict[str, dict] = {}

    for item in items:
        if getattr(item, "is_submitted", False):
            continue
        due_at = getattr(item, "due_date", None) or getattr(item, "end_date", None)
        if not due_at:
            continue
        due_at = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
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

        job_id = _slate_job_id(key)
        message = f"{item.name} — {item.course.code} is due now."
        _scheduler.add_job(
            _fire,
            trigger=DateTrigger(run_date=due_at),
            args=[message, chat_id],
            id=job_id,
            name=f"{item.name} ({item.course.code})",
            replace_existing=True,
            misfire_grace_time=900,
        )

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
