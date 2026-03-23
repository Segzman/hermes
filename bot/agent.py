"""
OpenRouter agent loop with tool calling.
Manages per-chat conversation history and executes tools.

This module is the core LLM integration layer. It:
  1. Maintains a per-chat conversation history (capped at MAX_HISTORY messages).
  2. Builds a rich system prompt that includes tool docs, memory context, and
     active background job summaries.
  3. Sends messages to the configured LLM provider (OpenRouter or Bedrock)
     with automatic fallback across multiple free models.
  4. Handles multi-step tool calling in a sequential loop (important for
     stateful browser/server tools).
  5. Detects and recovers from rate limits, quota exhaustion, garbled
     "function-as-text" outputs, and provider policy blocks.

Architecture notes:
  - The AsyncOpenAI client is reused for both OpenRouter and Bedrock because
    Bedrock exposes an OpenAI-compatible endpoint through its "Mantle" gateway.
  - Model preference is persisted to ~/.hermes/model_pref.json so it survives
    restarts and can be changed via the Telegram /models command.
  - Rate-limited models are temporarily disabled (cooldown) so the fallback
    chain can skip them without wasting time.
"""

import asyncio
import inspect
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI, RateLimitError
from dotenv import load_dotenv

from bot import jobs
from bot.tools import TOOLS, TOOL_CALLABLES
from bot.memory import get_index_for_prompt

load_dotenv()

log = logging.getLogger("hermes-agent")

# How many minutes to skip a model after it returns a 429 rate-limit error.
RATE_LIMIT_COOLDOWN_MINUTES = int(os.getenv("MODEL_RATE_LIMIT_COOLDOWN_MINUTES", "15"))

# File where the user's preferred model selection is persisted across restarts.
MODEL_PREF_FILE = Path(os.path.expanduser("~/.hermes/model_pref.json"))

# Available model options for the OpenRouter provider.
# Each entry includes a human-readable label and operational notes gleaned
# from live testing (tool-call support, privacy policy quirks, etc.).
OPENROUTER_MODEL_OPTIONS = [
    {
        "id": "qwen/qwen3-next-80b-a3b-instruct:free",
        "label": "Qwen3 Next 80B A3B",
        "notes": "Best current pinned free default for this bot.",
    },
    {
        "id": "openrouter/free",
        "label": "OpenRouter Free Auto",
        "notes": "Zero-maintenance router across free models.",
    },
    {
        "id": "arcee-ai/trinity-mini:free",
        "label": "Arcee Trinity Mini",
        "notes": "Light fallback with decent tool-use behavior.",
    },
    {
        "id": "stepfun/step-3.5-flash:free",
        "label": "Step 3.5 Flash",
        "notes": "Popular on OpenRouter; may not expose tool endpoints on your account.",
    },
    {
        "id": "openai/gpt-oss-120b:free",
        "label": "GPT-OSS 120B",
        "notes": "Strong benchmark profile; may be blocked by privacy policy settings.",
    },
]

# Available model options for the Amazon Bedrock provider.
# These models are accessed through Bedrock's OpenAI-compatible "Mantle" gateway.
BEDROCK_MODEL_OPTIONS = [
    {
        "id": "qwen.qwen3-next-80b-a3b-instruct",
        "label": "Qwen3 Next 80B A3B",
        "notes": "Best overall fit for Hermes from live tool-call and planning tests.",
    },
    {
        "id": "zai.glm-5",
        "label": "GLM-5",
        "notes": "Strong general assistant fallback with clean tool use.",
    },
    {
        "id": "deepseek.v3.2",
        "label": "DeepSeek V3.2",
        "notes": "Good reasoning/coding fallback; tool calls worked cleanly.",
    },
    {
        "id": "mistral.mistral-large-3-675b-instruct",
        "label": "Mistral Large 3",
        "notes": "Strong structured planner; likely slower and pricier.",
    },
    {
        "id": "moonshotai.kimi-k2.5",
        "label": "Kimi K2.5",
        "notes": "Good fallback; tool use worked in live probes.",
    },
    {
        "id": "qwen.qwen3-32b",
        "label": "Qwen3 32B",
        "notes": "Smaller backup option if larger models are unavailable.",
    },
]

