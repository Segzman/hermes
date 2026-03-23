"""
Browser-first computer use for Hermes via Playwright.

The browser stays alive briefly so the agent can do multi-step web tasks,
then Hermes tears it down aggressively and kills any leftover processes.

Architecture notes:
  - Three browser backends are supported: local Playwright, Browser Use
    (cloud browser API), and Browserbase (cloud browser with persistent
    contexts). The backend is selected by BROWSER_BACKEND env var.
  - A fallback chain tries each configured backend in order, so if the
    primary cloud service is unavailable, the bot degrades to local
    Playwright automatically.
  - The browser session is kept alive for BROWSER_IDLE_TIMEOUT_SECONDS
    between tool calls, allowing multi-step workflows (open, inspect,
    click, type, read) without restarting the browser each time.
  - An absolute session cap (BROWSER_MAX_SESSION_SECONDS) prevents
    runaway sessions from consuming resources indefinitely.
  - All browser operations are thread-safe via a reentrant lock (_lock)
    because the agent calls browser tools from asyncio.to_thread.
  - Stale sessions (idle timeout or max session exceeded) are cleaned up
    by a daemon timer thread that fires periodically.
  - Common website prompts (cookie consent, "Continue shopping", etc.)
    are auto-dismissed to reduce friction in automated workflows.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from dotenv import load_dotenv
import httpx

load_dotenv()

# Graceful import: Playwright is optional so the bot can still run
# other features if it is not installed.
try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable (truthy values: 1, true, yes, on)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_bool_if_set(name: str) -> bool | None:
    """
    Read a boolean env var, returning None if unset.

    This distinguishes "not configured" (None) from "explicitly false",
    which matters for Browserbase settings where omitting a field and
    sending false have different semantics.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_json(name: str):
    """Parse an environment variable as JSON, returning None if unset."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be valid JSON.") from exc


def _normalize_backend(value: str, default: str = "local") -> str:
    """Map various backend name aliases to canonical identifiers."""
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


# ── Configuration from environment ──────────────────────────────────────────

# Path to the .env file, used for persisting BROWSERBASE_CONTEXT_ID
BROWSER_ENV_FILE = Path(os.path.expanduser(os.getenv("HERMES_ENV_FILE", str(Path(__file__).resolve().parent.parent / ".env"))))
# Local browser profile directory for persistent cookies/storage
BROWSER_PROFILE_DIR = Path(os.path.expanduser(os.getenv("BROWSER_PROFILE_DIR", "~/.hermes/browser-profile")))
# Where screenshots are saved on disk
BROWSER_SCREENSHOT_DIR = Path(os.path.expanduser(os.getenv("BROWSER_SCREENSHOT_DIR", "~/hermes-screenshots")))
# Where browser downloads are saved on disk
BROWSER_DOWNLOAD_DIR = Path(os.path.expanduser(os.getenv("BROWSER_DOWNLOAD_DIR", "~/hermes-downloads")))
# Whether to run the browser headless (no visible window). True on servers.
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").strip().lower() != "false"
# Primary and fallback browser backends
BROWSER_BACKEND = _normalize_backend(os.getenv("BROWSER_BACKEND", "local"), default="local")
BROWSER_FALLBACK_BACKEND = _normalize_backend(os.getenv("BROWSER_FALLBACK_BACKEND", ""), default="")
# Viewport dimensions for consistent rendering
BROWSER_VIEWPORT_WIDTH = int(os.getenv("BROWSER_VIEWPORT_WIDTH", "1440"))
BROWSER_VIEWPORT_HEIGHT = int(os.getenv("BROWSER_VIEWPORT_HEIGHT", "900"))
# Default timeout for Playwright operations (element waits, clicks, etc.)
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "15000"))
# Extra settle time after page navigation to let JS frameworks render
BROWSER_SETTLE_MS = max(0, int(os.getenv("BROWSER_SETTLE_MS", "1200")))
# How long to keep a browser session alive between tool calls
BROWSER_IDLE_TIMEOUT_SECONDS = max(1, int(os.getenv("BROWSER_IDLE_TIMEOUT_SECONDS", "20")))
# Absolute maximum session lifetime (even if actively used)
BROWSER_MAX_SESSION_SECONDS = max(BROWSER_IDLE_TIMEOUT_SECONDS, int(os.getenv("BROWSER_MAX_SESSION_SECONDS", "120")))

# ── Browser Use cloud backend settings ──────────────────────────────────────
BROWSER_USE_CONNECT_BASE = os.getenv("BROWSER_USE_CONNECT_BASE", "wss://connect.browser-use.com").strip().rstrip("/")
BROWSER_USE_API_KEY = os.getenv("BROWSER_USE_API_KEY", "").strip()
BROWSER_USE_PROFILE_ID = os.getenv("BROWSER_USE_PROFILE_ID", "").strip()
BROWSER_USE_PROXY_COUNTRY_CODE = os.getenv("BROWSER_USE_PROXY_COUNTRY_CODE", "").strip().lower()
# Session timeout in minutes (clamped to 1-240 range)
BROWSER_USE_TIMEOUT_MINUTES = max(1, min(240, int(os.getenv("BROWSER_USE_TIMEOUT_MINUTES", str(max(1, (BROWSER_MAX_SESSION_SECONDS + 59) // 60))))))

# ── Browserbase cloud backend settings ──────────────────────────────────────
BROWSERBASE_API_BASE = os.getenv("BROWSERBASE_API_BASE", "https://api.browserbase.com").strip().rstrip("/")
BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY", "").strip()
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "").strip()
BROWSERBASE_REGION = os.getenv("BROWSERBASE_REGION", "").strip()
# Persistent context ID for maintaining login state across sessions
BROWSERBASE_CONTEXT_ID = os.getenv("BROWSERBASE_CONTEXT_ID", "").strip()
BROWSERBASE_EXTENSION_ID = os.getenv("BROWSERBASE_EXTENSION_ID", "").strip()

# ── Global browser session state (protected by _lock) ──────────────────────

_lock = threading.RLock()  # Reentrant lock for thread safety across tool calls
_cleanup_timer: threading.Timer | None = None  # Idle cleanup timer
_playwright = None      # Playwright instance
_browser = None         # Browser instance (only set for CDP-connected backends)
_context = None         # Browser context (persistent for local, default for cloud)
_page = None            # Current active page
_browser_session = None # Cloud backend session metadata (e.g. Browserbase session dict)
_active_backend = ""    # Which backend is currently running
_last_used_at = 0.0     # Monotonic timestamp of last tool call
_session_started_at = 0.0  # Monotonic timestamp of session creation

# Common website prompts to auto-dismiss. These cover cookie consent banners,
# "continue shopping" overlays on Amazon, and GDPR acceptance dialogs.
COMMON_PROMPT_SELECTORS = (
    ('#sp-cc-accept', "Continue shopping"),
    ('button:has-text("Continue shopping")', "Continue shopping"),
    ('input[type="submit"][value="Continue shopping"]', "Continue shopping"),
    ('button:has-text("Accept all")', "Accept all"),
    ('button:has-text("Accept")', "Accept"),
    ('button:has-text("I agree")', "I agree"),
    ('button:has-text("Got it")', "Got it"),
    ('button:has-text("Continue")', "Continue"),
)


def _require_playwright() -> None:
    """Raise if Playwright is not installed."""
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed in this environment.")


def _configured_backend_chain() -> list[str]:
    """
    Build the ordered list of backends to try.

    Starts with the primary backend, then the explicit fallback (if any),
    then implicit fallbacks based on what credentials are available.
    For example, if browser-use is primary but has no Browserbase key,
    local Playwright is added as an implicit fallback.
    """
    chain = [BROWSER_BACKEND or "local"]
    if BROWSER_FALLBACK_BACKEND and BROWSER_FALLBACK_BACKEND not in chain:
        chain.append(BROWSER_FALLBACK_BACKEND)
    # Add implicit fallbacks so we always have a viable option
    if len(chain) == 1 and chain[0] == "browser-use":
        implicit_fallback = "browserbase" if BROWSERBASE_API_KEY else "local"
        if implicit_fallback not in chain:
            chain.append(implicit_fallback)
    elif len(chain) == 1 and chain[0] == "browserbase" and "local" not in chain:
        chain.append("local")
    return chain


def _is_browser_use_backend(backend: str) -> bool:
    """Check if a backend string normalises to Browser Use."""
    return _normalize_backend(backend, default="") == "browser-use"


def _is_browserbase_backend(backend: str) -> bool:
    """Check if a backend string normalises to Browserbase."""
    return _normalize_backend(backend, default="") == "browserbase"


def _backend_label(backend: str) -> str:
    """Return a human-readable label for a backend identifier."""
    normalized = _normalize_backend(backend, default="local")
    if normalized == "browser-use":
        return "Browser Use"
    if normalized == "browserbase":
        return "Browserbase"
    return "local Playwright"


def _require_browser_use() -> None:
    """Raise if Browser Use API key is not configured."""
    if not BROWSER_USE_API_KEY:
        raise RuntimeError("Browser Use backend requires BROWSER_USE_API_KEY.")


def _require_browserbase() -> None:
    """Raise if Browserbase API key is not configured."""
    if not BROWSERBASE_API_KEY:
        raise RuntimeError("Browserbase backend requires BROWSERBASE_API_KEY.")


def _validate_url(url: str) -> str:
    """Validate that a URL uses http/https and has a valid host."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Only http and https URLs are allowed.")
    if not parsed.netloc:
        raise RuntimeError("URL is missing a valid host.")
    return url


