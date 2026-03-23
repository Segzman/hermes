"""
D2L Brightspace client for Sheridan Slate.

Covers:
  - Courses (enrolled)
  - Assignments / dropbox (including group work)
  - Quizzes
  - Discussions
  - Announcements / news
  - Grades
  - Slate messages (internal email)
  - Calendar (all event types)
  - Content / file downloads

Architecture:
  SlateClient is an async context manager that wraps httpx.AsyncClient with
  pre-loaded session cookies from auth.py. All API calls go through _get()
  or _try_get() (which swallows errors for optional endpoints).

  The D2L REST API is versioned; we use LE_VER (Learning Environment) and
  LP_VER (Learning Platform) version strings. Some Brightspace instances
  support newer API versions, so several methods try multiple version paths
  as fallbacks (e.g., quizzes tries 1.0, then 1.28).

  Submission detection uses a two-phase approach:
    1. Grade-based heuristic: if a grade exists for an item name, mark it
       as submitted (zero extra API calls, catches most cases).
    2. Explicit checks: for items not caught by grades, query the dropbox
       submission and discussion post endpoints directly.
  This avoids the N+1 query problem of checking every item individually
  while still being accurate for ungraded submissions.
"""

import asyncio
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import httpx
from dotenv import load_dotenv

from .auth import SESSION_FILE, SLATE_URL
from .models import (
    Announcement, Assignment, Attachment, Course,
    Discussion, GradeUpdate, Quiz, SlateMessage,
)

load_dotenv()

# Directory where downloaded assignment documents are saved
DOCS_DIR = Path(os.path.expanduser(os.getenv("DOCS_DIR", "~/hermes-docs")))

# D2L API version strings. "LE" = Learning Environment (course content),
# "LP" = Learning Platform (users, enrollments, messages).
LE_VER = "1.0"
LP_VER = "1.0"


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    """
    Parse a D2L date string into a timezone-aware UTC datetime.

    D2L uses several datetime formats across its API:
      - "2024-01-15T23:59:59.000Z"  (with milliseconds)
      - "2024-01-15T23:59:59Z"      (without milliseconds)
      - "2024-01-15T23:59:59"       (no timezone suffix)
    All are treated as UTC. Returns None for empty/unparseable strings.
    """
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _fmt_utc_ms(dt: datetime) -> str:
    """
    Format a datetime as a millisecond-precision UTC string for D2L calendar endpoints.

    Sheridan's Brightspace instance rejects second-precision timestamps on the
    myEvents route, so we always include ".000Z" milliseconds.
    """
    value = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _calendar_items(data: dict | list | None) -> list[dict]:
    """
    Extract calendar event items from a D2L response.

    The calendar API returns different envelope shapes depending on the
    endpoint version:
      - {"Objects": [...]}  — paginated response
      - {"Items": [...]}    — alternate envelope
      - [...]               — bare list
    This normalizes all three into a plain list.
    """
    if not data:
        return []
    if isinstance(data, dict):
        if isinstance(data.get("Objects"), list):
            return data["Objects"]
        if isinstance(data.get("Items"), list):
            return data["Items"]
    return data if isinstance(data, list) else []


def _text(val) -> str:
    """
    Extract plain text from a D2L RichText field.

    D2L represents rich text as {"Text": "plain", "Html": "<p>rich</p>"}.
    We prefer the plain text version; fall back to HTML if Text is missing.
    For simple string values, returns them directly.
    """
    if isinstance(val, dict):
        return val.get("Text", val.get("Html", ""))
    return str(val) if val else ""


def _is_relevant_course(code: str, name: str) -> bool:
    """
    Filter out non-academic course offerings.

    Sheridan's Brightspace includes "virtual community" orgs (code contains
    "_vc"), survey courses, and other administrative units that should not
    appear in the student's assignment list.
    """
    code_l = (code or "").lower()
    name_l = (name or "").lower()
    if "_vc" in code_l:
        return False
    if "virtual community" in name_l:
        return False
    if "survey" in name_l:
        return False
    return True