# Determine which LLM provider to use. Defaults to OpenRouter.
# Only "openrouter" and "bedrock" are valid; anything else falls back to OpenRouter.
PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
if PROVIDER not in {"openrouter", "bedrock"}:
    PROVIDER = "openrouter"


def _browser_runtime_note() -> str:
    """
    Build a short description of the active browser backend configuration
    for injection into the system prompt.

    This tells the LLM which browser backend is in use (Browser Use,
    Browserbase, or local Playwright) so it can answer meta-questions like
    "are you using Browserbase?" accurately.
    """
    def _normalize_backend(value: str, default: str = "local") -> str:
        """Map various backend name aliases to canonical names."""
        backend = (value or "").strip().lower()
        if not backend:
            return default
        aliases = {
            "browser-use": "browser-use",
            "browseruse": "browser-use",
            "bu": "browser-use",
            "browserbase": "browserbase",
            "browserbase-cdp": "browserbase",
            "bb": "browserbase",
            "local": "local",
            "playwright": "local",
        }
        return aliases.get(backend, default)

    def _label(backend: str) -> str:
        """Return a human-readable label for a backend identifier."""
        if backend == "browser-use":
            return "Browser Use"
        if backend == "browserbase":
            return "Browserbase"
        return "local Playwright"

    backend = _normalize_backend(os.getenv("BROWSER_BACKEND", "local"), default="local")
    fallback = _normalize_backend(os.getenv("BROWSER_FALLBACK_BACKEND", ""), default="")
    if backend == "browser-use":
        has_api_key = bool(os.getenv("BROWSER_USE_API_KEY", "").strip())
        profile_id = os.getenv("BROWSER_USE_PROFILE_ID", "").strip()
        fallback_text = f" with {_label(fallback)} fallback configured" if fallback and fallback != backend else ""
        if has_api_key and profile_id:
            return (
                f"Current browser backend: Browser Use primary{fallback_text} with a reusable profile configured. "
                "If the user asks whether you are using Browser Use, the answer is yes."
            )
        if has_api_key:
            return (
                f"Current browser backend: Browser Use primary{fallback_text}. "
                "If the user asks whether you are using Browser Use, the answer is yes."
            )
        if fallback and fallback != backend:
            return (
                f"Current browser backend: Browser Use is configured as primary but missing its API key, "
                f"so {_label(fallback)} will be used as fallback."
            )
        return (
            "Current browser backend: Browser Use is configured as primary but missing its API key, "
            "so browser actions will fail until that is set."
        )
    if backend == "browserbase":
        context_id = os.getenv("BROWSERBASE_CONTEXT_ID", "").strip()
        fallback_text = f" with {_label(fallback)} fallback configured" if fallback and fallback != backend else ""
        if context_id:
            return (
                f"Current browser backend: Browserbase{fallback_text} with a reusable persistent context configured. "
                "If the user asks whether you are using Browserbase, the answer is yes."
            )
        return (
            f"Current browser backend: Browserbase{fallback_text} without a saved persistent context. "
            "If the user asks whether you are using Browserbase, the answer is yes."
        )
    return (
        "Current browser backend: local Playwright on the server. "
        "If the user asks whether you are using Browserbase, the answer is no."
    )


def _provider_settings() -> dict:
    """
    Return a settings dict for the active LLM provider, including:
      - label: human-readable name
      - default_model: model ID to use when no preference is saved
      - api_key: API key from environment
      - base_url: OpenAI-compatible API endpoint
      - headers: extra HTTP headers (e.g. OpenRouter referer tracking)
      - options: list of model option dicts for the /models menu
      - fallbacks: ordered list of model IDs to try when the primary fails
    """
    if PROVIDER == "bedrock":
        return {
            "label": "Amazon Bedrock",
            "default_model": os.getenv("BEDROCK_MODEL", "qwen.qwen3-next-80b-a3b-instruct"),
            "api_key": os.getenv("BEDROCK_API_KEY", ""),
            "base_url": os.getenv("BEDROCK_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/v1"),
            "headers": {},
            "options": BEDROCK_MODEL_OPTIONS,
            "fallbacks": [
                "qwen.qwen3-next-80b-a3b-instruct",
                "zai.glm-5",
                "deepseek.v3.2",
                "mistral.mistral-large-3-675b-instruct",
                "moonshotai.kimi-k2.5",
                "qwen.qwen3-32b",
            ],
        }
    return {
        "label": "OpenRouter",
        "default_model": os.getenv("OPENROUTER_MODEL", "qwen/qwen3-next-80b-a3b-instruct:free"),
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "headers": {
            "HTTP-Referer": "https://github.com/hermes-slate-assistant",
            "X-Title": "Hermes Slate Assistant",
        },
        "options": OPENROUTER_MODEL_OPTIONS,
        "fallbacks": [
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "arcee-ai/trinity-mini:free",
            "openrouter/free",
            "stepfun/step-3.5-flash:free",
            "openai/gpt-oss-120b:free",
        ],
    }


