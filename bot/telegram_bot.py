"""
Hermes Telegram bot -- interactive agent + proactive Slate checker + reminders.

Run:  python -m bot.telegram_bot

Architecture notes:
  - This is the main entry point for the Hermes bot. It sets up the
    Telegram bot application with python-telegram-bot (PTB), registers
    command and message handlers, and starts the polling loop.
  - Message flow:
    1. Telegram sends an Update to the bot
    2. The auth guard (_allowed) checks the chat ID against the allowed list
    3. For commands: the appropriate cmd_* handler runs directly
    4. For messages: handle_message() processes the input, then either:
       a. Short-circuits to job status if the user is asking about progress
       b. Routes to a background sub-agent for browser/terminal tasks
       c. Runs the full LLM agent loop for everything else
  - The proactive Slate checker runs on a configurable interval (default 60 min)
    and notifies the user about new assignments without being asked.
  - Reminders are initialised during post_init (after the event loop is running)
    and use the _send helper to deliver messages.
  - Message delivery uses a markdown-first approach with plain-text fallback,
    because Telegram's markdown parser is strict and rejects some LLM output.
  - Long messages are chunked into 4000-char pieces (Telegram's limit is 4096).
"""

import logging
import os
import sys

from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from bot import agent, jobs, reminders
from bot.message_input import build_agent_input
from slate.checker import cmd_run_check as run_check

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Only allow messages from this chat ID (single-user bot). Empty = allow all.
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# How often (in minutes) to run the proactive Slate check
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("hermes-bot")


# ── auth guard ────────────────────────────────────────────────────────────────

def _allowed(update: Update) -> bool:
    """
    Check if the incoming message is from the allowed Telegram chat.

    If ALLOWED_CHAT_ID is not set, all chats are allowed (useful for
    development). In production, this restricts the bot to a single user.
    """
    cid = str(update.effective_chat.id)
    return not ALLOWED_CHAT_ID or cid == ALLOWED_CHAT_ID


# ── send helper (used by reminders) ──────────────────────────────────────────

# Reference to the PTB Application, set during post_init.
# Used by _send() to deliver messages from non-handler contexts (reminders, jobs).
_app_ref = None

def _chunk_text(text: str) -> list[str]:
    """
    Split text into chunks of at most 4000 characters.

    Telegram's message limit is 4096 characters; we use 4000 to leave
    margin for any added formatting.
    """
    if not text:
        return [""]
    return [text[i:i + 4000] for i in range(0, len(text), 4000)]


async def _deliver_text(markdown_sender, plain_sender, text: str) -> None:
    """
    Deliver text using markdown formatting, falling back to plain text
    if markdown parsing fails.

    Telegram's MarkdownV1 parser is strict and rejects things like
    unmatched asterisks or underscores, which LLMs produce frequently.
    The plain-text fallback ensures the message always gets through.
    """
    for chunk in _chunk_text(text):
        try:
            await markdown_sender(chunk)
        except Exception:
            try:
                await plain_sender(chunk)
            except Exception as e:
                log.error(f"Failed to send message: {e}")


async def _send(chat_id: str, text: str) -> None:
    """
    Send a message to a specific chat ID.

    Used by the reminder system and background jobs to deliver messages
    outside of the normal request-reply flow.
    """
    if not _app_ref:
        return
    await _deliver_text(
        lambda chunk: _app_ref.bot.send_message(
            chat_id=int(chat_id),
            text=chunk,
            parse_mode=ParseMode.MARKDOWN,
        ),
        lambda chunk: _app_ref.bot.send_message(
            chat_id=int(chat_id),
            text=chunk,
        ),
        text,
    )