def _submission_records_show_user_submission(data: dict | list | None, user_id: int | None) -> bool:
    """
    Check if the D2L dropbox submission records contain a submission by the
    given user. The submission endpoint returns a list of Entity+Submissions
    pairs; we look for a non-empty Submissions list matching the user's entity ID.

    If user_id is None, any submission counts (fallback for when whoami fails).
    """
    items = data if isinstance(data, list) else (data.get("Objects", []) if isinstance(data, dict) else [])
    for item in items or []:
        entity = item.get("Entity") or {}
        try:
            entity_id = int(entity.get("EntityId"))
        except Exception:
            entity_id = None
        submissions = item.get("Submissions") or []
        # If we know the user ID, match it; otherwise accept any submission
        if user_id is not None and entity_id == user_id and submissions:
            return True
        if user_id is None and submissions:
            return True
    return False


def _discussion_posts_show_user_submission(data: dict | list | None, user_id: int | None) -> bool:
    """
    Check if the user has made at least one non-deleted post in a discussion topic.
    Returns False if user_id is unknown (cannot verify ownership without it).
    """
    items = data if isinstance(data, list) else (data.get("Objects", []) if isinstance(data, dict) else [])
    if user_id is None:
        return False
    for item in items or []:
        try:
            posting_user = int(item.get("PostingUserId"))
        except Exception:
            posting_user = None
        if posting_user == user_id and not item.get("IsDeleted", False):
            return True
    return False