# Initialise provider settings at module load time.
SETTINGS = _provider_settings()
MODEL = SETTINGS["default_model"]
API_KEY = SETTINGS["api_key"]
MODEL_OPTIONS = SETTINGS["options"]
# Fast lookup set for validating model IDs from user selection.
_model_option_ids = {item["id"] for item in MODEL_OPTIONS}
# Tracks when each model was rate-limited so we can skip it temporarily.
_rate_limited_until: dict[str, datetime] = {}
# Tracks when each model was disabled (e.g. policy block) for a longer window.
_disabled_until: dict[str, datetime] = {}

# Single shared AsyncOpenAI client, configured for the active provider.
_client = AsyncOpenAI(
    api_key=API_KEY,
    base_url=SETTINGS["base_url"],
    default_headers=SETTINGS["headers"],
)

# Conversation history per chat_id, capped to keep token use reasonable.
# Each key is a Telegram chat ID string; each value is a list of message dicts.
_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 20  # messages to keep per user


def _dedupe_models(models: list[str]) -> list[str]:
    """Remove duplicate model IDs while preserving order."""
    seen = set()
    return [model for model in models if not (model in seen or seen.add(model))]


def get_model_options() -> list[dict]:
    """Return a copy of the available model options for display."""
    return [dict(item) for item in MODEL_OPTIONS]


def get_provider() -> str:
    """Return the canonical provider name ('openrouter' or 'bedrock')."""
    return PROVIDER


def get_provider_label() -> str:
    """Return the human-readable provider label (e.g. 'OpenRouter')."""
    return SETTINGS["label"]


def _load_preferred_model() -> Optional[str]:
    """
    Load the user's preferred model from the persisted file.

    Returns None if the file does not exist, is corrupt, was saved for
    a different provider, or contains an unrecognised model ID.
    """
    if not MODEL_PREF_FILE.exists():
        return None
    try:
        data = json.loads(MODEL_PREF_FILE.read_text())
    except Exception:
        return None
    model_id = data.get("preferred_model")
    # Ignore preferences saved for a different provider to avoid cross-wiring.
    stored_provider = data.get("provider")
    if stored_provider and stored_provider != PROVIDER:
        return None
    return model_id if model_id in _model_option_ids else None


def get_preferred_model() -> str:
    """Return the user's preferred model, falling back to the provider default."""
    return _load_preferred_model() or MODEL


def set_preferred_model(model_id: str) -> str:
    """
    Save the user's preferred model to disk and clear any rate-limit cooldown
    for that model so it is tried immediately.
    """
    if model_id not in _model_option_ids:
        raise ValueError(f"Unknown model: {model_id}")
    MODEL_PREF_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PREF_FILE.write_text(json.dumps({"provider": PROVIDER, "preferred_model": model_id}))
    _rate_limited_until.pop(model_id, None)
    return model_id


def get_model_label(model_id: str) -> str:
    """Return the human-readable label for a model ID, or the raw ID if unknown."""
    for item in MODEL_OPTIONS:
        if item["id"] == model_id:
            return item["label"]
    return model_id


def get_fallback_chain() -> list[str]:
    """
    Build the ordered list of models to try: preferred model first,
    then the provider's configured fallbacks, with duplicates removed.
    """
    preferred = get_preferred_model()
    candidates = [preferred] + SETTINGS["fallbacks"]
    return _dedupe_models(candidates)


