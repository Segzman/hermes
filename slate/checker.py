"""
Main checker + interactive CLI for Slate (D2L Brightspace).

Usage:
    python -m slate.checker                         # list all pending items
    python -m slate.checker --watch                 # run on schedule
    python -m slate.checker --assignments           # assignments only
    python -m slate.checker --quizzes               # quizzes only
    python -m slate.checker --discussions            # discussions only
    python -m slate.checker --announcements         # announcements
    python -m slate.checker --grades                # recent grade updates
    python -m slate.checker --messages              # unread Slate messages
    python -m slate.checker --calendar              # calendar (next 30 days)
    python -m slate.checker --context <id>          # full assignment context
    python -m slate.checker --download <id>         # download & zip docs
    python -m slate.checker --plan <id>             # generate action plan prompt

Architecture:
  This module serves as both a standalone Rich CLI and the periodic checker
  that runs in --watch mode on the server. It:
    1. Fetches data via SlateClient.get_everything()
    2. Displays formatted tables using Rich
    3. Sends notifications for new/urgent items via notifier.py
    4. Optionally syncs reminders to Apple Reminders via bot.reminders

  The notification deduplication system uses a JSON file (~/.hermes/notified_ids.json)
  to track which items have already been notified about, preventing duplicate
  alerts across checker runs.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from .auth import SESSION_FILE
from .client import SlateClient
from .models import Assignment, Discussion, Quiz
from .notifier import notify

load_dotenv()

console = Console()

# How often the --watch mode checks for updates (in minutes)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))

# How many days ahead to consider items "notification-worthy"
REMINDER_DAYS = int(os.getenv("REMINDER_DAYS_AHEAD", "3"))

# Persistent set of item IDs that have already been notified about,
# preventing duplicate notifications across checker runs
_NOTIFIED_FILE = Path(os.path.expanduser("~/.hermes/notified_ids.json"))


def _load_notified() -> set[str]:
    """Load the set of previously-notified item IDs from disk."""
    if _NOTIFIED_FILE.exists():
        return set(json.loads(_NOTIFIED_FILE.read_text()))
    return set()


def _save_notified(ids: set[str]) -> None:
    """Persist the notified IDs set to disk for cross-run deduplication."""
    _NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _NOTIFIED_FILE.write_text(json.dumps(list(ids)))


# Emoji mapping for urgency levels — used in both table display and notifications
URGENCY_EMOJI = {
    "overdue": "🔴",
    "due_today": "🚨",
    "urgent": "🟠",
    "upcoming": "🟡",
    "future": "🟢",
    "no_deadline": "⚪",
    "done": "✅",
}


def _emoji(urgency: str) -> str:
    """Look up the emoji for an urgency level, returning empty string if unknown."""
    return URGENCY_EMOJI.get(urgency, "")


def _should_notify(item, notified: set[str]) -> bool:
    """
    Determine whether an item warrants a notification.

    Criteria:
      - Not already submitted or read
      - Urgency is overdue, due_today, urgent, or upcoming
      - Due date is within the window: not more than 7 days overdue, and
        not more than REMINDER_DAYS ahead (avoids spamming about items
        due far in the future)
      - Not already in the notified set (deduplication)
    """
    if getattr(item, "is_submitted", False) or getattr(item, "is_read", True):
        return False
    days = getattr(item, "days_until_due", lambda: None)()
    urgency = getattr(item, "urgency", lambda: "future")()
    item_id = str(getattr(item, "id", ""))
    return (
        urgency in ("overdue", "due_today", "urgent", "upcoming")
        and (days is not None and -7 <= days <= REMINDER_DAYS)  # ignore ancient overdue items
        and item_id not in notified
    )


# ── display helpers ───────────────────────────────────────────────────────────

def _print_deliverables(assignments: list, quizzes: list, discussions: list) -> None:
    """
    Print a Rich table of all pending (unsubmitted) deliverables.

    Filters out submitted items and items overdue by more than 30 days
    (likely from previous semesters or abandoned courses).
    """
    # Only show items due within the last 30 days or in the future (skip ancient old-course junk)
    def _is_relevant(item) -> bool:
        if item.is_submitted:
            return False
        days = item.days_until_due()
        if days is None:
            return True   # no deadline — show it (could be important)
        return days >= -30  # overdue by at most 30 days

    pending = (
        [a for a in assignments if _is_relevant(a)] +
        [q for q in quizzes if _is_relevant(q)] +
        [d for d in discussions if _is_relevant(d)]
    )
    if not pending:
        console.print("[green]All deliverables submitted![/green]")
        return

    def sort_key(item):
        """Sort by due date ascending, with no-deadline items at the end."""
        due = getattr(item, "due_date", None) or getattr(item, "end_date", None)
        if due:
            return due if due.tzinfo else due.replace(tzinfo=timezone.utc)
        return datetime.max.replace(tzinfo=timezone.utc)

    table = Table(title="Pending Deliverables", box=box.ROUNDED, show_lines=True)
    table.add_column("", width=2)           # Urgency emoji
    table.add_column("Type", width=10)
    table.add_column("Course", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Due", style="yellow")
    table.add_column("ID", style="dim", no_wrap=True)

    type_labels = {"assignment": "Assignment", "group": "Group Work", "quiz": "Quiz", "discussion": "Discussion"}

    for item in sorted(pending, key=sort_key):
        kind = getattr(item, "kind", item.__class__.__name__.lower())
        table.add_row(
            _emoji(item.urgency()),
            type_labels.get(kind, kind.title()),
            item.course.code,
            item.name,
            item.due_str(),
            item.id,
        )
    console.print(table)


def _print_announcements(items: list) -> None:
    """Print a Rich table of new (unread) announcements."""
    if not items:
        return
    new = [a for a in items if a.is_new]
    if not new:
        console.print("[dim]No new announcements.[/dim]")
        return
    table = Table(title=f"Announcements ({len(new)} new)", box=box.SIMPLE)
    table.add_column("Course", style="cyan")
    table.add_column("Title")
    table.add_column("Posted")
    for a in new:
        when = a.posted_at.strftime("%b %d") if a.posted_at else ""
        table.add_row(a.course.code, a.title, when)
    console.print(table)


def _print_grades(items: list) -> None:
    """Print a Rich table of recent grade updates, sorted by date descending."""
    if not items:
        console.print("[dim]No graded items found.[/dim]")
        return
    table = Table(title="Recent Grades", box=box.SIMPLE)
    table.add_column("Course", style="cyan")
    table.add_column("Item")
    table.add_column("Score", style="green")
    table.add_column("Date")
    for g in sorted(items, key=lambda x: x.graded_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        when = g.graded_at.strftime("%b %d") if g.graded_at else ""
        table.add_row(g.course.code, g.item_name, f"{g.score}/{g.total} ({g.percent:.0f}%)", when)
    console.print(table)


def _print_messages(items: list) -> None:
    """Print a Rich table of unread Slate messages."""
    unread = [m for m in items if not m.is_read]
    if not unread:
        console.print("[dim]No unread messages.[/dim]")
        return
    table = Table(title=f"Unread Messages ({len(unread)})", box=box.SIMPLE)
    table.add_column("From")
    table.add_column("Subject")
    table.add_column("Date")
    table.add_column("ID", style="dim")
    for m in unread:
        when = m.sent_at.strftime("%b %d") if m.sent_at else ""
        table.add_row(m.sender_name, m.subject, when, m.id)
    console.print(table)


def _print_calendar(events: list) -> None:
    """Print a Rich table of raw calendar events (next 30 days)."""
    if not events:
        console.print("[dim]No upcoming calendar events.[/dim]")
        return
    table = Table(title="Calendar (next 30 days)", box=box.SIMPLE)
    table.add_column("Date", style="yellow")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("Course", style="cyan")
    for e in events:
        from .client import _parse_date
        dt = _parse_date(e.get("StartDateTime") or e.get("DueDate", ""))
        date_str = dt.strftime("%b %d %I:%M%p") if dt else ""
        table.add_row(date_str, e.get("EventType", "Event"), e.get("Title", ""), e.get("OrgUnitCode", ""))
    console.print(table)


# ── commands ──────────────────────────────────────────────────────────────────

async def cmd_all() -> dict:
    """
    Fetch and display all data: deliverables, announcements, and messages.
    This is the default command when no flags are specified.
    """
    if not SESSION_FILE.exists():
        console.print("[red]No session. Run: python -m slate.auth[/red]")
        return {}
    async with SlateClient() as client:
        data = await client.get_everything()
    _print_deliverables(data["assignments"], data["quizzes"], data["discussions"])
    _print_announcements(data["announcements"])
    _print_messages(data["messages"])
    return data


async def cmd_run_check(notify_new: bool = True, sync_reminders: bool = False) -> None:
    """
    Fetch everything and send notifications where appropriate.

    This is the core function called by the --watch scheduler. It:
      1. Fetches all data from D2L
      2. Merges calendar events with known deliverables (via bot.tools)
      3. Optionally syncs reminders to Apple Reminders
      4. Sends notifications for new/urgent items that haven't been
         notified about yet (deduplication via notified_ids.json)
      5. Also notifies about new announcements, grades, and unread messages
    """
    if not SESSION_FILE.exists():
        return
    async with SlateClient() as client:
        data = await client.get_everything()

    notified = _load_notified()
    # Use the bot's merge_calendar to combine API data with calendar events,
    # catching deliverables that only appear in the calendar
    from bot.tools import _merge_calendar
    pending_items = _merge_calendar(data)

    # Optionally sync upcoming deadlines to Apple Reminders
    if sync_reminders:
        try:
            from bot.reminders import sync_slate_reminders
            await sync_slate_reminders(pending_items)
        except Exception as e:
            console.print(f"[yellow]Warning: Slate reminder sync failed: {e}[/yellow]")

    if notify_new:
        # Notify about pending deliverables that are urgent and not yet notified
        for item in pending_items:
            if _should_notify(item, notified):
                course_name = item.course.name
                await notify(
                    title=f"{_emoji(item.urgency())} {item.name}",
                    body=f"{course_name}\n{item.due_str()}",
                    due=getattr(item, "due_date", None),
                    url=item.course.url,
                )
                notified.add(str(item.id))

        # Notify about new announcements
        for ann in data["announcements"]:
            if ann.is_new and ann.id not in notified:
                await notify(
                    title=f"📢 {ann.course.code}: {ann.title}",
                    body=ann.body[:200] + ("..." if len(ann.body) > 200 else ""),
                )
                notified.add(ann.id)

        # Notify about new grades
        for grade in data["grades"]:
            # Construct a unique ID for each grade to avoid duplicate notifications
            gid = f"grade_{grade.course.id}_{grade.item_name}"
            if gid not in notified:
                await notify(
                    title=f"🎓 Grade posted: {grade.course.code}",
                    body=f"{grade.item_name}: {grade.score}/{grade.total} ({grade.percent:.0f}%)",
                )
                notified.add(gid)

        # Notify about unread messages
        for msg in data["messages"]:
            if not msg.is_read and msg.id not in notified:
                await notify(
                    title=f"✉️  New Slate message: {msg.subject}",
                    body=f"From: {msg.sender_name}\n{msg.body[:200]}",
                )
                notified.add(msg.id)

    _save_notified(notified)


async def cmd_context(assignment_id: str) -> None:
    """Fetch and display the full context (instructions, attachments) for an assignment."""
    async with SlateClient() as client:
        all_a = await client.get_everything()
        for a in all_a["assignments"]:
            if a.id == assignment_id:
                ctx = await client.get_assignment_context(a)
                console.print(Panel(ctx, title=a.name))
                return
    console.print(f"[red]Assignment {assignment_id} not found.[/red]")


async def cmd_download(assignment_id: str) -> None:
    """Download all attachments for an assignment and bundle them into a zip."""
    async with SlateClient() as client:
        data = await client.get_everything()
        for a in data["assignments"]:
            if a.id == assignment_id:
                console.print(f"Downloading docs for [cyan]{a.name}[/cyan]...")
                path = await client.download_assignment_docs(a)
                console.print(f"[green]Saved to: {path}[/green]")
                return
    console.print(f"[red]Assignment {assignment_id} not found.[/red]")


async def cmd_plan(assignment_id: str) -> None:
    """
    Generate an LLM-ready action plan prompt for an assignment.

    Fetches the assignment context and wraps it in a structured prompt that
    can be pasted into Hermes (or any LLM) to get a step-by-step plan.
    """
    async with SlateClient() as client:
        data = await client.get_everything()
        for a in data["assignments"]:
            if a.id == assignment_id:
                context = await client.get_assignment_context(a)
                prompt = f"""
