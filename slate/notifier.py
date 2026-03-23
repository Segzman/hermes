"""
Notification backends for the Slate checker.

Supports two independent notification channels:
  1. Telegram  — sends an HTML-formatted message via the Telegram Bot API
  2. Apple Reminders — creates an iCloud reminder via CalDAV (delegated to bot.apple)

Both are optional; configure whichever you want in .env:
  - TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  for Telegram
  - Apple Reminders requires iCloud credentials configured in bot.apple

Architecture:
  The notify() function is the single entry point called by checker.py.
  It fans out to all configured channels and logs which ones succeeded.
  If no channels are configured, it falls back to printing to stdout
  (useful for development and debugging).
"""

import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from bot.apple import create_reminder as apple_create_reminder

load_dotenv()

# Telegram configuration — both must be set for Telegram notifications to work
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Telegram ──────────────────────────────────────────────────────────────────

async def telegram_send(message: str) -> bool:
    """
    Send a Telegram message via the Bot API. Returns True on success.

    Uses HTML parse_mode so callers can include <b>bold</b> and <a href>links</a>.
    Web page preview is disabled to keep notifications compact.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        import httpx
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
            return resp.status_code == 200
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def apple_reminder_create(title: str, due: Optional[datetime] = None, notes: str = "") -> bool:
    """
    Create an iCloud Reminder via CalDAV. Returns True on success.

    Delegates to bot.apple.create_reminder which handles the CalDAV protocol.
    The due date, if provided, sets the reminder's alarm time.
    """
    try:
        apple_create_reminder(title, due=due, notes=notes)
        return True
    except Exception as e:
        print(f"Apple Reminders failed: {e}")
        return False


# ── Combined send ─────────────────────────────────────────────────────────────

async def notify(
    title: str,
    body: str,
    due: Optional[datetime] = None,
    url: Optional[str] = None,
) -> None:
    """
    Send a notification via all configured channels.

    Constructs channel-specific payloads:
      - Telegram: HTML-formatted message with optional "Open in Slate" link
      - Apple Reminders: plain-text note with optional Slate URL appended

    Logs which channels succeeded. If none are configured, prints to stdout
    as a fallback so notifications are not silently lost during development.
    """
    # Build Telegram message with HTML formatting
    tg_lines = [f"<b>{title}</b>", body]
    if url:
        tg_lines.append(f'<a href="{url}">Open in Slate</a>')
    tg_msg = "\n".join(tg_lines)

    tg_ok = await telegram_send(tg_msg)
    if tg_ok:
        print(f"  [Telegram] sent: {title}")

    # Apple Reminders — include the Slate URL in the notes field
    reminder_notes = body
    if url:
        reminder_notes += f"\n\nSlate URL: {url}"
    ar_ok = apple_reminder_create(title, due=due, notes=reminder_notes)
    if ar_ok:
        print(f"  [Apple Reminders] created: {title}")

    # Fallback: if no notification channels worked, print to stdout
    if not tg_ok and not ar_ok:
        print(f"  [notify] no channels configured — printing: {title}\n  {body}")