def _format_reset_time(err_text: str) -> Optional[str]:
    """
    Extract the X-RateLimit-Reset timestamp from an OpenRouter error message
    and format it in Toronto local time.

    The timestamp is a Unix epoch in milliseconds embedded in the error string.
    Returns None if the pattern is not found or parsing fails.
    """
    import re

    # OpenRouter includes the reset timestamp in the error body like:
    # "X-RateLimit-Reset': '1711234567890'"
    match = re.search(r"X-RateLimit-Reset': '(\d+)'", err_text)
    if not match:
        return None
    try:
        # OpenRouter uses millisecond timestamps, so divide by 1000
        ts = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
    except Exception:
        return None
    local = ts.astimezone(ZoneInfo("America/Toronto"))
    return local.strftime("%a %b %d %I:%M %p Toronto")


def _quota_exhausted_message(err_text: str) -> str:
    """Build a user-friendly message when the daily free-model quota is used up."""
    reset = _format_reset_time(err_text)
    if reset:
        return (
            f"OpenRouter free-model quota is exhausted for today. "
            f"It resets at {reset}. Add credits or wait for reset."
        )
    return "OpenRouter free-model quota is exhausted for today. Add credits or wait for reset."


async def _emit_status(status_cb, text: str) -> None:
    """Invoke the status callback if one is registered, for progress updates."""
    if status_cb is not None:
        await status_cb(text)


async def _call_with_fallback(messages: list, force_text: bool = False, status_cb=None) -> object:
    """
    Try each free model in order.
    Retries on: rate limit, exception, empty response, or garbled function-text output.
    force_text=True omits tools entirely so the model must produce plain text.

    This is the main LLM call function. It iterates through the fallback chain,
    skipping models that are currently in cooldown. For each model it:
      - Makes the API call with tools (unless force_text is set)
      - Validates the response (non-empty, no garbled tool syntax)
      - On rate limit: marks the model for cooldown and tries the next
      - On policy/endpoint errors: disables the model for a longer period
    """
    last_err = None
    if not API_KEY:
        raise RuntimeError(f"{get_provider_label()} API key not configured.")
    kwargs: dict = {
        "temperature": 0.7,
        "max_tokens": 2048,
    }
    # OpenRouter-specific: require the model to support our parameter set
    # and allow automatic fallback within OpenRouter's own routing.
    if PROVIDER == "openrouter":
        kwargs["extra_body"] = {"provider": {"require_parameters": True, "allow_fallbacks": True}}
    if not force_text:
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = "auto"

    now = datetime.now(tz=timezone.utc)
    chain = get_fallback_chain()
    # Filter out models in cooldown/disabled state. If all are blocked,
    # fall back to the full chain anyway (better to retry than give up).
    available_chain = [
        model for model in chain
        if not (_rate_limited_until.get(model) and _rate_limited_until[model] > now)
        and not (_disabled_until.get(model) and _disabled_until[model] > now)
    ] or chain

    for idx, model in enumerate(available_chain):
        # Double-check cooldown state (the chain was filtered above, but
        # this handles edge cases with timing).
        blocked_until = _rate_limited_until.get(model)
        if blocked_until and blocked_until > now:
            continue
        disabled_until = _disabled_until.get(model)
        if disabled_until and disabled_until > now:
            continue
        try:
            resp = await _client.chat.completions.create(model=model, messages=messages, **kwargs)
            msg = resp.choices[0].message
            content = (msg.content or "").strip()
            # Skip blank responses — some models return empty content
            # with no tool calls, which is useless.
            if not msg.tool_calls and not content:
                log.warning(f"Empty response from {model}, trying next...")
                await asyncio.sleep(0.5)
                continue
            # Skip garbled function-as-text outputs. Some older "Hermes"-style
            # models emit tool calls as literal text like "<function=foo>"
            # instead of using the structured tool_calls field.
            if not msg.tool_calls and "<function=" in content:
                log.warning(f"Garbled tool output from {model}, trying next...")
                await asyncio.sleep(0.5)
                continue
            return resp
        except RateLimitError as e:
            err_text = str(e)
            # OpenRouter has a daily quota for free models; when it is hit,
            # no other free model will work either, so raise immediately.
            if PROVIDER == "openrouter" and "free-models-per-day" in err_text:
                raise RuntimeError(_quota_exhausted_message(err_text))
            _rate_limited_until[model] = now + timedelta(minutes=RATE_LIMIT_COOLDOWN_MINUTES)
            log.warning(f"Rate limited on {model}, cooling down for {RATE_LIMIT_COOLDOWN_MINUTES} min and trying next...")
            next_model = available_chain[idx + 1] if idx + 1 < len(available_chain) else None
            if next_model:
                await _emit_status(
                    status_cb,
                    f"{get_model_label(model)} hit a rate limit. Trying {get_model_label(next_model)}.",
                )
            last_err = e
            await asyncio.sleep(1)
        except Exception as e:
            err_text = str(e)
            # OpenRouter policy blocks: disable the model for 24 hours so we
            # stop wasting attempts on it.
            if PROVIDER == "openrouter" and "guardrail restrictions and data policy" in err_text:
                _disabled_until[model] = now + timedelta(hours=24)
            # No compatible endpoint: disable for 6 hours (may come back).
            elif PROVIDER == "openrouter" and "No endpoints found that can handle the requested parameters" in err_text:
                _disabled_until[model] = now + timedelta(hours=6)
            log.warning(f"Error on {model}: {e}, trying next...")
            next_model = available_chain[idx + 1] if idx + 1 < len(available_chain) else None
            if next_model:
                if PROVIDER == "openrouter" and "guardrail restrictions and data policy" in err_text:
                    notice = f"{get_model_label(model)} is blocked by current OpenRouter privacy settings. Trying {get_model_label(next_model)}."
                elif PROVIDER == "openrouter" and "No endpoints found that can handle the requested parameters" in err_text:
                    notice = f"{get_model_label(model)} has no compatible endpoint right now. Trying {get_model_label(next_model)}."
                else:
                    notice = f"{get_model_label(model)} failed. Trying {get_model_label(next_model)}."
                await _emit_status(status_cb, notice)
            last_err = e
            await asyncio.sleep(1)
    raise last_err or RuntimeError("All models failed")