# ── command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command -- show welcome message with feature overview."""
    if not _allowed(update):
        return
    await update.message.reply_text(
        "👋 *Hey! I'm Hermes — your personal assistant.*\n\n"
        "I can help you with:\n"
        "• 🎓 School — check due schoolwork with /schoolwork\n"
        "• ⏰ Reminders — Telegram reminders or Apple Reminders with subtasks and priority\n"
        "• 📅 Calendar — add and view Apple Calendar events\n"
        "• 🌐 Browser automation — inspect and work through websites\n"
        "• 🤖 Background jobs — long-running tasks keep working while you keep chatting\n"
        "• 🧠 Memory — 'remember that my class is at 10am MWF'\n"
        "• 🎤 Voice notes — I can transcribe and act on them\n"
        "• 🖼 Images — send screenshots/photos and ask about them\n"
        "• 🔍 Web search — I'll look things up for you\n"
        "• 💬 Anything else — just ask\n\n"
        "Just talk to me naturally. No commands needed.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_slate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /slate command -- check Slate for pending work via the agent."""
    if not _allowed(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    async def _status(text: str) -> None:
        await _safe_send(update, f"ℹ️ {text}")

    reply = await agent.chat(
        str(update.effective_chat.id),
        "Check my Slate for pending assignments, quizzes, and discussions.",
        status_cb=_status,
    )
    await _safe_send(update, reply)


async def cmd_schoolwork(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schoolwork command -- alias for /slate."""
    await cmd_slate(update, ctx)


async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reminders command -- list pending reminders."""
    if not _allowed(update):
        return
    text = reminders.list_reminders()
    await _safe_send(update, text)


async def cmd_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /calendar command -- list upcoming Apple Calendar events."""
    if not _allowed(update):
        return
    from bot.tools import list_apple_calendar_events
    await _safe_send(update, list_apple_calendar_events())


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tasks command -- list open tasks."""
    if not _allowed(update):
        return
    from bot.tools import list_tasks
    await _safe_send(update, list_tasks())


async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /jobs command -- list background sub-agent jobs."""
    if not _allowed(update):
        return
    text = jobs.list_jobs_text(str(update.effective_chat.id), include_done=True, limit=10)
    await _safe_send(update, text)


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /memory command -- list stored memories."""
    if not _allowed(update):
        return
    from bot.memory import list_all
    await _safe_send(update, list_all())


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command -- clear conversation history for this chat."""
    if not _allowed(update):
        return
    agent.clear_history(str(update.effective_chat.id))
    await update.message.reply_text("🗑 Conversation history cleared.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command -- show available commands and usage examples."""
    if not _allowed(update):
        return
    await update.message.reply_text(
        "*Commands:*\n"
        "/start — intro\n"
        "/schoolwork — check due school work\n"
        "/slate — same as /schoolwork\n"
        "/models — choose the primary LLM\n"
        "/calendar — show Apple Calendar events\n"
        "/tasks — list current tasks\n"
        "/jobs — list background sub-agent jobs\n"
        "/reminders — list pending reminders\n"
        "/memory — list stored memories\n"
        "/clear — clear conversation history\n"
        "/help — this message\n\n"
        "*Just talk naturally for everything else!*\n"
        "e.g. 'remind me tomorrow at 9am to submit my assignment'\n"
        "e.g. 'add dentist tomorrow at 3pm to Apple Calendar'\n"
        "e.g. 'make an urgent Apple reminder with subtasks to renew passport'\n"
        "e.g. 'open sheridanworks in the browser and tell me what you see'\n"
        "e.g. 'find me a 30W wireless charger on Amazon.ca' and then ask 'how's that going?'\n"
        "e.g. 'what's due this week?'\n"
        "e.g. '/schoolwork'\n"
        "e.g. send a voice note with a reminder or task\n"
        "e.g. send a screenshot and ask 'what does this mean?'\n"
        "e.g. 'remember my student ID is 123456'\n"
        "e.g. 'search for how to write a literature review'",
        parse_mode=ParseMode.MARKDOWN,
    )


def _model_keyboard() -> InlineKeyboardMarkup:
    """
    Build an inline keyboard for the /models command.

    Each model is a button. The currently selected model gets a checkmark prefix.
    """
    current = agent.get_preferred_model()
    rows = []
    for item in agent.get_model_options():
        prefix = "✅ " if item["id"] == current else ""
        rows.append([InlineKeyboardButton(f"{prefix}{item['label']}", callback_data=f"model:{item['id']}")])
    return InlineKeyboardMarkup(rows)


def _model_menu_text() -> str:
    """Build the text content for the model selection menu."""
    lines = [
        "*Model Selection*",
        f"Provider: *{agent.get_provider_label()}*",
        f"Primary: `{agent.get_preferred_model()}`",
        "",
        "Choose the primary model for the current provider.",
        "Fallbacks stay enabled automatically.",
        "",
        "*Fallback order:*",
    ]
    for model_id in agent.get_fallback_chain():
        lines.append(f"• `{model_id}`")
    lines.append("")
    lines.append("*Options:*")
    for item in agent.get_model_options():
        lines.append(f"• *{item['label']}*: {item['notes']}")
    return "\n".join(lines)


async def cmd_models(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /models command -- show model selection menu with inline keyboard."""
    if not _allowed(update):
        return
    await update.message.reply_text(
        _model_menu_text(),
        reply_markup=_model_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_model_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle inline keyboard callback when a model is selected.

    Updates the preferred model and refreshes the menu to show the
    new selection.
    """
    query = update.callback_query
    if not query:
        return
    if not _allowed(update):
        await query.answer()
        return
    data = query.data or ""
    if not data.startswith("model:"):
        await query.answer()
        return
    model_id = data.split(":", 1)[1]
    try:
        agent.set_preferred_model(model_id)
    except Exception as e:
        log.error(f"Model selection failed: {e}")
        await query.answer("Could not change model.", show_alert=True)
        return
    await query.answer("Primary model updated.")
    # Refresh the menu in-place to show the updated selection
    await query.edit_message_text(
        _model_menu_text(),
        reply_markup=_model_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── safe send helper ─────────────────────────────────────────────────────────

async def _safe_send(update: Update, text: str) -> None:
    """Send reply with markdown, falling back to plain text on parse error."""
    await _deliver_text(
        lambda chunk: update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN),
        lambda chunk: update.message.reply_text(chunk),
        text,
    )


# ── message handler ───────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main message handler for all non-command messages.

    Processing flow:
      1. Build agent input from the message (text, voice, image)
      2. If the user is asking about job status and there are active jobs,
         short-circuit to the job status response
      3. If the message looks like a browser/terminal task, route to a
         background sub-agent so the main chat stays responsive
      4. Otherwise, run the full LLM agent loop inline
    """
    if not _allowed(update) or not update.message:
        return

    chat_id = str(update.effective_chat.id)
    user_text = await build_agent_input(update.message, ctx.bot)
    if not user_text:
        await update.message.reply_text("I can handle text, voice notes, and images right now.")
        return

    # Short-circuit: status queries when background jobs are active
    if jobs.is_status_query(user_text) and jobs.has_active_jobs(chat_id):
        await _safe_send(update, jobs.job_status_text(chat_id))
        return

    # Route browser/terminal tasks to background sub-agent
    if jobs.should_background(user_text) and not jobs.is_status_query(user_text):
        async def _run_agent(worker_chat_id: str, status_cb) -> str:
            return await agent.chat(worker_chat_id, user_text, status_cb=status_cb, background_mode=True)

        try:
            await jobs.start_background_agent_job(
                chat_id=chat_id,
                prompt=user_text,
                run_agent=_run_agent,
                send_text=_send,
                note_event=agent.note_event,
            )
        except Exception as e:
            log.error(f"Background job start failed: {e}", exc_info=True)
            await _safe_send(update, f"⚠️ Couldn't start the background task: {e}")
        return

    # Normal flow: show typing indicator and run the agent inline
    await update.message.chat.send_action(ChatAction.TYPING)

    async def _status(text: str) -> None:
        await _safe_send(update, f"ℹ️ {text}")

    try:
        reply = await agent.chat(chat_id, user_text, status_cb=_status)
    except Exception as e:
        log.error(f"Agent error: {e}", exc_info=True)
        reply = f"⚠️ Something went wrong: {e}"

    await _safe_send(update, reply)


# ── proactive Slate checker ───────────────────────────────────────────────────

async def _periodic_slate_check() -> None:
    """
    Scheduled task that checks Slate for new assignments/changes.

    Runs on an interval (CHECK_INTERVAL minutes) and notifies the user
    about new items without being asked.
    """
    log.info("Running scheduled Slate check...")
    try:
        await run_check(notify_new=True)
    except Exception as e:
        log.error(f"Scheduled Slate check failed: {e}")


# ── main ──────────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """
    Called by PTB after the event loop is running -- safe for async setup.

    Initialises:
      1. The reminder scheduler with SQLite persistence
      2. The periodic Slate checker on the configured interval
      3. Clears legacy Slate reminder jobs from previous runs
      4. Registers bot menu commands visible in the Telegram UI
    """
    global _app_ref
    _app_ref = app

    # Start the reminder scheduler
    scheduler = reminders.init_scheduler(_send)
    scheduler.add_job(
        _periodic_slate_check,
        "interval",
        minutes=CHECK_INTERVAL,
        id="slate_periodic_check",
        replace_existing=True,
        misfire_grace_time=120,
    )
    scheduler.start()
    # Clear legacy Slate reminders from previous runs before re-syncing
    removed = reminders.clear_slate_reminders()
    if removed:
        log.info(f"Cleared {removed} legacy Slate reminder jobs")
    log.info(f"Scheduler started — Slate check every {CHECK_INTERVAL} min")

    # Register bot menu commands (visible in Telegram's command menu)
    await app.bot.set_my_commands([
        BotCommand("schoolwork", "Show due school work"),
        BotCommand("slate", "Check assignments & quizzes"),
        BotCommand("models", "Choose primary LLM"),
        BotCommand("calendar", "Show Apple Calendar events"),
        BotCommand("tasks", "List current tasks"),
        BotCommand("jobs", "List background jobs"),
        BotCommand("reminders", "List pending reminders"),
        BotCommand("memory", "List stored memories"),
        BotCommand("clear", "Clear conversation history"),
        BotCommand("help", "Show help"),
    ])
    log.info(f"Bot ready — provider: {agent.get_provider_label()} model: {agent.get_preferred_model()}")


def main() -> None:
    """
    Entry point: build the PTB application, register all handlers, and
    start polling for updates.

    drop_pending_updates=True skips any messages that arrived while the
    bot was offline, preventing a flood of stale messages on restart.
    """
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("schoolwork", cmd_schoolwork))
    app.add_handler(CommandHandler("slate", cmd_slate))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("calendar", cmd_calendar))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    # Inline keyboard callback for model selection
    app.add_handler(CallbackQueryHandler(on_model_selected, pattern=r"^model:"))
    # Catch-all message handler for text, voice, audio, photos, and documents
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.VOICE | filters.AUDIO | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            handle_message,
        )
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
