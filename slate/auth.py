"""
Microsoft SSO auth for Sheridan Slate (D2L Brightspace).

Usage:
    python -m slate.auth            # interactive first-time login
    python -m slate.auth --check    # verify saved session is still valid

The session is saved to SLATE_SESSION_FILE (default: ~/.hermes/slate_session.json).
On EC2, run this once locally or with 'xvfb-run python -m slate.auth' and then
copy the session file to the server.

Architecture:
  Sheridan uses Microsoft SSO (Azure AD) for authentication. This means we
  cannot simply POST credentials — the user must complete an interactive
  browser flow (MFA, consent screens, etc.). Playwright opens a real Chromium
  window for this purpose.

  Once the SSO flow lands on the D2L home page, we capture the browser's
  storage state (cookies + local storage) and persist it to a JSON file.
  Subsequent API calls in client.py load these cookies into httpx, bypassing
  the need for a browser.

  The session typically lasts several hours but eventually expires. The
  --check flag uses a lightweight D2L whoami API call to verify validity
  without opening a browser.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Playwright is only needed for interactive login (done locally, not on EC2).
# Import lazily to avoid ModuleNotFoundError on headless servers where
# playwright is not installed (only httpx is needed for API calls).
try:
    from playwright.async_api import async_playwright, BrowserContext
except ImportError:
    async_playwright = None
    BrowserContext = None

# Base URL for Sheridan's Brightspace instance
SLATE_URL = os.getenv("SLATE_URL", "https://slate.sheridancollege.ca")

# Path to the persisted session file containing browser cookies
SESSION_FILE = Path(os.path.expanduser(os.getenv("SLATE_SESSION_FILE", "~/.hermes/slate_session.json")))


async def save_session(context: BrowserContext) -> None:
    """
    Persist the browser's full storage state (cookies, localStorage, etc.)
    to a JSON file so it can be reused by httpx-based API calls later.
    """
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    storage = await context.storage_state()
    SESSION_FILE.write_text(json.dumps(storage))
    print(f"Session saved → {SESSION_FILE}")


async def load_session(context: BrowserContext) -> bool:
    """
    Restore saved cookies into a Playwright browser context. Returns False
    if no session file exists. Used when resuming a previously authenticated
    browser session.
    """
    if not SESSION_FILE.exists():
        return False
    state = json.loads(SESSION_FILE.read_text())
    await context.add_cookies(state.get("cookies", []))
    return True


async def is_logged_in(_context=None) -> bool:
    """
    Quick check: hit the D2L whoami API endpoint using httpx with saved cookies.

    This avoids launching a full browser just to verify the session. The whoami
    endpoint returns the user's identifier on success (HTTP 200 with "Identifier"
    in the response body). Any failure (expired cookies, network error) returns False.
    """
    if not SESSION_FILE.exists():
        return False
    try:
        import httpx
        state = json.loads(SESSION_FILE.read_text())
        # Extract the hostname from SLATE_URL to filter cookies by domain
        host = SLATE_URL.split("//")[1].split("/")[0]
        cookies = {
            c["name"]: c["value"]
            for c in state.get("cookies", [])
            if host in c.get("domain", "")
        }
        async with httpx.AsyncClient(base_url=SLATE_URL, cookies=cookies, timeout=15, follow_redirects=True) as client:
            # /d2l/api/lp/1.0/users/whoami returns the current user's profile
            resp = await client.get("/d2l/api/lp/1.0/users/whoami")
            return resp.status_code == 200 and "Identifier" in resp.text
    except Exception:
        return False


async def interactive_login() -> None:
    """
    Opens a real browser window for the user to complete Microsoft SSO.
    After successful login, saves the session state.

    The browser must be headed (not headless) because the Microsoft SSO flow
    requires user interaction (password entry, MFA approval, etc.).
    slow_mo=100 adds a small delay between actions to avoid race conditions
    with Microsoft's login page transitions.
    """
    async with async_playwright() as p:
        # Must be headed for the user to interact with Microsoft SSO
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context()

        page = await context.new_page()
        print(f"\nOpening {SLATE_URL} — please log in with your Sheridan Microsoft account.")
        print("The browser will close automatically once you're in.\n")

        await page.goto(SLATE_URL)

        # Wait until we land on the D2L home page (user finished SSO).
        # The 5-minute timeout is generous to allow for slow MFA flows.
        try:
            await page.wait_for_url(f"{SLATE_URL}/d2l/home**", timeout=300_000)  # 5-min timeout
        except Exception:
            # Some D2L setups redirect differently (e.g. to a specific course);
            # accept any page under the Slate domain as a successful login.
            await page.wait_for_url(f"{SLATE_URL}/**", timeout=300_000)

        print("Login detected. Saving session...")
        await save_session(context)
        await browser.close()
        print("Done. You can now run the checker.")


async def check_session() -> bool:
    """Verify the saved session and print a human-readable status message."""
    ok = await is_logged_in()
    if ok:
        print("Session is valid.")
    else:
        print("Session expired — run 'python -m slate.auth' to re-authenticate.")
    return ok


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--check" in sys.argv:
        valid = asyncio.run(check_session())
        # Exit code 0 = valid, 1 = expired (useful for scripting)
        sys.exit(0 if valid else 1)
    else:
        asyncio.run(interactive_login())