def _system_prompt(chat_id: str, background_mode: bool = False) -> str:
    """
    Build the full system prompt for the LLM.

    The prompt includes:
      - Hermes identity and general behaviour rules
      - Browser backend configuration note
      - Complete tool documentation with usage guidance
      - Response formatting rules (Telegram-compatible, no markdown tables)
      - Injected memory context (user preferences, facts, routines)
      - Active/recent background job summaries
      - Background-mode instructions (if running as a sub-agent)
    """
    # Pull the user's stored memories to inject into the prompt so the LLM
    # has context about preferences, routines, etc. without needing a tool call.
    memory_ctx = get_index_for_prompt()
    memory_section = f"\n\n## Your Memory\n{memory_ctx}" if memory_ctx else ""
    # Summarise any active or recent background jobs so the LLM can report
    # their status without making an extra tool call.
    job_ctx = jobs.context_summary(chat_id)
    job_section = f"\n\n## Active Or Recent Background Jobs\n{job_ctx}" if job_ctx else ""
    # When running as a background sub-agent, add extra instructions to
    # keep working until a concrete result is reached rather than stopping
    # at intermediate steps.
    background_section = (
        "\n\n## Background Job Mode\n"
        "You are running as a background sub-agent for a long-running task.\n"
        "Do not stop at an intermediate step like 'let me try again', 'one moment', or 'browser session reset'. "
        "Keep using tools until you either have a concrete result for the user or a specific blocker you can explain."
        if background_mode else ""
    )
    return f"""You are Hermes — an all-round personal assistant running on a private server for one user.
You are in the Toronto timezone (ET). You communicate via Telegram.
School support is only one part of your job. You are not primarily an academic bot.
{_browser_runtime_note()}

You have access to tools. You MUST call the appropriate tool whenever a request matches one. NEVER say "I can't do that" if a relevant tool exists. Here is what you can do:

## School — Sheridan Slate (D2L Brightspace)
The user is a Sheridan College student. You can check their school portal:
- **slate_check_assignments** — pending assignments, quizzes, discussions. Use `days_ahead` to filter (0=today, 7=this week). Call this when the user asks anything about what's due, homework, assignments, quizzes, or school work.
- **slate_get_assignment_details** — full instructions for a specific item by ID. Call after checking assignments.
- **slate_download_docs** — download attached files for an assignment.
- **slate_action_plan** — generate a study/work plan for an assignment.
- **slate_check_announcements** — course announcements. `days_back` controls how far to look.
- **slate_check_grades** — recent grades and scores.
- **slate_check_messages** — unread Slate messages/email.
- **slate_refresh** — force a fresh fetch from Slate (bypasses 5-minute cache).

## Reminders
- **set_reminder** — schedule a future reminder. `when` examples: "in 30 minutes", "tomorrow at 9am", "2026-03-25 14:00". `message` is what to remind about.
- **set_apple_reminder** — create a rich Apple Reminder in iCloud. Supports priority, urgent reminders, location text, people tags, subtasks, and choosing a reminder list.
- **list_apple_reminders** — show Apple Reminders from iCloud.
- **update_apple_reminder** — update an Apple Reminder by UID or unique title match.
- **delete_apple_reminder** — delete an Apple Reminder by UID or unique title match.
- **list_reminders** — show all pending reminders.
- **cancel_reminder** — cancel by number or text match.

## Apple Calendar
- **add_apple_calendar_event** — add an event to Apple Calendar / iCloud Calendar.
- **list_apple_calendar_events** — show upcoming Apple Calendar events.
- **update_apple_calendar_event** — update an Apple Calendar event by UID or unique title match.
- **delete_apple_calendar_event** — delete an Apple Calendar event by UID or unique title match.

## Computer Use
You can control a persistent browser session for web workflows:
- **browser_open** — open a URL in the browser.
- **browser_current_page** — show the current page title and URL.
- **browser_interactives** — list visible inputs, buttons, links, and suggested selectors on the current page.
- **browser_read** — read page text or extract text from a CSS selector.
- **browser_click** — click an element by CSS selector.
- **browser_type** — type into an element by CSS selector. For secrets already stored in the environment, pass `env:VAR_NAME` instead of the raw value.
- **browser_create_context** — create and save a reusable Browserbase context ID for persistent login state across sessions.
- **browser_screenshot** — save a screenshot of the current page.
- **browser_upload_file** — upload a server file into a file input on the current page.
- **browser_download** — download a file by clicking a selector or opening a direct download URL.
- **browser_login_status** — inspect the current page and report a best-effort login-state guess.
- **browser_reset** — force-close the browser session and kill leftover browser processes.

## Terminal
- **terminal_run** — run a bounded shell command on the EC2 host. Use this for files, processes, services, git, installs, or CLI tasks. It is non-interactive and kills child processes after completion or timeout.
- **service_status** — inspect a systemd service.
- **service_restart** — restart a systemd service when the user explicitly asks.
- **service_logs** — read recent journal logs for a service.

## Background Sub-Agents
Long-running browser or server tasks may be running in a separate background sub-agent so the main chat stays responsive:
- **list_background_jobs** — list current or recent background jobs for this chat.
- **background_job_status** — inspect one background job by latest/default, job ID, or prompt text.
- **cancel_background_job** — cancel a running background job.

## Tasks
- **add_task** — create a task with optional due time, priority, and notes.
- **list_tasks** — show open or completed tasks.
- **complete_task** — mark a task done.
- **reopen_task** — reopen a completed task.
- **delete_task** — delete a task.
- **task_from_slate** — convert a Slate item into a persistent task.

## Memory
You can remember things the user tells you across conversations:
- **remember** — save a fact/preference/context item with optional description and tags. Use memory types like `user`, `feedback`, `routine`, `contact`, `project`, or `note`.
- **recall** — search memories by keyword, optionally filtered by type.
- **list_memories** — show stored memories, optionally filtered by type.
- **forget** — delete a memory by name.

## Web Search
- **web_search** — search Google for any information. Use this for weather, news, current events, factual questions, or anything you're unsure about. `query` is the search terms.
- **hybrid_web_lookup** — try both web search and a direct browser check, then return both evidence streams. Use this for current, site-specific lookups like prices, flyers, menus, hours, retailer pages, or product availability.

## Utilities
- **get_current_time** — get the exact current date/time in Toronto ET.

## Response Rules
- Keep responses short and direct. No preamble or trailing suggestions.
- Never use markdown tables (| pipes) — Telegram can't render them. Use bullet lists.
- Present status/list outputs directly.
- Default to general assistant behavior unless the user is clearly asking about school/Slate.
- For planning/context tools like `slate_action_plan` and `slate_get_assignment_details`, use the tool output to write the final answer rather than echoing the raw prompt.
- Always call a tool when the user's intent matches one. Be proactive.
- For weather: call web_search with "weather [city] today".
- For "what's due": call slate_check_assignments.
- For "add this to my tasks" or "make a task": call add_task or task_from_slate.
- For Apple/iPhone/iCloud reminders: use set/list/update/delete Apple reminder tools as needed. A unique title match is okay; if there may be duplicates, list first.
- For Apple/iPhone/iCloud calendar requests: use add/list/update/delete Apple Calendar tools as needed. A unique title match is okay; if there may be duplicates, list first.
- For browser/computer-use tasks on websites: use the browser_* tools step by step. On unfamiliar pages, call browser_interactives before clicking or typing so you have reliable selectors. Use browser_login_status when auth state matters. If the browser gets stuck, call browser_reset before retrying. For passwords or codes already stored in the environment, use `browser_type` with `env:VAR_NAME` instead of typing the raw secret into the chat.
- For current shopping, grocery, product, price, flyer, menu, hours, or retailer-site lookups where freshness matters, prefer `hybrid_web_lookup` so you try both search and a direct page check before answering.
- For file flows on websites: use browser_upload_file and browser_download instead of vague instructions.
- For CLI/system/server tasks: use terminal_run instead of browser tools. Prefer service_status/service_logs/service_restart for systemd work.
- For "how's that going", "status", "progress", or "is it still running": check background jobs first with background_job_status or list_background_jobs.
- When a long-running browser/server workflow is already running in the background, report its status instead of starting over unless the user explicitly asks to retry or cancel.
- Use memory proactively for stable preferences, recurring people/places, course or project context, routines, service names, and workflow preferences when the user shares something likely to matter again.
- Before asking the user to repeat known preferences or context, check recall or rely on the injected memory section if it already covers it.
- For "what should I do next": check list_tasks and Slate before answering.
- For time: call get_current_time.{memory_section}{job_section}{background_section}"""


