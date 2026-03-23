"""
Persistent task system for Hermes.

Tracks actionable items separately from point-in-time reminders.
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
    return Path(os.path.expanduser(os.getenv("TASKS_DB", "~/.hermes/tasks.db")))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def _fmt_due(due_at: Optional[datetime]) -> str:
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
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "⚪")


@dataclass
class Task:
    id: int
    title: str
    notes: str
    status: str
    priority: str
    due_at: Optional[datetime]
    source: str
    source_id: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
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
        source = f" [{self.source}:{self.source_id}]" if self.source and self.source_id else ""
        notes = f"\n   {self.notes}" if self.notes else ""
        status = "✅" if self.status == "done" else _priority_emoji(self.priority)
        return f"{status} {self.id}. {self.title}{source}\n   {_fmt_due(self.due_at)}{notes}"


def _normalize_priority(priority: str) -> str:
    value = (priority or "medium").strip().lower()
    return value if value in _VALID_PRIORITIES else "medium"


def _normalize_status(status: str) -> str:
    value = (status or "open").strip().lower()
    return value if value in _VALID_STATUSES else "open"


def _resolve_task(conn: sqlite3.Connection, ref: str) -> Optional[Task]:
    ref = str(ref).strip()
    if not ref:
        return None
    if ref.isdigit():
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(ref),)).fetchone()
        return Task.from_row(row) if row else None
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
    with _db() as conn:
        task = _resolve_task(conn, ref)
        if not task:
            return None
        conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        return task


def get_task(ref: str) -> Optional[Task]:
    with _db() as conn:
        return _resolve_task(conn, ref)


def format_task_list(status: str = "open", limit: int = 20) -> str:
    tasks = list_tasks(status=status, limit=limit)
    label = "Open tasks" if status == "open" else "Completed tasks"
    if not tasks:
        return f"No {status} tasks."
    lines = [f"{label}:"]
    for task in tasks:
        lines.append(task.summary())
    return "\n".join(lines)