You are helping a Sheridan College student complete the following assignment.
Read the details below, then:
1. Summarize what needs to be done in plain language
2. List all deliverables clearly
3. Create a step-by-step action plan with realistic time estimates
4. Flag any risks, confusing requirements, or things to watch out for
5. Suggest useful resources or approaches

---
{context}
---

Produce the action plan now.
""".strip()
                console.print(Panel(prompt, title="Paste into Hermes", border_style="cyan"))
                return
    console.print(f"[red]Assignment {assignment_id} not found.[/red]")


async def cmd_watch() -> None:
    """
    Run the checker on a recurring schedule using APScheduler.

    Performs an immediate check on start, then repeats every CHECK_INTERVAL
    minutes. Blocks until Ctrl+C or a SystemExit signal.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    console.print(f"Watcher started — checking every [cyan]{CHECK_INTERVAL}[/cyan] min. Ctrl+C to stop.")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(cmd_run_check, "interval", minutes=CHECK_INTERVAL)
    scheduler.start()

    await cmd_run_check()   # immediate check on start
    await cmd_all()         # print current state

    try:
        # Block forever until interrupted — the scheduler runs in the background
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        console.print("\nWatcher stopped.")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    def _get_arg(flag: str) -> str | None:
        """Extract the value following a CLI flag (e.g., --context <id>)."""
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
            console.print(f"[red]{flag} requires an ID argument[/red]")
            sys.exit(1)
        return None

    # Route to the appropriate command based on CLI flags.
    # Each command creates an async function inline and runs it with asyncio.run().
    if "--watch" in args:
        asyncio.run(cmd_watch())
    elif "--context" in args:
        asyncio.run(cmd_context(_get_arg("--context")))
    elif "--download" in args:
        asyncio.run(cmd_download(_get_arg("--download")))
    elif "--plan" in args:
        asyncio.run(cmd_plan(_get_arg("--plan")))
    elif "--grades" in args:
        async def _grades():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_grades(data["grades"])
        asyncio.run(_grades())
    elif "--messages" in args:
        async def _msgs():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_messages(data["messages"])
        asyncio.run(_msgs())
    elif "--announcements" in args:
        async def _ann():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_announcements(data["announcements"])
        asyncio.run(_ann())
    elif "--calendar" in args:
        async def _cal():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_calendar(data["calendar_events"])
        asyncio.run(_cal())
    elif "--quizzes" in args:
        async def _quiz():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_deliverables([], data["quizzes"], [])
        asyncio.run(_quiz())
    elif "--discussions" in args:
        async def _disc():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_deliverables([], [], data["discussions"])
        asyncio.run(_disc())
    elif "--assignments" in args:
        async def _asgn():
            async with SlateClient() as c:
                data = await c.get_everything()
            _print_deliverables(data["assignments"], [], [])
        asyncio.run(_asgn())
    else:
        # Default: show everything
        asyncio.run(cmd_all())