def _trim_history(chat_id: str) -> None:
    """
    Keep only the most recent MAX_HISTORY messages for a chat.
    This prevents unbounded token growth in long conversations.
    """
    hist = _history[chat_id]
    if len(hist) > MAX_HISTORY:
        # Keep system-level context: trim oldest non-system messages
        _history[chat_id] = hist[-MAX_HISTORY:]


def note_event(chat_id: str, text: str) -> None:
    """
    Inject an assistant-role message into the chat history without going
    through the LLM. Used by the background job system to record events
    (e.g. "sub-agent started", "sub-agent completed") so the main
    conversation has context about what happened.
    """
    if not text:
        return
    _history[chat_id].append({"role": "assistant", "content": text})
    _trim_history(chat_id)


def _tool_progress_label(fn_name: str) -> str:
    """
    Return a user-facing progress label for a tool call, shown as a
    status update in Telegram while the tool runs. Empty string means
    no status update is needed (for fast/quiet tools).
    """
    labels = {
        "browser_open": "Opening the page",
        "browser_current_page": "Checking the current page",
        "browser_interactives": "Inspecting interactive page elements",
        "browser_click": "Clicking on the page",
        "browser_type": "Typing into the page",
        "browser_read": "Reading the page",
        "browser_screenshot": "Capturing a screenshot",
        "browser_upload_file": "Uploading the file",
        "browser_download": "Downloading the file",
        "browser_login_status": "Checking login state",
        "browser_reset": "Resetting the browser session",
        "hybrid_web_lookup": "Searching and checking the site",
        "terminal_run": "Running the terminal command",
        "service_status": "Checking the service status",
        "service_restart": "Restarting the service",
        "service_logs": "Reading the service logs",
        "slate_refresh": "Refreshing Slate",
        "slate_download_docs": "Downloading assignment files",
        "web_search": "Searching the web",
    }
    return labels.get(fn_name, "")


