"""
Slate data cache — stores a full fetch in JSON, valid for CACHE_TTL seconds.
All tools read from here instead of hitting D2L on every message.

Architecture:
  The cache stores the complete output of SlateClient.get_everything() as a
  single JSON file at ~/.hermes/slate_cache.json. A "_meta" key holds the
  fetch timestamp used to determine freshness.

  Serialization converts dataclass model objects to plain dicts/strings (via
  dataclasses.asdict + custom datetime handling). Deserialization reconstructs
  the model objects, effectively acting as a simple ORM for the JSON file.

  The 5-minute TTL (CACHE_TTL) balances freshness with API politeness — the
  Hermes agent may query assignments multiple times in a single conversation,
  and we avoid making redundant API calls for each query.
"""

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Location of the cache file on disk
CACHE_FILE = Path(os.path.expanduser("~/.hermes/slate_cache.json"))

# Cache time-to-live in seconds. After this many seconds, cached data is
# considered stale and a fresh fetch from D2L will be triggered.
CACHE_TTL = 300   # 5 minutes


# ── serialise / deserialise ───────────────────────────────────────────────────

def _serialise(data: dict) -> dict:
    """
    Convert dataclass objects to plain dicts / strings for JSON serialization.

    Handles nested structures recursively:
      - datetime -> ISO 8601 string
      - dataclass -> dict (via duck-typing __dataclass_fields__)
      - list -> recursively converted list
      - dict -> recursively converted dict
      - everything else -> passed through as-is
    """
    def _conv(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _conv(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [_conv(i) for i in obj]
        if isinstance(obj, dict):
            return {k: _conv(v) for k, v in obj.items()}
        return obj
    return _conv(data)


def _parse_dt(s) -> Optional[datetime]:
    """
    Parse an ISO 8601 datetime string back into a datetime object.
    Returns None for empty/invalid strings rather than raising an exception,
    since some D2L fields are legitimately null.
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _deserialise(raw: dict) -> dict:
    """
    Reconstruct model objects from the JSON dict.

    Each model type has its own builder function that maps JSON keys back
    to the corresponding dataclass constructor. This is the inverse of
    _serialise and must be kept in sync with any model changes.
    """
    from slate.models import (
        Assignment, Attachment, Course,
        Discussion, GradeUpdate, Quiz, SlateMessage, Announcement,
    )

    def _course(d) -> Course:
        return Course(**d)

    def _attachment(d) -> Attachment:
        return Attachment(name=d["name"], url=d["url"], size_bytes=d.get("size_bytes"))

    def _assignment(d) -> Assignment:
        return Assignment(
            id=d["id"], name=d["name"],
            course=_course(d["course"]),
            due_date=_parse_dt(d.get("due_date")),
            instructions=d.get("instructions", ""),
            attachments=[_attachment(a) for a in d.get("attachments", [])],
            is_submitted=d.get("is_submitted", False),
            score=d.get("score"), total_score=d.get("total_score"),
            kind=d.get("kind", "assignment"),
        )

    def _quiz(d) -> Quiz:
        return Quiz(
            id=d["id"], name=d["name"],
            course=_course(d["course"]),
            due_date=_parse_dt(d.get("due_date")),
            start_date=_parse_dt(d.get("start_date")),
            end_date=_parse_dt(d.get("end_date")),
            time_limit_minutes=d.get("time_limit_minutes"),
            attempts_allowed=d.get("attempts_allowed", 1),
            is_submitted=d.get("is_submitted", False),
            score=d.get("score"), total_score=d.get("total_score"),
        )

    def _discussion(d) -> Discussion:
        return Discussion(
            id=d["id"], name=d["name"],
            course=_course(d["course"]),
            due_date=_parse_dt(d.get("due_date")),
            description=d.get("description", ""),
            is_submitted=d.get("is_submitted", False),
        )

    def _announcement(d) -> Announcement:
        return Announcement(
            id=d["id"], title=d["title"],
            course=_course(d["course"]),
            body=d.get("body", ""),
            posted_at=_parse_dt(d.get("posted_at")),
            is_new=d.get("is_new", True),
        )

    def _grade(d) -> GradeUpdate:
        return GradeUpdate(
            course=_course(d["course"]),
            item_name=d["item_name"],
            score=d["score"], total=d["total"],
            graded_at=_parse_dt(d.get("graded_at")),
        )

    def _message(d) -> SlateMessage:
        return SlateMessage(
            id=d["id"], subject=d["subject"],
            sender_name=d.get("sender_name", ""),
            sender_email=d.get("sender_email", ""),
            body=d.get("body", ""),
            sent_at=_parse_dt(d.get("sent_at")),
            is_read=d.get("is_read", True),
        )

    # Reconstruct each collection from its raw JSON list.
    # calendar_events are kept as raw dicts since they come directly from the
    # D2L calendar API and don't have a corresponding model class.
    return dict(
        courses=[_course(c) for c in raw.get("courses", [])],
        assignments=[_assignment(a) for a in raw.get("assignments", [])],
        quizzes=[_quiz(q) for q in raw.get("quizzes", [])],
        discussions=[_discussion(d) for d in raw.get("discussions", [])],
        announcements=[_announcement(a) for a in raw.get("announcements", [])],
        grades=[_grade(g) for g in raw.get("grades", [])],
        messages=[_message(m) for m in raw.get("messages", [])],
        calendar_events=raw.get("calendar_events", []),
    )


# ── public API ────────────────────────────────────────────────────────────────

def get_age_seconds() -> Optional[float]:
    """
    Return how old the cache is in seconds, or None if no valid cache exists.
    Reads only the _meta.fetched_at field without deserializing the full payload.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        meta = json.loads(CACHE_FILE.read_text()).get("_meta", {})
        ts = meta.get("fetched_at")
        if not ts:
            return None
        fetched = datetime.fromisoformat(ts)
        return (datetime.now(tz=timezone.utc) - fetched).total_seconds()
    except Exception:
        return None


def is_fresh() -> bool:
    """Return True if the cache exists and is younger than CACHE_TTL seconds."""
    age = get_age_seconds()
    return age is not None and age < CACHE_TTL


def get_pull_time_str() -> str:
    """
    Return a human-readable string describing when the cache was last updated.
    Examples: "no cache", "fetched 42s ago", "fetched 3m 15s ago".
    Used in CLI output to show data freshness to the user.
    """
    age = get_age_seconds()
    if age is None:
        return "no cache"
    if age < 60:
        return f"fetched {int(age)}s ago"
    return f"fetched {int(age // 60)}m {int(age % 60)}s ago"


def load() -> Optional[dict]:
    """
    Load cached data if fresh, else return None.

    Returns None (triggering a fresh API fetch) in three cases:
      1. Cache file does not exist
      2. Cache is older than CACHE_TTL
      3. Cache file is corrupted or cannot be deserialized
    """
    if not is_fresh():
        return None
    try:
        raw = json.loads(CACHE_FILE.read_text())
        return _deserialise(raw)
    except Exception:
        return None


def save(data: dict) -> None:
    """
    Persist fetched data to cache with a fresh timestamp.

    The _meta.fetched_at timestamp is injected into the serialized payload
    so that subsequent loads can determine cache freshness without checking
    file modification time (which can be unreliable across filesystems).
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialise(data)
    payload["_meta"] = {"fetched_at": datetime.now(tz=timezone.utc).isoformat()}
    CACHE_FILE.write_text(json.dumps(payload))


def invalidate() -> None:
    """Delete the cache file, forcing the next load() to return None."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