def _resolve_secret_text(text: str) -> str:
    """
    Resolve an "env:VAR_NAME" reference to its environment variable value.

    This allows the agent to type passwords/secrets into browser fields
    without exposing the raw value in the conversation. If the text does
    not start with "env:", it is returned unchanged.
    """
    value = (text or "").strip()
    if not value.startswith("env:"):
        return text
    env_name = value[4:].strip()
    if not env_name:
        raise RuntimeError("Secret env reference is missing a variable name. Use env:MY_PASSWORD.")
    secret = os.getenv(env_name)
    if secret is None:
        raise RuntimeError(f"Environment variable `{env_name}` is not set.")
    return secret


def _write_env_var(path: Path, name: str, value: str) -> None:
    """
    Write or update a KEY=VALUE line in a .env file.

    If the key already exists, its value is updated in place.
    If not, a new line is appended. This is used to persist the
    BROWSERBASE_CONTEXT_ID after creating a new Browserbase context.
    """
    path = path.expanduser()
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{name}="):
            new_lines.append(f"{name}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        # Ensure there is a blank line separator before the new entry
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append(f"{name}={value}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _screenshot_path() -> Path:
    """Generate a timestamped file path for a browser screenshot."""
    BROWSER_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return BROWSER_SCREENSHOT_DIR / f"browser_{stamp}.png"


def _download_path(filename: str) -> Path:
    """
    Generate a file path for a browser download.

    Appends a timestamp suffix if a file with the same name already exists
    to avoid overwriting previous downloads.
    """
    BROWSER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(filename or "download.bin").name or "download.bin"
    target = BROWSER_DOWNLOAD_DIR / name
    if not target.exists():
        return target
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return target.with_name(f"{target.stem}_{stamp}{target.suffix}")


def _resolve_upload_path(file_path: str) -> Path:
    """
    Resolve and validate a file path for browser upload.

    Handles ~ expansion and relative paths (resolved against cwd).
    Raises RuntimeError if the file does not exist or is not a regular file.
    """
    path = Path(os.path.expanduser((file_path or "").strip()))
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists():
        raise RuntimeError(f"Upload file does not exist: {path}")
    if not path.is_file():
        raise RuntimeError(f"Upload path is not a file: {path}")
    return path


def _browser_patterns() -> list[str]:
    """
    Return process-matching patterns used to find and kill leftover
    browser processes from local Playwright sessions.
    """
    profile = str(BROWSER_PROFILE_DIR)
    return [
        f"--user-data-dir={profile}",
        f"chrome-headless-shell.*{profile}",
        f"chromium.*{profile}",
        "playwright/driver/package/cli.js run-driver",
    ]


def _kill_browser_processes() -> None:
    """
    Aggressively kill any browser processes associated with the local
    Playwright profile. This runs pkill for each known process pattern.

    Failures are silently suppressed since the processes may already be dead.
    """
    for pattern in _browser_patterns():
        with contextlib.suppress(Exception):
            subprocess.run(
                ["pkill", "-f", pattern],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _cancel_cleanup_locked() -> None:
    """Cancel the pending idle-cleanup timer (must be called with _lock held)."""
    global _cleanup_timer
    if _cleanup_timer is not None:
        _cleanup_timer.cancel()
        _cleanup_timer = None


def _touch_session_locked() -> None:
    """
    Mark the session as recently used and reset the idle cleanup timer.

    Must be called with _lock held. This is called after every successful
    tool operation to extend the session's life.
    """
    global _last_used_at, _cleanup_timer
    _last_used_at = time.monotonic()
    _cancel_cleanup_locked()
    # Schedule a cleanup check after the idle timeout
    timer = threading.Timer(BROWSER_IDLE_TIMEOUT_SECONDS, _idle_cleanup)
    timer.daemon = True
    timer.start()
    _cleanup_timer = timer


def _session_is_stale_locked() -> bool:
    """
    Check if the current session should be torn down.

    A session is stale if:
      - It has been idle for >= BROWSER_IDLE_TIMEOUT_SECONDS, OR
      - It has been alive for >= BROWSER_MAX_SESSION_SECONDS (absolute cap)
    """
    if _page is None or _session_started_at <= 0:
        return False
    now = time.monotonic()
    return (
        (now - _last_used_at) >= BROWSER_IDLE_TIMEOUT_SECONDS
        or (now - _session_started_at) >= BROWSER_MAX_SESSION_SECONDS
    )


def _best_effort_close(obj, method_name: str) -> None:
    """Call a close/stop method on an object, suppressing all exceptions."""
    if obj is None:
        return
    method = getattr(obj, method_name, None)
    if callable(method):
        with contextlib.suppress(Exception):
            method()


def _browserbase_session_payload() -> dict:
    """
    Build the JSON payload for creating a Browserbase session.

    This assembles all optional Browserbase features (proxies, captcha
    solving, ad blocking, stealth mode, persistent context, etc.) from
    environment variables into the session creation request.
    """
    keep_alive = _env_bool("BROWSERBASE_KEEP_ALIVE", False)
    if keep_alive and not BROWSERBASE_PROJECT_ID:
        raise RuntimeError("Browserbase keep-alive requires BROWSERBASE_PROJECT_ID so Hermes can release sessions.")

    payload = {
        "timeout": max(60, int(os.getenv("BROWSERBASE_TIMEOUT_SECONDS", str(max(60, BROWSER_MAX_SESSION_SECONDS))))),
        "browserSettings": {
            "viewport": {
                "width": BROWSER_VIEWPORT_WIDTH,
                "height": BROWSER_VIEWPORT_HEIGHT,
            },
        },
    }
    if BROWSERBASE_PROJECT_ID:
        payload["projectId"] = BROWSERBASE_PROJECT_ID
    if keep_alive:
        payload["keepAlive"] = True
    if BROWSERBASE_REGION:
        payload["region"] = BROWSERBASE_REGION
    if BROWSERBASE_EXTENSION_ID:
        payload["extensionId"] = BROWSERBASE_EXTENSION_ID

    # Optional JSON-encoded fields from env
    proxies = _env_json("BROWSERBASE_PROXIES")
    if proxies is not None:
        payload["proxies"] = proxies

    user_metadata = _env_json("BROWSERBASE_USER_METADATA")
    if user_metadata is not None:
        payload["userMetadata"] = user_metadata

    # Boolean browser settings (only included when explicitly set)
    browser_settings = payload["browserSettings"]
    for env_name, key in (
        ("BROWSERBASE_BLOCK_ADS", "blockAds"),
        ("BROWSERBASE_SOLVE_CAPTCHAS", "solveCaptchas"),
        ("BROWSERBASE_RECORD_SESSION", "recordSession"),
        ("BROWSERBASE_LOG_SESSION", "logSession"),
        ("BROWSERBASE_ADVANCED_STEALTH", "advancedStealth"),
    ):
        value = _env_bool_if_set(env_name)
        if value is not None:
            browser_settings[key] = value

    browser_os = os.getenv("BROWSERBASE_OS", "").strip()
    if browser_os:
        browser_settings["os"] = browser_os

    captcha_image_selector = os.getenv("BROWSERBASE_CAPTCHA_IMAGE_SELECTOR", "").strip()
    if captcha_image_selector:
        browser_settings["captchaImageSelector"] = captcha_image_selector

    captcha_input_selector = os.getenv("BROWSERBASE_CAPTCHA_INPUT_SELECTOR", "").strip()
    if captcha_input_selector:
        browser_settings["captchaInputSelector"] = captcha_input_selector

    # Persistent context allows login cookies to survive across sessions
    if BROWSERBASE_CONTEXT_ID:
        browser_settings["context"] = {
            "id": BROWSERBASE_CONTEXT_ID,
            "persist": _env_bool("BROWSERBASE_CONTEXT_PERSIST", True),
        }

    return payload


def _browserbase_headers() -> dict[str, str]:
    """Return HTTP headers for Browserbase API requests."""
    _require_browserbase()
    return {
        "Content-Type": "application/json",
        "X-BB-API-Key": BROWSERBASE_API_KEY,
    }


def _browserbase_create_session() -> dict:
    """Create a new Browserbase session and return the session metadata dict."""
    response = httpx.post(
        f"{BROWSERBASE_API_BASE}/v1/sessions",
        headers=_browserbase_headers(),
        json=_browserbase_session_payload(),
        timeout=20.0,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("connectUrl"):
        raise RuntimeError("Browserbase session did not return a connectUrl.")
    return data


def create_browserbase_context(save_to_env: bool = True, env_path: str = "") -> str:
    """
    Create a new Browserbase persistent context and optionally save its
    ID to the .env file.

    A persistent context preserves cookies, localStorage, and other
    browser state across separate Browserbase sessions, enabling login
    persistence.
    """
    global BROWSERBASE_CONTEXT_ID
    _require_browserbase()
    if not BROWSERBASE_PROJECT_ID:
        raise RuntimeError("Browserbase context creation requires BROWSERBASE_PROJECT_ID.")

    response = httpx.post(
        f"{BROWSERBASE_API_BASE}/v1/contexts",
        headers=_browserbase_headers(),
        json={"projectId": BROWSERBASE_PROJECT_ID},
        timeout=20.0,
    )
    response.raise_for_status()
    data = response.json()
    context_id = (data.get("id") or "").strip()
    if not context_id:
        raise RuntimeError("Browserbase context creation did not return an id.")

    # Update both the module global and the process environment
    BROWSERBASE_CONTEXT_ID = context_id
    os.environ["BROWSERBASE_CONTEXT_ID"] = context_id

    if save_to_env:
        target = Path(env_path).expanduser() if env_path else BROWSER_ENV_FILE
        _write_env_var(target, "BROWSERBASE_CONTEXT_ID", context_id)

    return context_id


def _browserbase_release_session(session_id: str) -> None:
    """
    Release a Browserbase keep-alive session so it does not consume
    resources after Hermes is done with it.

    Only applies when BROWSERBASE_KEEP_ALIVE is enabled.
    """
    if not session_id:
        return
    if not _env_bool("BROWSERBASE_KEEP_ALIVE", False):
        return
    payload = {"status": "REQUEST_RELEASE"}
    if BROWSERBASE_PROJECT_ID:
        payload["projectId"] = BROWSERBASE_PROJECT_ID
    with contextlib.suppress(Exception):
        response = httpx.post(
            f"{BROWSERBASE_API_BASE}/v1/sessions/{session_id}",
            headers=_browserbase_headers(),
            json=payload,
            timeout=20.0,
        )
        response.raise_for_status()


def _browser_use_connect_url() -> str:
    """
    Build the WebSocket URL for connecting to a Browser Use session.

    Parameters like viewport size, timeout, and optional profile/proxy
    are encoded into the query string.
    """
    _require_browser_use()
    params = {
        "apiKey": BROWSER_USE_API_KEY,
        "timeout": BROWSER_USE_TIMEOUT_MINUTES,
        "browserScreenWidth": BROWSER_VIEWPORT_WIDTH,
        "browserScreenHeight": BROWSER_VIEWPORT_HEIGHT,
    }
    if BROWSER_USE_PROFILE_ID:
        params["profileId"] = BROWSER_USE_PROFILE_ID
    if BROWSER_USE_PROXY_COUNTRY_CODE:
        params["proxyCountryCode"] = BROWSER_USE_PROXY_COUNTRY_CODE
    # Handle base URLs that might already have query parameters
    separator = "&" if "?" in BROWSER_USE_CONNECT_BASE else "?"
    return f"{BROWSER_USE_CONNECT_BASE}{separator}{urlencode(params)}"


def _start_backend_locked(playwright, backend: str):
    """
    Start a specific browser backend and return its components.

    Returns: (backend_name, browser_obj, context_obj, session_metadata)

    For cloud backends (Browser Use, Browserbase), connects via CDP
    (Chrome DevTools Protocol) over WebSocket. For local, launches a
    persistent Chromium context with the local profile directory.
    """
    normalized = _normalize_backend(backend, default="local")
    if _is_browser_use_backend(normalized):
        connect_url = _browser_use_connect_url()
        browser = playwright.chromium.connect_over_cdp(connect_url)
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("Browser Use did not expose a default browser context.")
        return normalized, browser, contexts[0], {"connectUrl": connect_url}

    if _is_browserbase_backend(normalized):
        browser_session = _browserbase_create_session()
        browser = playwright.chromium.connect_over_cdp(browser_session["connectUrl"])
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("Browserbase did not expose a default browser context.")
        return normalized, browser, contexts[0], browser_session

    if normalized == "local":
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # Kill any zombie processes from a previous crash
        _kill_browser_processes()

        launch_args = ["--disable-dev-shm-usage"]
        # --no-sandbox is needed in CI or when no display server is available
        if os.getenv("CI") or os.getenv("DISPLAY", "") == "":
            launch_args.append("--no-sandbox")

        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=BROWSER_HEADLESS,
            args=launch_args,
            viewport={"width": BROWSER_VIEWPORT_WIDTH, "height": BROWSER_VIEWPORT_HEIGHT},
        )
        return normalized, None, context, {}

    raise RuntimeError(f"Unknown browser backend: {backend}")


def _reset_browser_locked(force_kill: bool = True) -> None:
    """
    Tear down the current browser session completely.

    Must be called with _lock held. Clears all global state, closes
    browser objects in reverse order (page -> context -> browser ->
    playwright), and kills leftover processes for local backends.
    For Browserbase, releases the keep-alive session.
    """
    global _playwright, _browser, _context, _page, _browser_session, _active_backend, _last_used_at, _session_started_at
    # Save references before clearing globals (for cleanup)
    page = _page
    context = _context
    browser = _browser
    playwright = _playwright
    browser_session = _browser_session or {}
    active_backend = _active_backend
    # Clear all globals immediately
    _page = None
    _context = None
    _browser = None
    _playwright = None
    _browser_session = None
    _active_backend = ""
    _last_used_at = 0.0
    _session_started_at = 0.0
    _cancel_cleanup_locked()

    # Kill local browser processes before closing (prevents zombie processes)
    if force_kill and not _is_browserbase_backend(active_backend) and not _is_browser_use_backend(active_backend):
        _kill_browser_processes()
    # Close objects in reverse creation order
    _best_effort_close(page, "close")
    _best_effort_close(context, "close")
    _best_effort_close(browser, "close")
    _best_effort_close(playwright, "stop")
    # Release Browserbase session if applicable
    if _is_browserbase_backend(active_backend):
        _browserbase_release_session(browser_session.get("id", ""))


def _idle_cleanup() -> None:
    """
    Timer callback that checks if the browser session is stale and
    tears it down if so. If the session is still active, reschedules
    the timer.
    """
    with _lock:
        if _page is None:
            return
        if not _session_is_stale_locked():
            _touch_session_locked()
            return
        _reset_browser_locked(force_kill=True)


def reset_browser(force_kill: bool = True) -> None:
    """Public API to force-close the browser session."""
    with _lock:
        _reset_browser_locked(force_kill=force_kill)


def _ensure_page_locked():
    """
    Ensure a browser page is available, starting a new session if needed.

    Must be called with _lock held. Handles:
      - Reusing the existing page if it is still alive and not stale
      - Tearing down stale sessions before starting a new one
      - Trying each backend in the fallback chain
      - Setting up page defaults (timeout, viewport)
    """
    global _playwright, _browser, _context, _page, _browser_session, _active_backend, _session_started_at
    _require_playwright()

    # Reuse existing page if still alive and fresh
    if _page is not None:
        if _page.is_closed() or _session_is_stale_locked():
            _reset_browser_locked(force_kill=True)
        else:
            _touch_session_locked()
            return _page

    try:
        _playwright = sync_playwright().start()
        errors: list[str] = []
        # Try each backend in the configured chain
        for backend in _configured_backend_chain():
            browser = None
            context = None
            browser_session = {}
            try:
                active_backend, browser, context, browser_session = _start_backend_locked(_playwright, backend)
                _active_backend = active_backend
                _browser = browser
                _context = context
                _browser_session = browser_session
                break
            except Exception as exc:
                errors.append(f"{_backend_label(backend)}: {exc}")
                _best_effort_close(context, "close")
                _best_effort_close(browser, "close")
                if _is_browserbase_backend(backend):
                    _browserbase_release_session(browser_session.get("id", ""))
        else:
            # All backends failed
            joined = " | ".join(errors) if errors else "No browser backend was configured."
            raise RuntimeError(f"Unable to start a browser session. {joined}")

        # Use the last page in the context (or create one if none exists)
        pages = list(_context.pages)
        _page = pages[-1] if pages else _context.new_page()
        _page.set_default_timeout(BROWSER_TIMEOUT_MS)
        _session_started_at = time.monotonic()
        _touch_session_locked()
        return _page
    except Exception:
        _reset_browser_locked(force_kill=True)
        raise


def _current_page_locked(page) -> str:
    """Build a status string showing the current page title and URL."""
    title = (page.title() or "").strip() or "(untitled)"
    url = page.url or "(no page loaded)"
    return f"Browser page:\nTitle: {title}\nURL: {url}"


def _wait_for_page_ready(page, timeout_ms: int = 5000) -> None:
    """
    Wait for the page to finish loading after navigation or interaction.

    Uses a two-stage approach: first wait for DOM content, then wait for
    network idle. The extra settle time (BROWSER_SETTLE_MS) accounts for
    JavaScript frameworks that render after networkidle.
    """
    with contextlib.suppress(PlaywrightTimeoutError, Exception):
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    with contextlib.suppress(PlaywrightTimeoutError, Exception):
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    if BROWSER_SETTLE_MS:
        with contextlib.suppress(Exception):
            page.wait_for_timeout(BROWSER_SETTLE_MS)


def _dismiss_common_prompts_locked(page) -> str:
    """
    Try to dismiss common website prompts (cookie consent, etc.).

    Iterates through COMMON_PROMPT_SELECTORS and clicks the first one
    that is visible. Returns the label of the dismissed prompt, or empty
    string if none were found.
    """
    for selector, label in COMMON_PROMPT_SELECTORS:
        with contextlib.suppress(Exception):
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.wait_for(state="visible", timeout=min(2500, BROWSER_TIMEOUT_MS))
            with contextlib.suppress(Exception):
                locator.scroll_into_view_if_needed(timeout=1500)
            locator.click(timeout=min(3000, BROWSER_TIMEOUT_MS))
            _wait_for_page_ready(page, timeout_ms=min(5000, BROWSER_TIMEOUT_MS))
            return label
    return ""


def _run_with_prompt_retry(page, action):
    """
    Run an action, and if it fails, try dismissing a common prompt first
    then retry. This handles the case where a cookie banner or overlay
    is blocking the target element.
    """
    try:
        return action()
    except Exception:
        dismissed = _dismiss_common_prompts_locked(page)
        if not dismissed:
            raise
        return action()


def _visible_locator(page, selector: str):
    """
    Find the first visible element matching a CSS selector, wait for it
    to be visible, and scroll it into view.
    """
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=BROWSER_TIMEOUT_MS)
    with contextlib.suppress(Exception):
        locator.scroll_into_view_if_needed(timeout=1500)
    return locator


def _login_status_text(page) -> str:
    """
    Inspect the current page and produce a best-effort login status guess.

    Heuristic approach: checks for common logged-in indicators ("sign out",
    "my account", "dashboard") and login-page indicators ("sign in",
    "log in") in the page title, URL, and body text.
    """
    title = (page.title() or "").strip() or "(untitled)"
    url = page.url or "(no page loaded)"
    cookies = []
    if _context is not None and url.startswith(("http://", "https://")):
        with contextlib.suppress(Exception):
            cookies = _context.cookies([url])
    body_text = ""
    with contextlib.suppress(Exception):
        body_text = " ".join(page.locator("body").inner_text().split())[:2500]
    blob = f"{title}\n{url}\n{body_text}".lower()

    # Heuristic login detection based on common UI patterns
    status = "Login status unclear."
    if any(token in blob for token in ("sign out", "log out", "logout", "my account", "dashboard")):
        status = "Likely signed in."
    elif any(token in blob for token in ("sign in", "log in", "login", "authenticate")):
        status = "Likely on a login page or not signed in."

    lines = [
        status,
        f"Title: {title}",
        f"URL: {url}",
        f"Cookies for page: {len(cookies)}",
    ]
    if body_text:
        lines.extend(["", body_text[:800]])
    return "\n".join(lines)


# ── Public browser tool implementations ──────────────────────────────────────
# Each function acquires _lock, ensures a page exists, performs the action,
# and resets the browser on any exception to avoid a corrupted session.

def open_url(url: str) -> str:
    """Navigate to a URL and return the page title/URL."""
    target = _validate_url(url)
    with _lock:
        page = _ensure_page_locked()
        try:
            page.goto(target, wait_until="domcontentloaded")
            _wait_for_page_ready(page, timeout_ms=min(5000, BROWSER_TIMEOUT_MS))
            _dismiss_common_prompts_locked(page)
            _touch_session_locked()
            return _current_page_locked(page)
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def current_page() -> str:
    """Return the current page title and URL."""
    with _lock:
        page = _ensure_page_locked()
        try:
            _dismiss_common_prompts_locked(page)
            _touch_session_locked()
            return _current_page_locked(page)
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def click(selector: str) -> str:
    """Click the first visible element matching a CSS selector."""
    with _lock:
        page = _ensure_page_locked()
        try:
            def _click_once():
                locator = _visible_locator(page, selector)
                locator.click(timeout=BROWSER_TIMEOUT_MS)
                _wait_for_page_ready(page, timeout_ms=min(5000, BROWSER_TIMEOUT_MS))
                return _current_page_locked(page)

            result = _run_with_prompt_retry(page, _click_once)
            _touch_session_locked()
            return result
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def type_text(selector: str, text: str, press_enter: bool = False) -> str:
    """
    Type text into an input field.

    Supports "env:VAR_NAME" syntax for typing secrets without exposing them.
    Selects all existing text before typing to replace it (Ctrl+A then fill).
    """
    with _lock:
        page = _ensure_page_locked()
        try:
            text_value = _resolve_secret_text(text)

            def _type_once():
                locator = _visible_locator(page, selector)
                locator.click(timeout=BROWSER_TIMEOUT_MS)
                # Select all existing text before typing to replace it
                with contextlib.suppress(Exception):
                    locator.press("ControlOrMeta+A", timeout=1500)
                locator.fill(text_value, timeout=BROWSER_TIMEOUT_MS)
                if press_enter:
                    locator.press("Enter", timeout=2000)
                    _wait_for_page_ready(page, timeout_ms=min(5000, BROWSER_TIMEOUT_MS))
                return f'Typed into `{selector}`.'

            result = _run_with_prompt_retry(page, _type_once)
            _touch_session_locked()
            return result
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def read_page(selector: str = "", max_items: int = 20, max_chars: int = 4000) -> str:
    """
    Read text from the current page.

    If a CSS selector is provided, extracts text from matching elements
    (numbered list format). Otherwise, returns the full page body text
    (collapsed whitespace, truncated to max_chars).
    """
    with _lock:
        page = _ensure_page_locked()
        try:
            _dismiss_common_prompts_locked(page)
            if selector:
                locator = page.locator(selector)
                count = locator.count()
                if count == 0:
                    return f'No elements matched `{selector}`.'
                lines = []
                for idx in range(min(count, max(1, max_items))):
                    text = (locator.nth(idx).inner_text() or "").strip()
                    if text:
                        lines.append(f"{idx + 1}. {text}")
                _touch_session_locked()
                if not lines:
                    return f'Elements matched `{selector}`, but they had no readable text.'
                return "\n".join(lines)[:max_chars]

            title = (page.title() or "").strip() or "(untitled)"
            body = page.locator("body").inner_text()
            # Collapse whitespace for cleaner output
            body = " ".join(body.split())
            if len(body) > max_chars:
                body = body[: max_chars - 3] + "..."
            _touch_session_locked()
            return f"{title}\n{page.url}\n\n{body}"
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def list_interactives(max_items: int = 25) -> str:
    """
    List visible interactive elements (inputs, buttons, links) on the
    current page with suggested CSS selectors.

    Uses a JavaScript evaluation to scan the DOM for interactive elements,
    generate best-effort selectors, and return them as a numbered list.
    The agent uses these selectors for subsequent click/type operations.
    """
    with _lock:
        page = _ensure_page_locked()
        try:
            _dismiss_common_prompts_locked(page)
            # JavaScript runs in the browser context to enumerate visible
            # interactive elements and generate CSS selectors for each.
            items = page.evaluate(
                """
                (maxItems) => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    if (!style || style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const esc = (value) => String(value || "").replace(/\\\\/g, "\\\\\\\\").replace(/"/g, '\\\\\\"');
                  const shortText = (value) => String(value || "").trim().replace(/\\s+/g, " ").slice(0, 60);
                  const makeSelector = (el) => {
                    const tag = el.tagName.toLowerCase();
                    if (el.id) return `#${CSS.escape(el.id)}`;
                    const text = shortText(el.innerText || el.value || "");
                    if ((tag === "button" || tag === "a") && text) return `${tag}:has-text("${esc(text)}")`;
                    const aria = el.getAttribute("aria-label");
                    if (aria) return `${tag}[aria-label="${esc(aria)}"]`;
                    const placeholder = el.getAttribute("placeholder");
                    if (placeholder) return `${tag}[placeholder="${esc(placeholder)}"]`;
                    const name = el.getAttribute("name");
                    if (name) return `${tag}[name="${esc(name)}"]`;
                    const type = el.getAttribute("type");
                    if (type) return `${tag}[type="${esc(type)}"]`;
                    return tag;
                  };
                  const describe = (el) => {
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute("role") || "";
                    const label = shortText(
                      el.getAttribute("aria-label") ||
                      el.getAttribute("placeholder") ||
                      el.getAttribute("name") ||
                      el.innerText ||
                      el.value ||
                      ""
                    );
                    return {
                      tag,
                      role,
                      label: label || "(no label)",
                      selector: makeSelector(el),
                    };
                  };
                  const seen = new Set();
                  const priority = (tag) => {
                    if (tag === "input" || tag === "textarea" || tag === "select") return 0;
                    if (tag === "button") return 1;
                    return 2;
                  };
                  const nodes = Array.from(document.querySelectorAll('input, textarea, select, button, a[href], [role="button"]'))
                    .filter(visible)
                    .map(describe)
                    .sort((a, b) => priority(a.tag) - priority(b.tag))
                    .filter((item) => {
                      const key = `${item.selector}::${item.label}`;
                      if (seen.has(key)) return false;
                      seen.add(key);
                      return true;
                    })
                    .slice(0, maxItems);
                  return nodes;
                }
                """,
                max(1, min(int(max_items), 50)),
            )
            _touch_session_locked()
            if not items:
                return "No visible interactive elements found."
            lines = ["Visible interactive elements:\n"]
            for idx, item in enumerate(items, start=1):
                role = f" role={item['role']}" if item.get("role") else ""
                lines.append(
                    f"{idx}. <{item['tag']}{role}> {item['label']}\n"
                    f"   selector: `{item['selector']}`"
                )
            return "\n".join(lines)
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def take_screenshot(full_page: bool = True) -> str:
    """Take a screenshot of the current page and return the file path."""
    with _lock:
        page = _ensure_page_locked()
        try:
            _dismiss_common_prompts_locked(page)
            path = _screenshot_path()
            page.screenshot(path=str(path), full_page=full_page)
            _touch_session_locked()
            return str(path)
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def upload_file(selector: str, file_path: str) -> str:
    """Upload a local file into a file input element on the page."""
    upload_path = _resolve_upload_path(file_path)
    with _lock:
        page = _ensure_page_locked()
        try:
            def _upload_once():
                locator = _visible_locator(page, selector)
                locator.set_input_files(str(upload_path), timeout=BROWSER_TIMEOUT_MS)
                return f'Uploaded `{upload_path}` into `{selector}`.'

            result = _run_with_prompt_retry(page, _upload_once)
            _touch_session_locked()
            return result
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def download(selector: str = "", url: str = "") -> str:
    """
    Download a file through the browser.

    Either clicks a selector that triggers a download, or navigates to
    a direct download URL. Returns the local file path where the download
    was saved.
    """
    with _lock:
        page = _ensure_page_locked()
        try:
            with page.expect_download(timeout=BROWSER_TIMEOUT_MS) as download_info:
                if selector:
                    def _click_once():
                        locator = _visible_locator(page, selector)
                        locator.click(timeout=BROWSER_TIMEOUT_MS)
                    _run_with_prompt_retry(page, _click_once)
                elif url:
                    page.goto(_validate_url(url), wait_until="domcontentloaded")
                    _wait_for_page_ready(page, timeout_ms=min(5000, BROWSER_TIMEOUT_MS))
                else:
                    raise RuntimeError("Provide a selector or URL for browser download.")
            item = download_info.value
            target = _download_path(item.suggested_filename)
            item.save_as(str(target))
            _touch_session_locked()
            return str(target)
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise


def login_status() -> str:
    """Inspect the current page and report a best-effort login state guess."""
    with _lock:
        page = _ensure_page_locked()
        try:
            _dismiss_common_prompts_locked(page)
            _touch_session_locked()
            return _login_status_text(page)
        except Exception:
            _reset_browser_locked(force_kill=True)
            raise