def _inject_chat_context(fn_name: str, fn, args: dict, chat_id: str) -> dict:
    """
    Automatically inject the chat_id argument for tools that need it
    (e.g. background job tools). This avoids requiring the LLM to know
    or pass the internal chat ID.
    """
    params = {}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if fn_name in {"list_background_jobs", "background_job_status", "cancel_background_job"} or "chat_id" in params:
        args.setdefault("chat_id", chat_id)
    return args


async def _exec_tool(chat_id: str, tc, status_cb=None) -> tuple[str, str]:
    """
    Execute one tool call in a background thread.

    Returns (tool_call_id, result_str). The tool is run via asyncio.to_thread
    because most tools are synchronous (browser, terminal, CalDAV) and would
    block the event loop otherwise.
    """
    fn_name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    fn = TOOL_CALLABLES.get(fn_name)
    if not fn:
        return tc.id, f"Unknown tool: {fn_name}"
    args = _inject_chat_context(fn_name, fn, args, chat_id)
    label = _tool_progress_label(fn_name)
    if label:
        await _emit_status(status_cb, label)
    try:
        result = await asyncio.to_thread(fn, **args)
    except Exception as e:
        result = f"Tool error: {e}"
    log.debug(f"tool {fn_name} -> {str(result)[:120]}")
    return tc.id, str(result)


