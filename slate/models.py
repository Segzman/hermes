"""
Domain models for the Slate (D2L Brightspace) integration.

All deliverables (Assignment, Quiz, Discussion) share a common pattern:
  - A due date with urgency classification
  - A submission flag (is_submitted)
  - Human-readable summary and status formatting

Dates are stored in UTC internally and converted to America/Toronto for display,
since Sheridan College is in Ontario.

Design notes:
  - Dataclasses are used for simplicity and easy serialization (cache.py uses
    dataclasses.asdict for JSON persistence).
  - The urgency system maps days-until-due to color-coded categories that
    drive emoji display in the CLI and notification priority in checker.py.
  - Quiz uses end_date as a fallback for due_date because D2L sometimes only
    sets the availability window, not an explicit due date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Sheridan College is in the Eastern time zone (Toronto).
# All user-facing date strings are localized to this timezone.
_TZ = ZoneInfo("America/Toronto")
_TZ_LABEL = "Toronto"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _due_str(due_date: Optional[datetime], is_submitted: bool = False, submitted_label: str = "Done") -> str:
    """
    Format a due date into a human-readable status string.

    Returns contextual text like "OVERDUE by 3d", "Due TODAY 11:59 PM Toronto",
    or "Due TOMORROW 11:59 PM Toronto". If the item is submitted, shows a
    checkmark with the submitted_label (e.g. "Done" or "Posted" for discussions).
    """
    if is_submitted:
        return f"✅ {submitted_label}"
    if not due_date:
        return "No deadline"
    now = datetime.now(tz=timezone.utc)
    # Ensure the due date is timezone-aware before computing the delta
    due = due_date if due_date.tzinfo else due_date.replace(tzinfo=timezone.utc)
    due_local = due.astimezone(_TZ)
    days = (due - now).days
    if days < 0:
        return f"OVERDUE by {abs(days)}d"
    if days == 0:
        return f"Due TODAY {due_local.strftime('%I:%M %p')} {_TZ_LABEL}"
    if days == 1:
        return f"Due TOMORROW {due_local.strftime('%I:%M %p')} {_TZ_LABEL}"
    # For items further out, show the full weekday + date + time
    return due_local.strftime(f"Due %a %b %d %I:%M %p {_TZ_LABEL}")


def _days_until(due_date: Optional[datetime]) -> Optional[int]:
    """
    Calculate integer days until the due date. Negative values mean overdue.
    Returns None if there is no due date.
    """
    if not due_date:
        return None
    now = datetime.now(tz=timezone.utc)
    due = due_date if due_date.tzinfo else due_date.replace(tzinfo=timezone.utc)
    return (due - now).days


def _urgency(due_date: Optional[datetime], is_done: bool = False) -> str:
    """
    Classify urgency into a category string used for emoji mapping and
    notification filtering.

    Categories (in order of severity):
      "done"        — already submitted / graded
      "overdue"     — past due date
      "due_today"   — due within the next 24 hours
      "urgent"      — due within 2 days
      "upcoming"    — due within 7 days
      "future"      — more than 7 days away
      "no_deadline" — no due date set
    """
    if is_done:
        return "done"
    days = _days_until(due_date)
    if days is None:
        return "no_deadline"
    if days < 0:
        return "overdue"
    if days == 0:
        return "due_today"
    if days <= 2:
        return "urgent"
    if days <= 7:
        return "upcoming"
    return "future"


# ── Shared base ───────────────────────────────────────────────────────────────

@dataclass
class Course:
    """A Brightspace course offering (orgUnit). The id is the D2L OrgUnitId."""
    id: str
    name: str       # Full course name, e.g. "Web Programming - SEC. 001"
    code: str       # Short code, e.g. "SYST10049_241_37728"
    url: str        # Direct link to the course homepage on Slate


@dataclass
class Attachment:
    """A file attached to a dropbox folder (assignment). URL points to the D2L download endpoint."""
    name: str
    url: str
    size_bytes: Optional[int] = None


# ── Deliverables (things with a due date / submission) ────────────────────────

@dataclass
class Assignment:
    """
    A dropbox folder in D2L. Represents homework, projects, or group submissions.

    The 'kind' field distinguishes solo assignments ("assignment") from group
    work ("group"), determined by checking the category name or folder name
    for the word "group".
    """
    id: str
    name: str
    course: Course
    due_date: Optional[datetime]
    instructions: str
    attachments: list[Attachment] = field(default_factory=list)
    is_submitted: bool = False
    score: Optional[float] = None
    total_score: Optional[float] = None
    kind: str = "assignment"        # assignment | group

    def days_until_due(self) -> Optional[int]:
        return _days_until(self.due_date)

    def urgency(self) -> str:
        return _urgency(self.due_date, self.is_submitted)

    def due_str(self) -> str:
        return _due_str(self.due_date, self.is_submitted)

    def summary(self) -> str:
        """One-line summary with type emoji for CLI display."""
        tag = "👥" if self.kind == "group" else "📝"
        return f"{tag} [{self.course.code}] {self.name} — {self.due_str()}"


@dataclass
class Quiz:
    """
    A D2L quiz. Has both a due_date and an availability window (start_date to
    end_date). When due_date is not set, end_date is used as the effective deadline.
    """
    id: str
    name: str
    course: Course
    due_date: Optional[datetime]
    start_date: Optional[datetime]      # When the quiz becomes available
    end_date: Optional[datetime]        # When the quiz availability closes
    time_limit_minutes: Optional[int]   # Enforced time limit, if any
    attempts_allowed: int               # Number of attempts the student gets
    is_submitted: bool = False
    score: Optional[float] = None
    total_score: Optional[float] = None

    def days_until_due(self) -> Optional[int]:
        # Fall back to end_date if due_date is not explicitly set
        return _days_until(self.due_date or self.end_date)

    def urgency(self) -> str:
        return _urgency(self.due_date or self.end_date, self.is_submitted)

    def due_str(self) -> str:
        return _due_str(self.due_date or self.end_date, self.is_submitted)

    def summary(self) -> str:
        limit = f" ({self.time_limit_minutes}min)" if self.time_limit_minutes else ""
        return f"📋 [{self.course.code}] {self.name}{limit} — {self.due_str()}"


@dataclass
class Discussion:
    """
    A D2L discussion topic. The id is formatted as "{forum_id}_{topic_id}"
    to uniquely identify topics across forums. The submitted_label is "Posted"
    instead of "Done" because discussions are posted, not submitted.
    """
    id: str
    name: str
    course: Course
    due_date: Optional[datetime]
    description: str
    is_submitted: bool = False

    def days_until_due(self) -> Optional[int]:
        return _days_until(self.due_date)

    def urgency(self) -> str:
        return _urgency(self.due_date, self.is_submitted)

    def due_str(self) -> str:
        # Use "Posted" instead of "Done" for discussions
        return _due_str(self.due_date, self.is_submitted, "Posted")

    def summary(self) -> str:
        return f"💬 [{self.course.code}] {self.name} — {self.due_str()}"


# ── Informational items ───────────────────────────────────────────────────────

@dataclass
class Announcement:
    """A D2L news/announcement item. is_new reflects the D2L IsRead flag (inverted)."""
    id: str
    title: str
    course: Course
    body: str
    posted_at: Optional[datetime]
    is_new: bool = True

    def summary(self) -> str:
        when = self.posted_at.strftime("%b %d") if self.posted_at else "unknown"
        return f"📢 [{self.course.code}] {self.title} ({when})"


@dataclass
class GradeUpdate:
    """
    A graded item from the D2L gradebook. Used both for display and as a
    heuristic to mark deliverables as submitted — if a grade exists for an
    item name, the corresponding assignment/quiz is assumed to be submitted.
    """
    course: Course
    item_name: str
    score: float
    total: float
    graded_at: Optional[datetime]

    @property
    def percent(self) -> float:
        """Calculate percentage score, guarding against division by zero."""
        return (self.score / self.total * 100) if self.total else 0.0

    def summary(self) -> str:
        when = self.graded_at.strftime("%b %d") if self.graded_at else ""
        return f"🎓 [{self.course.code}] {self.item_name}: {self.score}/{self.total} ({self.percent:.1f}%) {when}"


@dataclass
class SlateMessage:
    """An internal Slate/Brightspace message (the built-in email system)."""
    id: str
    subject: str
    sender_name: str
    sender_email: str
    body: str
    sent_at: Optional[datetime]
    is_read: bool = False

    def summary(self) -> str:
        when = self.sent_at.strftime("%b %d") if self.sent_at else ""
        unread = "🔵 " if not self.is_read else ""
        return f"{unread}✉️  {self.subject} — from {self.sender_name} ({when})"
