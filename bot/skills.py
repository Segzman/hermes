"""
Pattern-matched skill router — handles common intents WITHOUT hitting the LLM.
Falls through to the agent for complex/ambiguous queries.

This makes basic commands 100% reliable regardless of model quality.
"""

import re
from bot.tools import (
    slate_check_assignments, slate_check_announcements, slate_check_grades,
    slate_check_messages, slate_refresh, slate_get_assignment_details,
    get_current_time, web_search, set_reminder, set_apple_reminder,
    list_reminders, cancel_reminder, remember, recall, list_memories, forget,
    list_tasks, add_task, complete_task, add_apple_calendar_event,
    list_apple_calendar_events,
)


def try_skill(text: str) -> str | None:
    """
    Try to match user text to a known skill pattern.
    Returns the response string if matched, or None to fall through to LLM.
    """
    t = text.lower().strip()

    # ── Slate: what's due ─────────────────────────────────────────────────
    if _match(t, [
        r"what.?s due", r"due this week", r"due today", r"due tomorrow",
        r"pending (assignments?|work|tasks?|deliverables?)",
        r"check (my )?(slate|assignments?|school|homework)",
        r"^/slate$", r"^/schoolwork$", r"upcoming (assignments?|quizzes?|work)",
        r"anything due", r"do i have (any )?(assignments?|homework|work)",
    ]):
        days = None
        if "today" in t:
            days = 0
        elif "tomorrow" in t:
            days = 1
        elif "this week" in t or "next few days" in t:
            days = 7
        return slate_check_assignments(days_ahead=days)

    # ── Slate: grades ─────────────────────────────────────────────────────
    if _match(t, [r"(my )?grades?", r"marks?", r"scores?", r"how did i do"]):
        return slate_check_grades()

    # ── Slate: announcements ──────────────────────────────────────────────
    if _match(t, [r"announcements?", r"(any )?news", r"what.?s new"]):
        return slate_check_announcements()

    # ── Slate: messages ───────────────────────────────────────────────────
    if _match(t, [r"(unread )?(slate )?messages?", r"(my )?inbox"]):
        return slate_check_messages()

    # ── Slate: refresh ────────────────────────────────────────────────────
    if _match(t, [r"refresh slate", r"force (re)?fetch", r"update slate"]):
        return slate_refresh()

    # ── Slate: assignment details ─────────────────────────────────────────
    m = re.search(r"details?\s+(for\s+)?#?(\d+)", t)
    if m:
        return slate_get_assignment_details(m.group(2))

    # ── Time ──────────────────────────────────────────────────────────────
    if _match(t, [r"what time", r"current time", r"what.?s the time", r"time (is it|now)"]):
        return get_current_time()

    # ── Weather (direct search) ───────────────────────────────────────────
    m = re.search(r"weather\s+(in\s+)?(.+)", t)
    if m:
        city = m.group(2).strip().rstrip("?.")
        return web_search(f"weather {city} today")

    # ── Web search (explicit) ─────────────────────────────────────────────
    for prefix in [r"search\s+(for\s+)?", r"google\s+", r"look\s*up\s+"]:
        m = re.match(prefix + r"(.+)", t)
        if m:
            return web_search(m.group(m.lastindex).strip().rstrip("?."))

    # ── Reminders ─────────────────────────────────────────────────────────
    if _match(t, [r"^/reminders?$", r"(list|show|my) reminders?", r"pending reminders?"]):
        return list_reminders()

    if _match(t, [r"^/calendar$", r"(show|list|my) calendar", r"upcoming calendar"]):
        return list_apple_calendar_events()

    m = re.match(r"remind me\s+(.+?)\s+to\s+(.+)", t)
    if m:
        return set_reminder(when=m.group(1), message=m.group(2))
    m = re.match(r"remind me\s+to\s+(.+?)\s+(in\s+.+|at\s+.+|tomorrow.+|on\s+.+)", t)
    if m:
        return set_reminder(when=m.group(2), message=m.group(1))

    m = re.match(r"cancel reminder\s+(.+)", t)
    if m:
        return cancel_reminder(m.group(1).strip())

    m = re.match(r"(apple|icloud|iphone)\s+reminder\s+(.+?)\s+(?:for|at)\s+(.+)", t)
    if m:
        return set_apple_reminder(title=m.group(2).strip(), when=m.group(3).strip())

    m = re.match(r"add\s+(.+?)\s+to\s+(apple|icloud|iphone)\s+calendar(?:\s+at\s+(.+))?", t)
    if m:
        start = m.group(3).strip() if m.group(3) else "tomorrow at 9am"
        return add_apple_calendar_event(title=m.group(1).strip(), start=start)

    # ── Tasks ─────────────────────────────────────────────────────────────
    if _match(t, [r"^/tasks?$", r"(show|list|my) tasks?", r"what (should i do|do i have)"]):
        return list_tasks()

    m = re.match(r"(add|create)\s+(a\s+)?task\s*[:\-]?\s*(.+)", t)
    if m:
        return add_task(title=m.group(3).strip())

    m = re.match(r"(finish|complete|done with)\s+task\s+(.+)", t)
    if m:
        return complete_task(m.group(2).strip())

    # ── Memory ────────────────────────────────────────────────────────────
    if _match(t, [r"^/memory$", r"(list|show|my) memories?", r"what do you (remember|know)"]):
        return list_memories()

    m = re.match(r"remember\s+(that\s+)?(.+)", t, re.I)
    if m:
        content = m.group(2).strip()
        name = content[:40].replace(" ", "_").lower()
        return remember(name=name, content=content, memory_type="note")

    m = re.match(r"(recall|do you remember)\s+(.+)", t, re.I)
    if m:
        return recall(m.group(2).strip())

    m = re.match(r"forget\s+(about\s+)?(.+)", t, re.I)
    if m:
        return forget(m.group(2).strip())

    # ── Greetings ─────────────────────────────────────────────────────────
    if _match(t, [r"^(hi|hello|hey|sup|yo|what.?s up)\b"]):
        return "Hey! What do you need?"

    # ── No match → fall through to LLM ───────────────────────────────────
    return None


def _match(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)