class SlateClient:
    """
    Async D2L Brightspace API client. Must be used as an async context manager:

        async with SlateClient() as client:
            courses = await client.get_courses()

    Loads session cookies from the auth module's SESSION_FILE on entry.
    """

    def __init__(self):
        if not SESSION_FILE.exists():
            raise RuntimeError("No session found. Run 'python -m slate.auth' first.")
        self._client: Optional[httpx.AsyncClient] = None
        self._user_id: Optional[int] = None

    async def __aenter__(self):
        """
        Initialize the httpx client with cookies from the saved session.

        Filters cookies to only those matching the Slate hostname to avoid
        sending unrelated cookies from the browser's full storage state.
        """
        state = json.loads(SESSION_FILE.read_text())
        host = SLATE_URL.split("//")[1].split("/")[0]
        cookies = {
            c["name"]: c["value"]
            for c in state.get("cookies", [])
            if host in c.get("domain", "")
        }
        self._client = httpx.AsyncClient(
            base_url=SLATE_URL,
            cookies=cookies,
            timeout=30,
            follow_redirects=True,
            # Request JSON responses from D2L API endpoints
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    # ── internal ──────────────────────────────────────────────────────────────

    async def _get(self, path: str, **params) -> dict | list:
        """
        Make a GET request to a D2L API endpoint.
        Filters out None-valued params to keep URLs clean.
        Raises on HTTP errors (4xx/5xx).
        """
        resp = await self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    async def _try_get(self, path: str, **params) -> dict | list | None:
        """
        Like _get but returns None on any error instead of raising.
        Used for optional endpoints that may not exist on all Brightspace versions.
        """
        try:
            return await self._get(path, **params)
        except Exception:
            return None

    async def _get_user_id(self) -> Optional[int]:
        """
        Get the current user's D2L numeric identifier via the whoami endpoint.
        Cached after the first call to avoid redundant API requests.
        """
        if self._user_id is not None:
            return self._user_id
        # /d2l/api/lp/{version}/users/whoami returns {"Identifier": "12345", ...}
        data = await self._try_get(f"/d2l/api/lp/{LP_VER}/users/whoami")
        try:
            self._user_id = int((data or {}).get("Identifier"))
        except Exception:
            self._user_id = None
        return self._user_id

    async def _assignment_has_submission(self, course_id: str, folder_id: str) -> bool:
        """
        Check whether the current user has uploaded a submission to a specific
        dropbox folder. Uses the submissions endpoint which lists all submissions
        for the folder.
        """
        data = await self._try_get(f"/d2l/api/le/{LE_VER}/{course_id}/dropbox/folders/{folder_id}/submissions/")
        if not data:
            return False
        return _submission_records_show_user_submission(data, await self._get_user_id())

    async def _discussion_has_submission(self, course_id: str, forum_id: str, topic_id: str) -> bool:
        """
        Check whether the current user has posted in a specific discussion topic.
        Queries the posts endpoint for the given forum/topic combination.
        """
        data = await self._try_get(
            f"/d2l/api/le/{LE_VER}/{course_id}/discussions/forums/{forum_id}/topics/{topic_id}/posts/"
        )
        if not data:
            return False
        return _discussion_posts_show_user_submission(data, await self._get_user_id())

    # ── courses ───────────────────────────────────────────────────────────────

    async def get_courses(self) -> list[Course]:
        """
        Get only active, current-semester course offerings.

        Uses orgUnitTypeId=3 (course offerings) to exclude organizational units
        like departments and semesters. Filters out:
          - Courses with empty or "root" codes
          - Virtual community / survey courses (via _is_relevant_course)
          - Courses whose access period has already ended
        """
        now = datetime.now(tz=timezone.utc)
        data = await self._get(
            f"/d2l/api/lp/{LP_VER}/enrollments/myenrollments/",
            pageSize=200, isActive="true", orgUnitTypeId=3,
        )
        courses = []
        for item in data.get("Items", []):
            org = item.get("OrgUnit", {})
            oid = org.get("Id")
            if not oid:
                continue
            code = org.get("Code", "")
            name = org.get("Name", "")
            # Skip placeholder and root-level org units
            if not code or code.lower() in ("", "root"):
                continue
            if not _is_relevant_course(code, name):
                continue
            # Skip courses whose access period has ended
            access = item.get("Access", {})
            end_str = access.get("EndDate")
            if end_str:
                end_date = _parse_date(end_str)
                if end_date and end_date < now:
                    continue
            courses.append(Course(
                id=str(oid),
                name=name,
                code=code,
                url=f"{SLATE_URL}/d2l/home/{oid}",
            ))
        return courses

    # ── assignments (dropbox) ─────────────────────────────────────────────────

    async def get_assignments(self, course: Course) -> list[Assignment]:
        """
        Fetch all dropbox folders for a course.

        Submission status is NOT checked here (would require N extra API calls).
        Instead, is_submitted defaults to False and is resolved later in
        get_everything() via grade data and explicit submission checks.

        Group assignments are detected by checking if "group" appears in the
        category name or folder name.
        """
        data = await self._try_get(f"/d2l/api/le/{LE_VER}/{course.id}/dropbox/folders/", pageSize=200)
        if not data:
            return []
        # Handle both paginated {"Objects": [...]} and bare list responses
        items = data.get("Objects", data) if isinstance(data, dict) else data
        results = []
        for folder in items:
            fid = str(folder.get("Id", ""))
            # Build download URLs for each attachment in the dropbox folder
            attachments = [
                Attachment(
                    name=a.get("FileName", a.get("Name", "")),
                    url=f"{SLATE_URL}/d2l/api/le/{LE_VER}/{course.id}/dropbox/folders/{fid}/attachments/{a.get('FileId', '')}",
                )
                for a in folder.get("Attachments", [])
            ]
            # Detect group assignments by checking category and folder name
            category = folder.get("CategoryId") or folder.get("CategoryName", "")
            is_group = "group" in str(category).lower() or "group" in folder.get("Name", "").lower()
            results.append(Assignment(
                id=fid,
                name=folder.get("Name", "Untitled"),
                course=course,
                due_date=_parse_date(folder.get("DueDate") or folder.get("EndDate")),
                instructions=_text(folder.get("Instructions")),
                attachments=attachments,
                is_submitted=False,  # resolved later via grade data
                kind="group" if is_group else "assignment",
            ))
        return results

    # ── quizzes ───────────────────────────────────────────────────────────────

    async def get_quizzes(self, course: Course) -> list[Quiz]:
        """
        Fetch quizzes for a course. Tries API version 1.0 first, then falls
        back to 1.28 for newer Brightspace instances. No per-quiz attempt
        check is done here; submission status is resolved in get_everything().
        """
        data = await self._try_get(f"/d2l/api/le/{LE_VER}/{course.id}/quizzes/", pageSize=200) or \
               await self._try_get(f"/d2l/api/le/1.28/{course.id}/quizzes/", pageSize=200)
        if not data:
            return []
        items = data.get("Objects", data) if isinstance(data, dict) else data
        results = []
        for q in items:
            qid = str(q.get("QuizId", q.get("Id", "")))
            results.append(Quiz(
                id=qid,
                name=q.get("Name", "Untitled Quiz"),
                course=course,
                due_date=_parse_date(q.get("DueDate")),
                start_date=_parse_date(q.get("StartDate")),
                end_date=_parse_date(q.get("EndDate")),
                # TimeLimit is an object with IsEnforced (bool) and Time (minutes)
                time_limit_minutes=q.get("TimeLimit", {}).get("IsEnforced") and q.get("TimeLimit", {}).get("Time"),
                # NumberOfAttemptsAllowed can be 0 (unlimited) — default to 1
                attempts_allowed=q.get("AttemptsAllowed", {}).get("NumberOfAttemptsAllowed", 1) or 1,
                is_submitted=False,  # resolved later via grade data
            ))
        return results

    # ── discussions ───────────────────────────────────────────────────────────

    async def get_discussions(self, course: Course) -> list[Discussion]:
        """
        Fetch discussion topics for a course.

        D2L organizes discussions as forums -> topics. We fetch all forums first,
        then concurrently fetch topics for each forum using asyncio.gather.
        Topic IDs are stored as "{forum_id}_{topic_id}" to enable later lookups
        when checking for user posts.
        """
        data = await self._try_get(f"/d2l/api/le/{LE_VER}/{course.id}/discussions/forums/", pageSize=200)
        if not data:
            return []
        items = data.get("Objects", data) if isinstance(data, dict) else data
        results = []

        async def _topics_for_forum(forum):
            """Fetch all topics within a single forum."""
            fid = str(forum.get("ForumId", forum.get("Id", "")))
            topics_data = await self._try_get(
                f"/d2l/api/le/{LE_VER}/{course.id}/discussions/forums/{fid}/topics/", pageSize=200
            )
            topics = topics_data.get("Objects", topics_data) if isinstance(topics_data, dict) else (topics_data or [])
            return fid, forum, topics

        # Fetch topics for all forums concurrently to minimize total latency
        forum_results = await asyncio.gather(*[_topics_for_forum(f) for f in items], return_exceptions=True)
        for r in forum_results:
            if not isinstance(r, tuple):
                continue  # Skip exceptions from individual forum fetches
            fid, forum, topics = r
            for topic in topics:
                tid = str(topic.get("TopicId", topic.get("Id", "")))
                results.append(Discussion(
                    # Compound ID enables splitting back into forum_id + topic_id
                    # for the discussion submission check in get_everything()
                    id=f"{fid}_{tid}",
                    name=topic.get("Name", forum.get("Name", "Discussion")),
                    course=course,
                    due_date=_parse_date(topic.get("EndDate") or topic.get("DueDate")),
                    description=_text(topic.get("Description") or forum.get("Description", "")),
                    is_submitted=False,  # resolved later via grade data
                ))
        return results

    # ── announcements ─────────────────────────────────────────────────────────

    async def get_announcements(self, course: Course) -> list[Announcement]:
        """
        Fetch news/announcement items for a course.
        The is_new flag is the inverse of D2L's IsRead field.
        """
        data = await self._try_get(f"/d2l/api/le/{LE_VER}/{course.id}/news/", pageSize=50)
        if not data:
            return []
        items = data.get("Objects", data) if isinstance(data, dict) else data
        results = []
        for item in items:
            results.append(Announcement(
                id=str(item.get("Id", "")),
                title=item.get("Title", "Announcement"),
                course=course,
                body=_text(item.get("Body")),
                # D2L uses "StartDate" for when the announcement was published
                posted_at=_parse_date(item.get("StartDate") or item.get("PublishedAt")),
                is_new=not item.get("IsRead", False),
            ))
        return results

    # ── grades ────────────────────────────────────────────────────────────────

    async def get_grade_updates(self, course: Course) -> list[GradeUpdate]:
        """
        Fetch the student's grade values for a course.

        Only includes items where both numerator and denominator are present
        (skips ungraded or text-only grade items). The grade data is also used
        as a heuristic in get_everything() to mark deliverables as submitted.
        """
        data = await self._try_get(f"/d2l/api/le/{LE_VER}/{course.id}/grades/values/myGradeValues/")
        if not data:
            return []
        items = data.get("Objects", data) if isinstance(data, dict) else data
        results = []
        for item in items:
            pts = item.get("PointsNumerator")
            denom = item.get("PointsDenominator")
            # Skip grade items without numeric scores (e.g., pass/fail, text feedback)
            if pts is None or not denom:
                continue
            results.append(GradeUpdate(
                course=course,
                item_name=item.get("GradeObjectName", "Grade Item"),
                score=float(pts),
                total=float(denom),
                graded_at=_parse_date(item.get("LastModified")),
            ))
        return results

    # ── Slate messages (internal mail) ────────────────────────────────────────

    async def get_messages(self, unread_only: bool = False) -> list[SlateMessage]:
        """
        Fetch messages from Brightspace's internal messaging system.

        The messages API path varies across Brightspace versions, so we try
        multiple version prefixes (1.0, 1.28, 1.46) until one succeeds.
        """
        # Try multiple known message API paths for different Brightspace versions
        data = None
        for path in [
            f"/d2l/api/lp/{LP_VER}/messages/inbox/",
            f"/d2l/api/lp/1.28/messages/inbox/",
            f"/d2l/api/lp/1.46/messages/inbox/",
        ]:
            data = await self._try_get(path, pageSize=50)
            if data:
                break
        if not data:
            return []
        items = data.get("Objects", data) if isinstance(data, dict) else data
        results = []
        for msg in items:
            is_read = msg.get("IsRead", True)
            if unread_only and is_read:
                continue
            results.append(SlateMessage(
                id=str(msg.get("MessageId", msg.get("Id", ""))),
                subject=msg.get("Subject", "(no subject)"),
                sender_name=msg.get("SenderName", ""),
                sender_email=msg.get("SenderEmail", ""),
                body=_text(msg.get("Body", "")),
                sent_at=_parse_date(msg.get("SentDate")),
                is_read=is_read,
            ))
        return results

    # ── calendar ──────────────────────────────────────────────────────────────

    async def get_calendar_events(self, course: Optional[Course] = None) -> list[dict]:
        """
        Returns raw calendar events from D2L — covers past 60 days + next 90 days
        so overdue and upcoming items both appear.

        Sheridan's Brightspace instance rejects second-precision timestamps on the
        `myEvents` route, so the window must be sent with millisecond UTC strings
        (handled by _fmt_utc_ms).

        If no course is specified, fetches events for ALL enrolled courses
        concurrently. Calendar events are returned as raw dicts (not model
        objects) because they serve as supplementary data for the merge_calendar
        logic in slate_cli.py.
        """
        from datetime import timedelta
        now = datetime.now(tz=timezone.utc)
        # Wide window: 60 days back (catch overdue) + 90 days forward (catch future)
        start = now - timedelta(days=60)
        end = now + timedelta(days=90)
        params = dict(
            startDateTime=_fmt_utc_ms(start),
            endDateTime=_fmt_utc_ms(end),
        )

        if course:
            # Try the "myEvents" endpoint first (user-specific), falling back
            # to older API versions if the endpoint is not available
            for path in [
                f"/d2l/api/le/1.46/{course.id}/calendar/events/myEvents/",
                f"/d2l/api/le/1.28/{course.id}/calendar/events/myEvents/",
                f"/d2l/api/le/{LE_VER}/{course.id}/calendar/events/myEvents/",
            ]:
                items = _calendar_items(await self._try_get(path, **params))
                if items:
                    return items

            # Fallback: fetch the full course calendar (not user-specific) and
            # manually filter by time range since this endpoint may not accept
            # date parameters.
            for path in [
                f"/d2l/api/le/1.46/{course.id}/calendar/events/",
                f"/d2l/api/le/1.28/{course.id}/calendar/events/",
                f"/d2l/api/le/{LE_VER}/{course.id}/calendar/events/",
            ]:
                items = _calendar_items(await self._try_get(path))
                if not items:
                    continue
                filtered = []
                for item in items:
                    when = _parse_date(item.get("EndDateTime") or item.get("StartDateTime"))
                    # Include events within our time window, or those with no date
                    if when is None or start <= when <= end:
                        filtered.append(item)
                return filtered
            return []

        # No specific course — fetch calendar events for all enrolled courses
        courses = await self.get_courses()
        results = await asyncio.gather(
            *[self.get_calendar_events(course) for course in courses],
            return_exceptions=True,
        )
        events: list[dict] = []
        for result in results:
            if isinstance(result, list):
                events.extend(result)
        return events

    # ── combined fetch ────────────────────────────────────────────────────────

    async def get_everything(self) -> dict:
        """
        Fetch all content in parallel. This is the main entry point used by
        both the CLI (slate_cli.py) and the checker (checker.py).

        Execution strategy:
          1. Fetch all enrolled courses
          2. For each course, concurrently fetch: assignments, quizzes,
             discussions, announcements, grades, and calendar events
          3. Also fetch messages (not course-specific) in parallel
          4. Use grade names to mark graded items as submitted (zero extra
             API calls — covers most cases)
          5. For remaining unsubmitted items, explicitly check dropbox
             submissions and discussion posts (parallel batch)

        The two-phase submission detection minimizes API calls while still
        catching ungraded work the user has already submitted.
        """
        courses = await self.get_courses()

        async def _for_course(course: Course):
            """Fetch all data types for a single course concurrently."""
            a, q, d, ann, g, cal = await asyncio.gather(
                self.get_assignments(course),
                self.get_quizzes(course),
                self.get_discussions(course),
                self.get_announcements(course),
                self.get_grade_updates(course),
                self.get_calendar_events(course),
                return_exceptions=True,
            )
            # Replace exceptions with empty lists to avoid crashing the whole fetch
            return (
                a if isinstance(a, list) else [],
                q if isinstance(q, list) else [],
                d if isinstance(d, list) else [],
                ann if isinstance(ann, list) else [],
                g if isinstance(g, list) else [],
                cal if isinstance(cal, list) else [],
            )

        # Fetch all courses + messages concurrently
        course_results, messages = await asyncio.gather(
            asyncio.gather(*[_for_course(c) for c in courses], return_exceptions=True),
            self.get_messages(),
            return_exceptions=True,
        )

        # Flatten per-course results into combined lists
        assignments, quizzes, discussions, announcements, grades, calendar = [], [], [], [], [], []
        for r in (course_results if isinstance(course_results, list) else []):
            if isinstance(r, tuple):
                a, q, d, ann, g, cal = r
                assignments.extend(a); quizzes.extend(q); discussions.extend(d)
                announcements.extend(ann); grades.extend(g)
                calendar.extend(cal)

        # ── Phase 1: Grade-based submission detection ──
        # Build a set of (course_id, lowercase_item_name) from grade data.
        # If a deliverable's name matches a graded item, mark it as submitted.
        # This is a heuristic — it works because D2L grade item names usually
        # match the assignment/quiz name exactly.
        graded_keys: set[tuple] = set()
        for g in grades:
            graded_keys.add((g.course.id, g.item_name.lower().strip()))

        def _mark_submitted(items):
            """Mark items as submitted if they have a matching grade entry."""
            for item in items:
                key = (item.course.id, item.name.lower().strip())
                if key in graded_keys:
                    item.is_submitted = True
            return items

        _mark_submitted(assignments)
        _mark_submitted(quizzes)

        # ── Phase 2: Explicit submission checks ──
        # For items not caught by the grade heuristic, query the actual
        # submission/post endpoints. These are batched and run in parallel.
        assignment_targets = [item for item in assignments if not item.is_submitted]
        assignment_checks = [
            self._assignment_has_submission(item.course.id, item.id)
            for item in assignment_targets
        ]
        discussion_targets = []
        discussion_checks = []
        for item in discussions:
            if item.is_submitted or "_" not in item.id:
                continue
            # Split the compound ID back into forum_id and topic_id
            forum_id, topic_id = item.id.split("_", 1)
            discussion_targets.append(item)
            discussion_checks.append(self._discussion_has_submission(item.course.id, forum_id, topic_id))

        # Run all submission checks concurrently
        assignment_results, discussion_results = await asyncio.gather(
            asyncio.gather(*assignment_checks, return_exceptions=True),
            asyncio.gather(*discussion_checks, return_exceptions=True),
        )

        # Apply the results — only mark as submitted on True (not on exceptions)
        for item, result in zip(assignment_targets, assignment_results):
            if result is True:
                item.is_submitted = True

        for item, result in zip(discussion_targets, discussion_results):
            if result is True:
                item.is_submitted = True

        return dict(
            courses=courses,
            assignments=assignments,
            quizzes=quizzes,
            discussions=discussions,
            announcements=announcements,
            grades=grades,
            messages=messages if isinstance(messages, list) else [],
            calendar_events=calendar if isinstance(calendar, list) else [],
        )

    # ── document download ─────────────────────────────────────────────────────

    async def download_attachment(self, attachment: Attachment, dest_dir: Path) -> Path:
        """
        Download a single attachment file to the specified directory.
        Sanitizes the filename to remove special characters that could cause
        filesystem issues.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Replace non-alphanumeric characters (except ._- and space) with underscores
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in attachment.name)
        dest = dest_dir / safe_name
        resp = await self._client.get(attachment.url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

    async def download_assignment_docs(self, assignment: Assignment) -> Path:
        """
        Download all attachments for an assignment and bundle them into a zip file.

        Files are saved to DOCS_DIR/{course_code}/{assignment_name}/, and the
        zip is created alongside the folder. Returns the path to the zip file
        (or the directory if no files were downloaded).
        """
        safe_course = assignment.course.code.replace("/", "_")
        safe_name = assignment.name.replace("/", "_")[:50]  # Truncate long names
        dest_dir = DOCS_DIR / safe_course / safe_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        downloaded: list[Path] = []
        for att in assignment.attachments:
            try:
                path = await self.download_attachment(att, dest_dir)
                downloaded.append(path)
                print(f"  Downloaded: {att.name}")
            except Exception as e:
                print(f"  Warning: could not download {att.name}: {e}")

        if not downloaded:
            return dest_dir

        # Create a zip containing all downloaded files for easy transfer
        zip_path = dest_dir.parent / f"{safe_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in downloaded:
                zf.write(f, f.name)
        return zip_path

    async def get_assignment_context(self, assignment: Assignment) -> str:
        """
        Build a markdown-formatted context string for an assignment.
        Used by the --plan command to generate an LLM-ready prompt with all
        relevant assignment details.
        """
        lines = [
            f"# {assignment.name}",
            f"Course: {assignment.course.name} ({assignment.course.code})",
            f"Type: {assignment.kind}",
            f"Status: {assignment.due_str()}",
            "",
            "## Instructions",
            assignment.instructions or "(No instructions provided)",
        ]
        if assignment.attachments:
            lines += ["", "## Attachments"]
            for a in assignment.attachments:
                lines.append(f"- {a.name}")
        return "\n".join(lines)
