"""
iCloud Apple Reminders and Calendar integration via CalDAV.

This module provides CRUD operations for Apple Reminders (VTODO) and Apple
Calendar events (VEVENT) through the CalDAV protocol against iCloud's server
at caldav.icloud.com.

Architecture notes:
  - Authentication uses an Apple ID and an app-specific password (not the
    main Apple ID password). These are configured via APPLE_ID and
    APPLE_APP_PASSWORD environment variables.
  - iCloud exposes separate CalDAV calendars for reminders (supporting
    VTODO components) and calendar events (supporting VEVENT components).
    The user can configure preferred list/calendar names via env vars.
  - ICS (iCalendar) data is built manually as strings rather than using
    a library like icalendar, because the CalDAV library's save_todo/
    save_event methods accept raw ICS text and this keeps dependencies
    minimal.
  - Subtasks are simulated by creating separate VTODO items with a naming
    convention ("Parent Title - Subtask Label"). CalDAV does not natively
    support subtask hierarchies in a cross-client way.
  - Matching entries by UID, URL, exact title, or partial title enables
    flexible update/delete operations from the LLM agent.

ICS format reference:
  - VTODO: RFC 5545 Section 3.6.2
  - VEVENT: RFC 5545 Section 3.6.1
  - VALARM: RFC 5545 Section 3.6.6 (used for alerts/notifications)
  - TRIGGER:-PTnM means "n minutes before" in alarm definitions
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# iCloud credentials — APPLE_APP_PASSWORD must be an app-specific password
# generated at appleid.apple.com, not the main Apple ID password.
APPLE_ID = os.getenv("APPLE_ID", "")
APPLE_APP_PASSWORD = os.getenv("APPLE_APP_PASSWORD", "")
# Optional: preferred CalDAV calendar/list names. If empty, the first
# matching calendar for the component type (VTODO or VEVENT) is used.
APPLE_REMINDERS_NAME = os.getenv("APPLE_REMINDERS_NAME", "")
APPLE_CALENDAR_NAME = os.getenv("APPLE_CALENDAR_NAME", "")
# All datetime operations use Toronto local time for display.
LOCAL_TZ = ZoneInfo("America/Toronto")


@dataclass
class AppleCalendarEvent:
    """Parsed representation of an iCloud VEVENT calendar event."""
    title: str
    start_at: datetime
    end_at: Optional[datetime]
    calendar_name: str
    location: str = ""
    url: str = ""
    uid: str = ""
    notes: str = ""
    alert_minutes_before: int = 0


@dataclass
class AppleReminderItem:
    """
    Parsed representation of an iCloud VTODO reminder.

    priority uses iCalendar numeric values: 1=high, 5=medium, 9=low, 0=none.
    people stores attendee email addresses or plain names.
    """
    title: str
    due: Optional[datetime]
    calendar_name: str
    uid: str = ""
    notes: str = ""
    location: str = ""
    priority: int = 0
    completed: bool = False
    url: str = ""
    people: tuple[str, ...] = ()
    alert_minutes_before: int = 0


def _require_config() -> None:
    """Raise if iCloud credentials are missing."""
    if not APPLE_ID or not APPLE_APP_PASSWORD:
        raise RuntimeError(
            "Apple iCloud is not configured. Set APPLE_ID and APPLE_APP_PASSWORD in .env."
        )


def _load_caldav():
    """
    Lazily import the caldav library.

    This is deferred because caldav is a heavy dependency and not all
    Hermes deployments need Apple integration.
    """
    try:
        import caldav
    except ImportError as exc:
        raise RuntimeError("Python package 'caldav' is not installed in this environment.") from exc
    return caldav


def _escape_ics(text: str) -> str:
    """
    Escape a string for embedding in an ICS property value.

    Per RFC 5545 Section 3.3.11, backslash, newlines, commas, and
    semicolons must be escaped in TEXT-type property values.
    """
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _unescape_ics(text: str) -> str:
    """Reverse the ICS escaping applied by _escape_ics."""
    return (
        (text or "")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware (default to UTC if naive)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp(dt: datetime) -> str:
    """
    Format a datetime as an ICS UTC timestamp string: YYYYMMDDTHHMMSSZ.

    All times are stored in UTC in the ICS data to avoid timezone ambiguity.
    """
    return _utc(dt).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _calendar_name(calendar) -> str:
    """Extract the human-readable name from a CalDAV calendar object."""
    return getattr(calendar, "name", "") or "Apple Calendar"


def _principal():
    """
    Authenticate to iCloud CalDAV and return the principal object.

    The principal is the entry point for discovering calendars and
    performing operations.
    """
    _require_config()
    caldav = _load_caldav()
    client = caldav.DAVClient(
        url="https://caldav.icloud.com",
        username=APPLE_ID,
        password=APPLE_APP_PASSWORD,
    )
    return client.principal()


def _find_calendar(principal, component: str, preferred_name: str = ""):
    """
    Find a CalDAV calendar that supports the given component type.

    Args:
        principal: CalDAV principal object from _principal()
        component: ICS component type, e.g. "VTODO" for reminders or
                   "VEVENT" for calendar events
        preferred_name: optional calendar/list name to match (case-insensitive)

    If a preferred name is given and no calendar matches, raises RuntimeError
    with a descriptive message. Otherwise returns the first matching calendar.
    """
    preferred = (preferred_name or "").strip().lower()
    matches = []
    for calendar in principal.calendars():
        supported = set(calendar.get_supported_components() or [])
        if component not in supported:
            continue
        matches.append(calendar)
        name = _calendar_name(calendar).strip().lower()
        if preferred and name == preferred:
            return calendar
    if preferred:
        label = "Reminders" if component == "VTODO" else "Calendar"
        raise RuntimeError(
            f'Apple {label} list "{preferred_name}" is not currently exposed by iCloud CalDAV.'
        )
    if matches:
        return matches[0]
    label = "Reminders" if component == "VTODO" else "Calendar"
    raise RuntimeError(f"Could not find an Apple {label} calendar for {component}.")


def _priority_value(priority: str = "", urgent: bool = False) -> int:
    """
    Convert a human-readable priority string to an iCalendar PRIORITY integer.

    iCalendar PRIORITY values: 1-4 = high, 5 = medium, 6-9 = low, 0 = undefined.
    We use 1 for high/urgent, 5 for medium, 9 for low.
    """
    if urgent:
        return 1
    value = (priority or "").strip().lower()
    return {"high": 1, "medium": 5, "low": 9}.get(value, 0)


def _priority_label(value: int) -> str:
    """Convert an iCalendar PRIORITY integer back to a human-readable string."""
    return {1: "high", 5: "medium", 9: "low"}.get(int(value or 0), "")


def _todo_uid(title: str, due: Optional[datetime], notes: str) -> str:
    """Generate a unique UID for a new VTODO item."""
    return f"hermes-reminder-{uuid.uuid4()}"


def _event_uid(title: str, start_at: datetime) -> str:
    """Generate a unique UID for a new VEVENT item."""
    return f"hermes-event-{uuid.uuid4()}"


def _attendee_lines(people: list[str]) -> tuple[str, str]:
    """
    Build ICS ATTENDEE lines for people associated with a reminder.

    Email addresses become proper ATTENDEE:mailto: lines. Plain names
    (without @) are collected into a text note since CalDAV ATTENDEE
    requires an email URI.

    Returns:
        Tuple of (ics_attendee_lines_string, extra_notes_for_description)
    """
    attendee_lines = []
    plain_people = []
    for person in people:
        person = (person or "").strip()
        if not person:
            continue
        if "@" in person:
            attendee_lines.append(f"ATTENDEE:{_escape_ics('mailto:' + person)}\n")
        else:
            plain_people.append(person)
    extra_notes = ""
    if plain_people:
        extra_notes = f"People: {', '.join(plain_people)}"
    return "".join(attendee_lines), extra_notes


def _make_vtodo(
    title: str,
    due: Optional[datetime],
    notes: str,
    alert_minutes_before: int = 60,
    priority: str = "",
    urgent: bool = False,
    location: str = "",
    people: Optional[list[str]] = None,
    uid: str = "",
    status: str = "NEEDS-ACTION",
    completed_at: Optional[datetime] = None,
) -> str:
    """
    Build a complete ICS VCALENDAR string containing a VTODO component.

    This constructs the raw ICS text that the CalDAV library will PUT to
    iCloud. Each property is conditionally included only when it has a value.

    The optional VALARM block creates a notification alert before the due time.
    """
    uid = uid or _todo_uid(title, due, notes)
    due_line = f"DUE:{_stamp(due)}\n" if due else ""
    priority_value = _priority_value(priority=priority, urgent=urgent)
    priority_line = f"PRIORITY:{priority_value}\n" if priority_value else ""
    location_line = f"LOCATION:{_escape_ics(location)}\n" if location else ""
    attendee_lines, people_note = _attendee_lines(people or [])
    status_value = (status or "NEEDS-ACTION").strip().upper()
    completed_line = ""
    if status_value == "COMPLETED":
        completed_line = f"COMPLETED:{_stamp(completed_at or datetime.now(tz=timezone.utc))}\n"
    full_notes = notes or ""
    if people_note:
        full_notes = f"{full_notes}\n{people_note}".strip()
    # VALARM triggers a notification on the device at the specified time before due
    alarm = ""
    if due and alert_minutes_before > 0:
        alarm = (
            "BEGIN:VALARM\n"
            f"TRIGGER:-PT{int(alert_minutes_before)}M\n"
            "ACTION:DISPLAY\n"
            f"DESCRIPTION:{_escape_ics(title)}\n"
            "END:VALARM\n"
        )
    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Hermes//EN\n"
        "BEGIN:VTODO\n"
        f"UID:{uid}\n"
        f"DTSTAMP:{_stamp(datetime.now(tz=timezone.utc))}\n"
        f"SUMMARY:{_escape_ics(title)}\n"
        f"{due_line}"
        f"DESCRIPTION:{_escape_ics(full_notes)}\n"
        f"{location_line}"
        f"{priority_line}"
        f"{attendee_lines}"
        f"STATUS:{status_value}\n"
        f"{completed_line}"
        f"{alarm}"
        "END:VTODO\n"
        "END:VCALENDAR\n"
    )


def _make_vevent(
    title: str,
    start_at: datetime,
    end_at: datetime,
    notes: str = "",
    location: str = "",
    alert_minutes_before: int = 30,
    uid: str = "",
) -> str:
    """
    Build a complete ICS VCALENDAR string containing a VEVENT component.

    Similar to _make_vtodo but for calendar events with DTSTART/DTEND.
    """
    uid = uid or _event_uid(title, start_at)
    alarm = ""
    if alert_minutes_before > 0:
        alarm = (
            "BEGIN:VALARM\n"
            f"TRIGGER:-PT{int(alert_minutes_before)}M\n"
            "ACTION:DISPLAY\n"
            f"DESCRIPTION:{_escape_ics(title)}\n"
            "END:VALARM\n"
        )
    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Hermes//EN\n"
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"DTSTAMP:{_stamp(datetime.now(tz=timezone.utc))}\n"
        f"DTSTART:{_stamp(start_at)}\n"
        f"DTEND:{_stamp(end_at)}\n"
        f"SUMMARY:{_escape_ics(title)}\n"
        f"DESCRIPTION:{_escape_ics(notes)}\n"
        f"LOCATION:{_escape_ics(location)}\n"
        f"{alarm}"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )


def _ics_lines(data: str) -> list[str]:
    """
    Unfold ICS long lines per RFC 5545 Section 3.1.

    ICS uses "line folding" where long lines are broken with a CRLF followed
    by a single whitespace character. This function reassembles them into
    logical lines for easier parsing.
    """
    unfolded: list[str] = []
    for raw_line in (data or "").splitlines():
        # Lines starting with space or tab are continuations of the previous line
        if raw_line[:1] in {" ", "\t"} and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    return unfolded


def _extract_ics_field(data: str, field: str) -> str:
    """
    Extract the first value for an ICS property from raw ICS text.

    Handles both simple "FIELD:value" and parameterised "FIELD;PARAM=x:value"
    forms. Returns empty string if the field is not found.
    """
    prefix = f"{field}:"
    for raw_line in _ics_lines(data):
        line = raw_line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
        # Handle parameterised form like "DTSTART;VALUE=DATE:20260325"
        if line.startswith(f"{field};"):
            return line.split(":", 1)[1].strip() if ":" in line else ""
    return ""


def _extract_ics_fields(data: str, field: str) -> list[str]:
    """
    Extract all values for a repeating ICS property (e.g. ATTENDEE).

    Unlike _extract_ics_field which returns the first match, this returns
    all matching values as a list.
    """
    values = []
    prefix = f"{field}:"
    for raw_line in _ics_lines(data):
        line = raw_line.strip()
        if line.startswith(prefix):
            values.append(line[len(prefix):].strip())
        elif line.startswith(f"{field};") and ":" in line:
            values.append(line.split(":", 1)[1].strip())
    return values


def _parse_ics_datetime(value: str) -> Optional[datetime]:
    """
    Parse an ICS datetime string into a Python datetime.

    Supports three formats:
      - "20260325T143000Z"  (UTC, with trailing Z)
      - "20260325T143000"   (local time, assumed Toronto)
      - "20260325"          (date only, assumed Toronto midnight)
    """
    if not value:
        return None
    value = value.strip()
    formats = ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S")
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            if value.endswith("Z"):
                return parsed.replace(tzinfo=timezone.utc)
            # No Z suffix: assume Toronto local time
            return parsed.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    # Try date-only format (8 digits)
    if len(value) == 8:
        try:
            parsed = datetime.strptime(value, "%Y%m%d")
            return parsed.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _parse_ics_int(value: str) -> int:
    """Safely parse an ICS integer value, defaulting to 0."""
    try:
        return int((value or "").strip() or "0")
    except ValueError:
        return 0


def _parse_alarm_minutes(data: str) -> int:
    """
    Extract the alarm trigger duration from ICS data.

    Looks for TRIGGER:-PTnM patterns in VALARM blocks.
    The "-PT" prefix means "before", and "M" means minutes.
    """
    for value in _extract_ics_fields(data, "TRIGGER"):
        text = value.strip().upper()
        # "-PT30M" means 30 minutes before the event/due time
        if text.startswith("-PT") and text.endswith("M"):
            try:
                return int(text[3:-1])
            except ValueError:
                continue
    return 0


def _parse_attendees(data: str) -> tuple[str, ...]:
    """
    Extract attendee email addresses from ICS ATTENDEE properties.

    Strips the "mailto:" URI prefix to return plain email addresses.
    """
    people = []
    for value in _extract_ics_fields(data, "ATTENDEE"):
        text = value.strip()
        if text.lower().startswith("mailto:"):
            text = text[7:]
        if text:
            people.append(text)
    return tuple(people)


def _resource_data(resource) -> str:
    """
    Extract raw ICS text from a CalDAV resource object.

    Different versions of the caldav library expose the ICS data through
    different attributes (.data, ._data, or via a .load() method), so we
    try multiple access patterns as a compatibility workaround.
    """
    data = getattr(resource, "data", "") or getattr(resource, "_data", "")
    if callable(data):
        data = data()
    if data:
        return data
    # Some resources require an explicit load before data is available
    loader = getattr(resource, "load", None)
    if callable(loader):
        loader()
    data = getattr(resource, "data", "") or getattr(resource, "_data", "")
    if callable(data):
        data = data()
    return data or ""


def _iter_component_resources(calendar, component: str):
    """
    List all resources of a given component type from a CalDAV calendar.

    Uses the appropriate CalDAV method (.todos() for VTODO, .events() for
    VEVENT) with fallback to the generic .objects() method.
    """
    resources = None
    if component == "VTODO" and hasattr(calendar, "todos"):
        try:
            resources = calendar.todos(include_completed=True)
        except TypeError:
            # Older caldav versions may not support include_completed kwarg
            resources = calendar.todos()
    elif component == "VEVENT" and hasattr(calendar, "events"):
        resources = calendar.events()
    elif hasattr(calendar, "objects"):
        resources = calendar.objects(load_objects=True)
    return list(resources or [])


def _parse_reminder_resource(resource, calendar_name: str) -> AppleReminderItem:
    """Parse a CalDAV VTODO resource into an AppleReminderItem dataclass."""
    data = _resource_data(resource)
    status = (_extract_ics_field(data, "STATUS") or "NEEDS-ACTION").upper()
    return AppleReminderItem(
        title=_unescape_ics(_extract_ics_field(data, "SUMMARY") or "Untitled reminder"),
        due=_parse_ics_datetime(_extract_ics_field(data, "DUE")),
        calendar_name=calendar_name,
        uid=_extract_ics_field(data, "UID"),
        notes=_unescape_ics(_extract_ics_field(data, "DESCRIPTION")),
        location=_unescape_ics(_extract_ics_field(data, "LOCATION")),
        priority=_parse_ics_int(_extract_ics_field(data, "PRIORITY")),
        completed=status == "COMPLETED",
        url=str(getattr(resource, "url", "") or ""),
        people=_parse_attendees(data),
        alert_minutes_before=_parse_alarm_minutes(data),
    )


def _parse_event_resource(resource, calendar_name: str) -> Optional[AppleCalendarEvent]:
    """
    Parse a CalDAV VEVENT resource into an AppleCalendarEvent dataclass.

    Returns None if the event has no parseable start time (which would make
    it unusable for display).
    """
    data = _resource_data(resource)
    start_at = _parse_ics_datetime(_extract_ics_field(data, "DTSTART"))
    if not start_at:
        return None
    return AppleCalendarEvent(
        title=_unescape_ics(_extract_ics_field(data, "SUMMARY") or "Untitled event"),
        start_at=start_at,
        end_at=_parse_ics_datetime(_extract_ics_field(data, "DTEND")),
        calendar_name=calendar_name,
        location=_unescape_ics(_extract_ics_field(data, "LOCATION")),
        url=str(getattr(resource, "url", "") or ""),
        uid=_extract_ics_field(data, "UID"),
        notes=_unescape_ics(_extract_ics_field(data, "DESCRIPTION")),
        alert_minutes_before=_parse_alarm_minutes(data),
    )


def _match_entry(ref: str, entries: list[tuple[object, object]], label: str):
    """
    Find a single entry matching a user-provided reference string.

    Matching is attempted in order of specificity:
      1. Exact UID match
      2. Exact URL match
      3. Exact title match (case-insensitive)
      4. Partial title match (case-insensitive substring)

    Raises RuntimeError if no match is found or if multiple entries match
    (to prevent accidental edits/deletes of the wrong item).
    """
    needle = (ref or "").strip().lower()
    if not needle:
        raise RuntimeError(f"A {label} reference is required.")

    def _bucket(predicate):
        return [(resource, item) for resource, item in entries if predicate(item)]

    matches = _bucket(lambda item: getattr(item, "uid", "").lower() == needle)
    if not matches:
        matches = _bucket(lambda item: getattr(item, "url", "").lower() == needle)
    if not matches:
        matches = _bucket(lambda item: getattr(item, "title", "").strip().lower() == needle)
    if not matches:
        matches = _bucket(lambda item: needle in getattr(item, "title", "").lower())
    if not matches:
        raise RuntimeError(f'No Apple {label} matched "{ref}".')
    if len(matches) > 1:
        options = ", ".join(
            f'{item.title} [{(item.uid or "?")[:12]}]'
            for _, item in matches[:5]
        )
        raise RuntimeError(f'Multiple Apple {label}s matched "{ref}": {options}')
    return matches[0]


def _subtask_prefixes(title: str) -> tuple[str, str]:
    """
    Return the two possible prefix patterns used for subtasks of a parent.

    Subtasks are named "Parent Title - Subtask Label" or "Parent Title --- Subtask Label"
    (using either a hyphen-dash or an em-dash separator).
    """
    return (f"{title} - ", f"{title} — ")


def _all_reminder_entries(calendar) -> list[tuple[object, AppleReminderItem]]:
    """Load and parse all VTODO resources from a CalDAV calendar."""
    calendar_name = _calendar_name(calendar)
    entries = []
    for resource in _iter_component_resources(calendar, "VTODO"):
        entries.append((resource, _parse_reminder_resource(resource, calendar_name)))
    return entries


def _all_event_entries(calendar) -> list[tuple[object, AppleCalendarEvent]]:
    """Load and parse all VEVENT resources from a CalDAV calendar."""
    calendar_name = _calendar_name(calendar)
    entries = []
    for resource in _iter_component_resources(calendar, "VEVENT"):
        item = _parse_event_resource(resource, calendar_name)
        if item:
            entries.append((resource, item))
    return entries


def create_reminder(
    title: str,
    due: Optional[datetime] = None,
    notes: str = "",
    alert_minutes_before: int = 60,
) -> dict:
    """
    Create a basic Apple Reminder. Delegates to create_rich_reminder
    with default options for backward compatibility.
    """
    return create_rich_reminder(
        title=title,
        due=due,
        notes=notes,
        alert_minutes_before=alert_minutes_before,
    )


def create_rich_reminder(
    title: str,
    due: Optional[datetime] = None,
    notes: str = "",
    alert_minutes_before: int = 60,
    priority: str = "",
    urgent: bool = False,
    location: str = "",
    people: Optional[list[str]] = None,
    subtasks: Optional[list[str]] = None,
    list_name: str = "",
) -> dict:
    """
    Create a rich Apple Reminder with optional priority, location, people,
    and subtasks.

    Subtasks are created as separate VTODO items with a naming convention
    ("Title - SubtaskLabel") because CalDAV does not support native subtask
    hierarchies in a way that works across all Apple clients.

    Returns a dict with calendar_name, subtasks_created count, and uid.
    """
    principal = _principal()
    calendar = _find_calendar(principal, "VTODO", list_name or APPLE_REMINDERS_NAME)
    uid = _todo_uid(title, due, notes)
    calendar.save_todo(
        _make_vtodo(
            title,
            due=due,
            notes=notes,
            alert_minutes_before=alert_minutes_before,
            priority=priority,
            urgent=urgent,
            location=location,
            people=people,
            uid=uid,
        )
    )
    # Create each subtask as a separate VTODO with a prefixed title
    created_subtasks = 0
    for subtask in subtasks or []:
        label = (subtask or "").strip()
        if not label:
            continue
        calendar.save_todo(
            _make_vtodo(
                f"{title} - {label}",
                due=due,
                notes=f"Subtask of {title}",
                alert_minutes_before=alert_minutes_before,
                priority=priority,
                urgent=urgent,
                uid=_todo_uid(label, due, f"Subtask of {title}"),
            )
        )
        created_subtasks += 1
    return {
        "calendar_name": _calendar_name(calendar),
        "subtasks_created": created_subtasks,
        "uid": uid,
    }


def list_apple_reminders(
    limit: int = 10,
    include_completed: bool = False,
    list_name: str = "",
) -> list[AppleReminderItem]:
    """
    List Apple Reminders from iCloud, sorted by due date.

    Completed reminders are excluded by default. Sorting places items
    with due dates first (earliest first), then items without due dates,
    with alphabetical title as a tiebreaker.
    """
    principal = _principal()
    calendar = _find_calendar(principal, "VTODO", list_name or APPLE_REMINDERS_NAME)
    items = [item for _, item in _all_reminder_entries(calendar)]
    if not include_completed:
        items = [item for item in items if not item.completed]
    items.sort(
        key=lambda item: (
            item.due is None,  # Items with due dates sort first
            item.due or datetime.max.replace(tzinfo=timezone.utc),
            item.title.lower(),
        )
    )
    return items[: max(1, limit)]


def update_apple_reminder(
    ref: str,
    title: Optional[str] = None,
    due: Optional[datetime] = None,
    clear_due: bool = False,
    notes: Optional[str] = None,
    clear_notes: bool = False,
    priority: Optional[str] = None,
    urgent: Optional[bool] = None,
    location: Optional[str] = None,
    clear_location: bool = False,
    people: Optional[list[str]] = None,
    clear_people: bool = False,
    alert_minutes_before: Optional[int] = None,
    completed: Optional[bool] = None,
    list_name: str = "",
) -> dict:
    """
    Update an existing Apple Reminder matched by ref (UID, title, or partial title).

    Only fields that are explicitly provided are changed; None means "keep current".
    "clear_*" flags allow explicitly removing a field value.

    If the title is changed, subtasks (identified by the naming convention)
    are automatically renamed to match the new parent title.
    """
    principal = _principal()
    calendar = _find_calendar(principal, "VTODO", list_name or APPLE_REMINDERS_NAME)
    entries = _all_reminder_entries(calendar)
    resource, current = _match_entry(ref, entries, "reminder")

    # Merge provided values with current values, respecting clear flags
    new_title = (title or "").strip() or current.title
    new_due = None if clear_due else (due if due is not None else current.due)
    new_notes = "" if clear_notes else (notes if notes is not None else current.notes)
    new_location = "" if clear_location else (location if location is not None else current.location)
    if clear_people:
        new_people = []
    elif people is None:
        new_people = list(current.people)
    else:
        new_people = [person for person in people if (person or "").strip()]
    priority_label = _priority_label(current.priority)
    new_priority = priority if priority is not None else priority_label
    new_urgent = bool(urgent) if urgent is not None else current.priority == 1
    new_alert = (
        max(0, int(alert_minutes_before))
        if alert_minutes_before is not None
        else current.alert_minutes_before
    )
    status = "COMPLETED" if (completed if completed is not None else current.completed) else "NEEDS-ACTION"

    # Save the updated reminder (no_create=True ensures we update, not duplicate)
    calendar.save_todo(
        _make_vtodo(
            title=new_title,
            due=new_due,
            notes=new_notes,
            alert_minutes_before=new_alert,
            priority=new_priority or "",
            urgent=new_urgent,
            location=new_location,
            people=new_people,
            uid=current.uid,
            status=status,
        ),
        no_create=True,
    )

    # If the title changed, also rename any subtasks that use the old
    # naming convention ("Old Title - Subtask") to "New Title - Subtask"
    renamed_subtasks = 0
    if new_title != current.title:
        for sub_resource, sub_item in entries:
            old_prefix = next(
                (prefix for prefix in _subtask_prefixes(current.title) if sub_item.title.startswith(prefix)),
                "",
            )
            if sub_item.uid == current.uid or not old_prefix:
                continue
            suffix = sub_item.title[len(old_prefix):]
            sub_notes = sub_item.notes.replace(f"Subtask of {current.title}", f"Subtask of {new_title}")
            calendar.save_todo(
                _make_vtodo(
                    title=f"{new_title} - {suffix}",
                    due=new_due if new_due is not None else sub_item.due,
                    notes=sub_notes,
                    alert_minutes_before=sub_item.alert_minutes_before or new_alert,
                    priority=_priority_label(sub_item.priority),
                    urgent=sub_item.priority == 1,
                    location=sub_item.location,
                    people=list(sub_item.people),
                    uid=sub_item.uid,
                    status="COMPLETED" if sub_item.completed else "NEEDS-ACTION",
                ),
                no_create=True,
            )
            renamed_subtasks += 1

    return {
        "calendar_name": _calendar_name(calendar),
        "title": new_title,
        "uid": current.uid,
        "completed": status == "COMPLETED",
        "renamed_subtasks": renamed_subtasks,
    }


def delete_apple_reminder(ref: str, list_name: str = "", delete_subtasks: bool = True) -> dict:
    """
    Delete an Apple Reminder and optionally its subtasks.

    Subtasks are identified by the naming convention ("Title - SubtaskLabel")
    and deleted before the parent to avoid orphans.
    """
    principal = _principal()
    calendar = _find_calendar(principal, "VTODO", list_name or APPLE_REMINDERS_NAME)
    entries = _all_reminder_entries(calendar)
    resource, current = _match_entry(ref, entries, "reminder")
    deleted_subtasks = 0
    if delete_subtasks:
        for sub_resource, sub_item in entries:
            if sub_item.uid == current.uid or not any(
                sub_item.title.startswith(prefix) for prefix in _subtask_prefixes(current.title)
            ):
                continue
            sub_resource.delete()
            deleted_subtasks += 1
    resource.delete()
    return {
        "calendar_name": _calendar_name(calendar),
        "title": current.title,
        "uid": current.uid,
        "deleted_subtasks": deleted_subtasks,
    }


def create_calendar_event(
    title: str,
    start_at: datetime,
    end_at: Optional[datetime] = None,
    notes: str = "",
    location: str = "",
    alert_minutes_before: int = 30,
) -> dict:
    """
    Create an Apple Calendar event.

    If end_at is not provided, defaults to 1 hour after start.
    Validates that end is after start to prevent invalid events.
    """
    start = _utc(start_at)
    end = _utc(end_at) if end_at else start + timedelta(hours=1)
    if end <= start:
        raise RuntimeError("Calendar event end time must be after the start time.")
    principal = _principal()
    calendar = _find_calendar(principal, "VEVENT", APPLE_CALENDAR_NAME)
    uid = _event_uid(title, start)
    calendar.save_event(
        _make_vevent(
            title=title,
            start_at=start,
            end_at=end,
            notes=notes,
            location=location,
            alert_minutes_before=alert_minutes_before,
            uid=uid,
        )
    )
    return {"calendar_name": _calendar_name(calendar), "uid": uid}


def list_upcoming_calendar_events(days: int = 7, limit: int = 10) -> list[AppleCalendarEvent]:
    """
    List upcoming Apple Calendar events within the next N days.

    Uses the CalDAV date_search method for efficient server-side filtering
    rather than downloading all events.
    """
    principal = _principal()
    calendar = _find_calendar(principal, "VEVENT", APPLE_CALENDAR_NAME)
    now = datetime.now(tz=timezone.utc)
    end = now + timedelta(days=max(1, days))
    results = calendar.date_search(start=now, end=end, compfilter="VEVENT")
    events: list[AppleCalendarEvent] = []
    for resource in results:
        item = _parse_event_resource(resource, _calendar_name(calendar))
        if item and item.start_at < end:
            events.append(item)
    events.sort(key=lambda item: item.start_at)
    return events[: max(1, limit)]


def update_apple_calendar_event(
    ref: str,
    title: Optional[str] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    clear_end: bool = False,
    notes: Optional[str] = None,
    clear_notes: bool = False,
    location: Optional[str] = None,
    clear_location: bool = False,
    alert_minutes_before: Optional[int] = None,
) -> dict:
    """
    Update an existing Apple Calendar event matched by ref.

    When the start time changes but the end time is not explicitly provided,
    the event's original duration is preserved (end = new_start + original_duration).
    """
    principal = _principal()
    calendar = _find_calendar(principal, "VEVENT", APPLE_CALENDAR_NAME)
    entries = _all_event_entries(calendar)
    _, current = _match_entry(ref, entries, "calendar event")

    new_title = (title or "").strip() or current.title
    new_start = _utc(start_at) if start_at else current.start_at
    # Preserve the original event duration when only start changes
    duration = (
        current.end_at - current.start_at
        if current.end_at and current.start_at
        else timedelta(hours=1)
    )
    if clear_end:
        new_end = new_start + duration
    elif end_at:
        new_end = _utc(end_at)
    elif start_at and current.end_at:
        # Start changed but end not specified: shift end to keep same duration
        new_end = new_start + duration
    else:
        new_end = current.end_at or (new_start + timedelta(hours=1))
    if new_end <= new_start:
        raise RuntimeError("Calendar event end time must be after the start time.")
    new_notes = "" if clear_notes else (notes if notes is not None else current.notes)
    new_location = "" if clear_location else (location if location is not None else current.location)
    new_alert = (
        max(0, int(alert_minutes_before))
        if alert_minutes_before is not None
        else current.alert_minutes_before
    )

    calendar.save_event(
        _make_vevent(
            title=new_title,
            start_at=new_start,
            end_at=new_end,
            notes=new_notes,
            location=new_location,
            alert_minutes_before=new_alert,
            uid=current.uid,
        ),
        no_create=True,
    )
    return {
        "calendar_name": _calendar_name(calendar),
        "title": new_title,
        "uid": current.uid,
        "start_at": new_start,
        "end_at": new_end,
    }


def delete_apple_calendar_event(ref: str) -> dict:
    """Delete an Apple Calendar event matched by ref (UID, title, or partial title)."""
    principal = _principal()
    calendar = _find_calendar(principal, "VEVENT", APPLE_CALENDAR_NAME)
    resource, current = _match_entry(ref, _all_event_entries(calendar), "calendar event")
    resource.delete()
    return {
        "calendar_name": _calendar_name(calendar),
        "title": current.title,
        "uid": current.uid,
    }