async def chat(chat_id: str, user_message: str, status_cb=None, background_mode: bool = False) -> str:
    """
    Process one user message; handles multi-step parallel tool calling.

    This is the main entry point for the agent. It:
      1. Appends the user message to chat history
      2. Builds the full message list (system prompt + history)
      3. Loops up to max_rounds times, calling the LLM and executing tools
      4. After force_text_after rounds, forces the LLM to produce a plain
         text response (no more tool calls) to prevent infinite loops
      5. Returns the final text response

    The force_text_after and max_rounds thresholds are higher in background
    mode to allow the sub-agent more steps for complex browser workflows.
    """
    _history[chat_id].append({"role": "user", "content": user_message})
    _trim_history(chat_id)

    messages = [{"role": "system", "content": _system_prompt(chat_id, background_mode=background_mode)}] + _history[chat_id]
    # Track which status messages have been sent to avoid duplicates.
    sent_statuses: set[str] = set()
    # Background agents get more tool-call rounds before being forced to
    # produce text, since browser workflows often need many steps.
    force_text_after = 6 if background_mode else 3
    max_rounds = 12 if background_mode else 10

    async def _status_once(text: str) -> None:
        """Send a status update only once (deduplicates repeated labels)."""
        if text in sent_statuses:
            return
        sent_statuses.add(text)
        await _emit_status(status_cb, text)

    for round_num in range(max_rounds):
        # After enough tool-call rounds, force the model to produce text
        # by omitting tools and adding an explicit instruction.
        force_text = round_num >= force_text_after
        send_messages = messages
        if force_text:
            send_messages = messages + [{
                "role": "system",
                "content": "Answer the user now based on the tool results above. No more tool calls.",
            }]

        response = await _call_with_fallback(send_messages, force_text=force_text, status_cb=_status_once)
        msg = response.choices[0].message

        # No tool calls: the model produced a final text response.
        if not msg.tool_calls:
            reply = msg.content or "(no response)"
            _history[chat_id].append({"role": "assistant", "content": reply})
            return reply

        # Strip 'index' from tool_calls — some providers (e.g. Venice) inject
        # an 'index' field that other providers reject on subsequent requests.
        assistant_msg = msg.model_dump(exclude_none=True)
        for tc in assistant_msg.get("tool_calls", []):
            tc.pop("index", None)
        messages.append(assistant_msg)

        # Execute tool calls sequentially rather than in parallel.
        # Browser/server tools are stateful (page state, session state),
        # so parallel execution can cause race conditions.
        results = []
        for tc in msg.tool_calls:
            results.append(await _exec_tool(chat_id, tc, status_cb=_status_once))

        # Append tool results to the message list for the next LLM round.
        for tool_call_id, result in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

    return "⚠️ Couldn't complete that. Try rephrasing."


def clear_history(chat_id: str) -> None:
    """Wipe the conversation history for a chat (used by /clear command)."""
    _history[chat_id] = []
