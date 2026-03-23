#!/usr/bin/env python3
"""
CLI for Slate tools — called by Hermes agent via terminal.
Usage:
  python slate_cli.py assignments [--days-ahead N] [--refresh]
  python slate_cli.py details <id>
  python slate_cli.py announcements [--days-back N]
  python slate_cli.py grades [--days-back N]
  python slate_cli.py messages
  python slate_cli.py refresh
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))

load_dotenv(os.path.expanduser("~/.hermes/.env"))

SLATE_SESSION = Path(os.path.expanduser("~/.hermes/slate_session.json"))
MAX_OVERDUE_DAYS = 30
CACHE_FILE = Path(os.path.expanduser("~/.hermes/slate_cache.json"))


def cache_age():
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
    age = cache_age()
    if age is None:
        return "no cache"
    if age < 60:
        return f"fetched {int(age)}s ago"
    return f"fetched {int(age // 60)}m {int(age % 60)}s ago"


def cache_save(data):
    from slate.cache import save

    save(data)


def cache_load():
    from slate.cache import load

    return load()


def cache_invalidate():
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()


def get_data(force_refresh=False):
    if not force_refresh:
        cached = cache_load()
        if cached is not None:
            return cached, pull_time_str()

    async def _fetch():
        from slate.client import SlateClient

        async with SlateClient() as c:
            return await c.get_everything()

    data = asyncio.run(_fetch())
    cache_save(data)
    return data, "just fetched"


def filter_deliverables(items, days_ahead=None):
    result = []
    for item in items:
        if getattr(item, "is_submitted", False):
            continue
        days = item.days_until_due()
        if days is not None and days < -MAX_OVERDUE_DAYS:
            continue
        if days_ahead is not None:
            if days is None or days > days_ahead:
                continue
        result.append(item)
    return result


def merge_calendar(data):
    from slate.models import Assignment

    existing = data["assignments"] + data["quizzes"] + data["discussions"]

    def _key(course_id, name):
        return (course_id, name.lower().strip())

    known = set()
    by_key = {}
    by_id = {}
    for item in existing:
        item_key = _key(item.course.id, item.name)
        known.add(item_key)
        by_key[item_key] = item
        by_id[(item.course.id, str(item.id))] = item

    course_map = {c.id: c for c in data.get("courses", [])}
    extras = []

    for ev in data.get("calendar_events", []):
        etype = ev.get("EventType", -1)
        title = ev.get("Title", "")
        assoc = ev.get("AssociatedEntity") or {}
        assoc_type = str(assoc.get("AssociatedEntityType") or "")
        is_deliverable_assoc = any(
            token in assoc_type for token in ("Dropbox", "Quizzing", "Discussion")
        )
        if etype not in (2, 3, 4) and " - Due" not in title and "Due" not in title and not is_deliverable_assoc:
            continue

        clean = title
        for suffix in (" - Due", " - Availability Ends", " - Availability Starts"):
            if clean.endswith(suffix):
                clean = clean[: -len(suffix)]
                break

        org_id = str(ev.get("AssociatedOrgUnitId") or ev.get("OrgUnitId") or "")
        course = course_map.get(org_id)
        if not course:
            continue

        due_str = ev.get("EndDateTime") or ev.get("StartDateTime")
        due_date = None
        if due_str:
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
                try:
                    due_date = datetime.strptime(due_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

        assoc_id = str(assoc.get("AssociatedEntityId") or "")
        existing_item = by_id.get((org_id, assoc_id)) if assoc_id else None
        if existing_item is None:
            existing_item = by_key.get(_key(org_id, clean))

        if existing_item is not None:
            if due_date and getattr(existing_item, "due_date", None) is None:
                existing_item.due_date = due_date
            if due_date and hasattr(existing_item, "end_date") and getattr(existing_item, "end_date", None) is None:
                existing_item.end_date = due_date
            continue

        if _key(org_id, clean) in known:
            continue

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
    if not items:
        return f"Nothing pending. ({pull_time})"

    emoji = {
        "overdue": "🔴",
        "due_today": "🚨",
        "urgent": "🟠",
        "upcoming": "🟡",
        "future": "🟢",
        "no_deadline": "⚪",
    }
    kind = {"assignment": "📝", "group": "👥", "quiz": "📋", "discussion": "💬"}

    def sort_key(item):
        due = getattr(item, "due_date", None) or getattr(item, "end_date", None)
        return due if due else datetime.max.replace(tzinfo=timezone.utc)

    lines = [f"{label} ({pull_time})\n"]
    for item in sorted(items, key=sort_key):
        item_kind = getattr(item, "kind", item.__class__.__name__.lower())
        lines.append(f"{emoji.get(item.urgency(), '')}{kind.get(item_kind, '📌')} [{item.id}] {item.name}")
        lines.append(f"   {item.course.code} — {item.due_str()}")
    return "\n".join(lines)


def cmd_assignments(args):
    if not SLATE_SESSION.exists():
        print("Not logged into Slate. Run auth locally and scp session file.")
        return
    data, pull_time = get_data(force_refresh=args.refresh)
    pending = filter_deliverables(merge_calendar(data), days_ahead=args.days_ahead)
    label = f"Due in next {args.days_ahead}d" if args.days_ahead is not None else "Pending deliverables"
    print(fmt_items(pending, pull_time, label))


def cmd_details(args):
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
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    data, pull_time = get_data()
    now = datetime.now(tz=timezone.utc)
    items = []
    for announcement in data["announcements"]:
        if not announcement.is_new:
            continue
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
            print(f"   {announcement.body[:300]}")


def cmd_grades(args):
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
    if not SLATE_SESSION.exists():
        print("Not logged into Slate.")
        return
    cache_invalidate()
    _, pull_time = get_data(force_refresh=True)
    print(f"Slate data refreshed. ({pull_time})")


def main():
    parser = argparse.ArgumentParser(description="Slate CLI for Hermes")
    sub = parser.add_subparsers(dest="cmd")

    assignments = sub.add_parser("assignments", help="Check pending assignments/quizzes/discussions")
    assignments.add_argument("--days-ahead", type=int, default=None)
    assignments.add_argument("--refresh", action="store_true")

    details = sub.add_parser("details", help="Get details for a specific item")
    details.add_argument("id", type=str)

    announcements = sub.add_parser("announcements", help="Check announcements")
    announcements.add_argument("--days-back", type=int, default=7)

    grades = sub.add_parser("grades", help="Check grades")
    grades.add_argument("--days-back", type=int, default=30)

    sub.add_parser("messages", help="Check unread messages")
    sub.add_parser("refresh", help="Force fresh fetch")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

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
