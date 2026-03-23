"""
Persistent task system for Hermes.

Tracks actionable items separately from point-in-time reminders.

Architecture notes:
  - Tasks are stored in a SQLite database (~/.hermes/tasks.db) for
    durability across restarts.
  - Unlike reminders (which fire at a specific time), tasks persist
    until explicitly completed or deleted. They represent ongoing
    to-do items.
  - Tasks can be imported from Slate assignments via task_from_slate()
    in the tools module, linking them back to the source via the
    source/source_id fields.
  - Resolution by reference (ref) supports both numeric IDs and
    partial title matches, making it easy for the user to say
    "complete task homework" without knowing the numeric ID.
  - The database schema is auto-created on first connection using
    CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

_TZ = ZoneInfo("America/Toronto")
_VALID_PRIORITIES = ("low", "medium", "high")
_VALID_STATUSES = ("open", "done")


def _db_path() -> Path:
    """Return the SQLite database file path (configurable via TASKS_DB env var)."""
    return Path(os.path.expanduser(os.getenv("TASKS_DB", "~/.hermes/tasks.db")))


def _connect() -> sqlite3.Connection:
    """
    Open a connection to the tasks database, creating it if needed.

    Uses sqlite3.Row as the row factory so results can be accessed
    by column name (dict-like access).
    """
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Auto-create the tasks table on first use
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'medium',
            due_at TEXT,
            source TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


@contextmanager
def _db():
    """
    Context manager for database operations.

    Automatically commits on success and rolls back on exception.
    Always closes the connection when done.
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    """Parse an ISO datetime string, returning None on failure."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def _fmt_due(due_at: Optional[datetime]) -> str:
    """
    Format a due date for display with relative context.

    Returns strings like:
      - "Overdue Mar 20 02:00 PM Toronto"
      - "Due today 02:00 PM Toronto"
      - "Due tomorrow 02:00 PM Toronto"
      - "Due Mon Mar 25 02:00 PM Toronto"
      - "No due date"
    """
    if not due_at:
        return "No due date"
    due = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
    local = due.astimezone(_TZ)
    today_local = datetime.now(tz=timezone.utc).astimezone(_TZ).date()
    day_diff = (local.date() - today_local).days
    if due < datetime.now(tz=timezone.utc):
        return f"Overdue {local.strftime('%b %d %I:%M %p Toronto')}"
    if day_diff == 0:
        return f"Due today {local.strftime('%I:%M %p Toronto')}"
    if day_diff == 1:
        return f"Due tomorrow {local.strftime('%I:%M %p Toronto')}"
    return local.strftime("Due %a %b %d %I:%M %p Toronto")


def _priority_emoji(priority: str) -> str:
    """Map a priority level to a coloured circle emoji."""
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "⚪")


@dataclass
class Task:
    """Represents a single task item from the database."""
    id: int
    title: str
    notes: str
    status: str
    priority: str
    due_at: Optional[datetime]
    source: str       # e.g. "slate" for imported tasks
    source_id: str    # e.g. the Slate assignment ID
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        """Construct a Task from a database row."""
        return cls(
            id=row["id"],
            title=row["title"],
            notes=row["notes"],
            status=row["status"],
            priority=row["priority"],
            due_at=_parse_dt(row["due_at"]),
            source=row["source"],
            source_id=row["source_id"],
            created_at=_parse_dt(row["created_at"]) or datetime.now(tz=timezone.utc),
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(tz=timezone.utc),
        )

    def summary(self) -> str:
        """Format the task as a multi-line summary for display."""
        source = f" [{self.source}:{self.source_id}]" if self.source and self.source_id else ""
        notes = f"\n   {self.notes}" if self.notes else ""
        status = "✅" if self.status == "done" else _priority_emoji(self.priority)
        return f"{status} {self.id}. {self.title}{source}\n   {_fmt_due(self.due_at)}{notes}"


def _normalize_priority(priority: str) -> str:
    """Normalise a priority string, defaulting to 'medium' if invalid."""
    value = (priority or "medium").strip().lower()
    return value if value in _VALID_PRIORITIES else "medium"


def _normalize_status(status: str) -> str:
    """Normalise a status string, defaulting to 'open' if invalid."""
    value = (status or "open").strip().lower()
    return value if value in _VALID_STATUSES else "open"


def _resolve_task(conn: sqlite3.Connection, ref: str) -> Optional[Task]:
    """
    Find a task by numeric ID or partial title match.

    For title matches, prefers open tasks over completed ones, and
    returns the most recently updated match (LIMIT 1).
    """
    ref = str(ref).strip()
    if not ref:
        return None
    # Try numeric ID first
    if ref.isdigit():
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(ref),)).fetchone()
        return Task.from_row(row) if row else None
    # Fall back to partial title match (case-insensitive)
    like = f"%{ref.lower()}%"
    row = conn.execute(
        "SELECT * FROM tasks WHERE lower(title) LIKE ? ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1",
        (like,),
    ).fetchone()
    return Task.from_row(row) if row else None


def add_task(
    title: str,
    due_at: Optional[datetime] = None,
    priority: str = "medium",
    notes: str = "",
    source: str = "",
    source_id: str = "",
) -> Task:
    """
    Create a new task and return the created Task object.

    Naive datetimes are assumed to be UTC. The source/source_id fields
    are used to link back to the original system (e.g. "slate" + assignment ID).
    """
    now = _now_iso()
    due_iso = (due_at if due_at and due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)).isoformat() if due_at else None
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (title, notes, status, priority, due_at, source, source_id, created_at, updated_at)
            VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """,
            (title.strip(), notes.strip(), _normalize_priority(priority), due_iso, source.strip(), source_id.strip(), now, now),
        )
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Task.from_row(row)


def list_tasks(status: str = "open", limit: int = 20) -> list[Task]:
    """
    List tasks filtered by status, ordered by priority (high first),
    then by due date (soonest first, nulls last), then by creation time.
    """
    status = _normalize_status(status)
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = ?
            ORDER BY
                CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                due_at,
                created_at DESC
            LIMIT ?
            """,
            (status, max(1, limit)),
        ).fetchall()
    return [Task.from_row(row) for row in rows]


def set_task_status(ref: str, status: str) -> Optional[Task]:
    """
    Change a task's status (e.g. open -> done).

    Returns the updated Task, or None if no task matched the reference.
    """
    status = _normalize_status(status)
    with _db() as conn:
        task = _resolve_task(conn, ref)
        if not task:
            return None
        now = _now_iso()
        conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now, task.id))
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()
        return Task.from_row(row)


def delete_task(ref: str) -> Optional[Task]:
    """
    Permanently delete a task.

    Returns the deleted Task for confirmation display, or None if no match.
    """
    with _db() as conn:
        task = _resolve_task(conn, ref)
        if not task:
            return None
        conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        return task


def get_task(ref: str) -> Optional[Task]:
    """Look up a single task by ID or partial title match."""
    with _db() as conn:
        return _resolve_task(conn, ref)


def format_task_list(status: str = "open", limit: int = 20) -> str:
    """Format a task list as a multi-line string for display."""
    tasks = list_tasks(status=status, limit=limit)
    label = "Open tasks" if status == "open" else "Completed tasks"
    if not tasks:
        return f"No {status} tasks."
    lines = [f"{label}:"]
    for task in tasks:
        lines.append(task.summary())
    return "\n".join(lines)
