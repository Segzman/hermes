import os
import unittest
from importlib import reload
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


class FakePage:
    def __init__(self):
        self.prompt_dismissed = False
        self.prompt_clicks = 0
        self.fills = []
        self.url = "https://www.amazon.ca/"
        self.closed = False

    def title(self):
        return "Amazon.ca"

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, *args, **kwargs):
        return None

    def set_default_timeout(self, timeout):
        return None

    def goto(self, url, wait_until=None):
        self.url = url

    def locator(self, selector):
        return FakeLocator(self, selector)

    def evaluate(self, script, max_items):
        return [
            {
                "tag": "input",
                "role": "",
                "label": "Search Amazon.ca",
                "selector": "#twotabsearchtextbox",
            },
            {
                "tag": "button",
                "role": "",
                "label": "Continue shopping",
                "selector": 'button:has-text("Continue shopping")',
            },
        ][:max_items]


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        if "Continue shopping" in self.selector and not self.page.prompt_dismissed:
            return 1
        if self.selector == "#search":
            return 1
        return 0

    def wait_for(self, state=None, timeout=None):
        if self.selector == "#search" and not self.page.prompt_dismissed:
            raise RuntimeError("blocked by interstitial")
        return None

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self, timeout=None):
        if "Continue shopping" in self.selector:
            self.page.prompt_dismissed = True
            self.page.prompt_clicks += 1
        return None

    def press(self, key, timeout=None):
        return None

    def fill(self, text, timeout=None):
        if not self.page.prompt_dismissed:
            raise RuntimeError("input still blocked")
        self.page.fills.append((self.selector, text))
        return None


class FakeContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.closed = False

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True

    def cookies(self, urls):
        return []


class FakeBrowser:
    def __init__(self, contexts=None):
        self.contexts = list(contexts or [])
        self.closed = False

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser=None):
        self.browser = browser
        self.connected_url = None

    def connect_over_cdp(self, url):
        self.connected_url = url
        return self.browser


class FailoverChromium(FakeChromium):
    def __init__(self, browser=None):
        super().__init__(browser=browser)
        self.connect_calls = []

    def connect_over_cdp(self, url):
        self.connect_calls.append(url)
        if "connect.browser-use.com" in url:
            raise RuntimeError("browser-use unavailable")
        self.connected_url = url
        return self.browser


class FakeStartedPlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeSyncPlaywright:
    def __init__(self, started_playwright):
        self.started_playwright = started_playwright

    def start(self):
        return self.started_playwright


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ComputerHelpersTests(unittest.TestCase):
    def test_validate_url_allows_http_and_https(self):
        from bot.computer import _validate_url

        self.assertEqual(_validate_url("https://example.com"), "https://example.com")
        self.assertEqual(_validate_url("http://example.com/path"), "http://example.com/path")

    def test_validate_url_rejects_non_web_schemes(self):
        from bot.computer import _validate_url

        with self.assertRaisesRegex(RuntimeError, "Only http and https"):
            _validate_url("javascript:alert(1)")

    def test_pick_best_search_result_prefers_requested_domain(self):
        import bot.tools as tools

        tools = reload(tools)
        items = [
            {"title": "Other", "snippet": "", "link": "https://example.com/item"},
            {"title": "Food Basics", "snippet": "", "link": "https://www.foodbasics.ca/flyer"},
        ]

        picked = tools._pick_best_search_result(items, preferred_domain="foodbasics.ca")
        self.assertEqual(picked["link"], "https://www.foodbasics.ca/flyer")


class ComputerToolWrapperTests(unittest.TestCase):
    def setUp(self):
        import bot.tools as tools

        self.tools = reload(tools)

    def test_browser_open_calls_backend_directly(self):
        with patch("bot.computer.open_url", return_value="opened") as open_mock:
            result = self.tools.browser_open("https://example.com")

        open_mock.assert_called_once_with("https://example.com")
        self.assertEqual(result, "opened")

    def test_browser_type_passes_press_enter(self):
        with patch("bot.computer.type_text", return_value="typed") as type_mock:
            result = self.tools.browser_type("#search", "Hermes", press_enter=True)

        type_mock.assert_called_once_with("#search", "Hermes", press_enter=True)
        self.assertEqual(result, "typed")

    def test_browser_create_context_formats_success(self):
        with patch("bot.computer.create_browserbase_context", return_value="ctx_123") as context_mock:
            result = self.tools.browser_create_context()

        context_mock.assert_called_once_with(save_to_env=True)
        self.assertIn("ctx_123", result)

    def test_browser_screenshot_formats_saved_path(self):
        with patch("bot.computer.take_screenshot", return_value="/tmp/browser.png") as shot_mock:
            result = self.tools.browser_screenshot(full_page=False)

        shot_mock.assert_called_once_with(full_page=False)
        self.assertIn("/tmp/browser.png", result)

    def test_browser_upload_file_formats_success(self):
        with patch("bot.computer.upload_file", return_value='Uploaded `/tmp/a.txt` into `input[type=file]`.') as upload_mock:
            result = self.tools.browser_upload_file("input[type=file]", "/tmp/a.txt")

        upload_mock.assert_called_once_with(selector="input[type=file]", file_path="/tmp/a.txt")
        self.assertIn("Uploaded", result)

    def test_browser_download_formats_saved_path(self):
        with patch("bot.computer.download", return_value="/tmp/report.pdf") as download_mock:
            result = self.tools.browser_download(selector="a.download")

        download_mock.assert_called_once_with(selector="a.download", url="")
        self.assertIn("/tmp/report.pdf", result)

    def test_browser_login_status_calls_backend(self):
        with patch("bot.computer.login_status", return_value="Likely signed in.") as status_mock:
            result = self.tools.browser_login_status()

        status_mock.assert_called_once_with()
        self.assertIn("Likely signed in", result)

    def test_browser_interactives_calls_backend(self):
        with patch("bot.computer.list_interactives", return_value="Visible interactive elements") as interactive_mock:
            result = self.tools.browser_interactives(max_items=10)

        interactive_mock.assert_called_once_with(max_items=10)
        self.assertIn("Visible interactive elements", result)

    def test_browser_reset_calls_backend(self):
        with patch("bot.computer.reset_browser") as reset_mock:
            result = self.tools.browser_reset()

        reset_mock.assert_called_once_with(force_kill=True)
        self.assertIn("cleaned up", result)

    def test_hybrid_web_lookup_combines_search_and_browser(self):
        search_items = [
            {
                "title": "Food Basics flyer",
                "snippet": "Fresh weekly deals",
                "link": "https://www.foodbasics.ca/flyer",
            }
        ]
        with patch("bot.tools._search_items", return_value=search_items), \
             patch("bot.computer.open_url", return_value="Browser page:\nTitle: Flyer\nURL: https://www.foodbasics.ca/flyer") as open_mock, \
             patch("bot.computer.read_page", return_value="Rice, eggs, tofu") as read_mock:
            result = self.tools.hybrid_web_lookup("food basics flyer", preferred_domain="foodbasics.ca")

        open_mock.assert_called_once_with("https://www.foodbasics.ca/flyer")
        read_mock.assert_called_once()
        self.assertIn("Search results:", result)
        self.assertIn("Direct browser check:", result)
        self.assertIn("Rice, eggs, tofu", result)

    def test_hybrid_web_lookup_reports_browser_failure(self):
        search_items = [
            {
                "title": "Food Basics flyer",
                "snippet": "Fresh weekly deals",
                "link": "https://www.foodbasics.ca/flyer",
            }
        ]
        with patch("bot.tools._search_items", return_value=search_items), \
             patch("bot.computer.open_url", side_effect=RuntimeError("blocked")):
            result = self.tools.hybrid_web_lookup("food basics flyer", preferred_domain="foodbasics.ca")

        self.assertIn("Search results:", result)
        self.assertIn("Direct browser check failed: blocked", result)

    def test_terminal_run_formats_output(self):
        with patch(
            "bot.terminal.run_command",
            return_value={
                "command": "pwd",
                "cwd": "/home/ubuntu/hermes",
                "exit_code": 0,
                "timed_out": False,
                "output": "/home/ubuntu/hermes",
            },
        ) as run_mock:
            result = self.tools.terminal_run("pwd")

        run_mock.assert_called_once_with(command="pwd", cwd="", timeout_seconds=20)
        self.assertIn("Status: exit 0", result)
        self.assertIn("/home/ubuntu/hermes", result)

    def test_service_status_formats_output(self):
        with patch(
            "bot.terminal.service_status",
            return_value={"exit_code": 0, "output": "active (running)"},
        ) as status_mock:
            result = self.tools.service_status("hermes-bot.service")

        status_mock.assert_called_once_with("hermes-bot.service")
        self.assertIn("active (running)", result)

    def test_service_restart_formats_output(self):
        with patch(
            "bot.terminal.service_restart",
            return_value={"timed_out": False, "exit_code": 0, "output": "active"},
        ) as restart_mock:
            result = self.tools.service_restart("hermes-bot.service")

        restart_mock.assert_called_once_with("hermes-bot.service")
        self.assertIn("Restarted service", result)

    def test_service_logs_formats_output(self):
        with patch(
            "bot.terminal.tail_logs",
            return_value={"output": "line1\nline2"},
        ) as logs_mock:
            result = self.tools.service_logs("hermes-bot.service", lines=50)

        logs_mock.assert_called_once_with("hermes-bot.service", lines=50)
        self.assertIn("line1", result)


class ComputerInteractionRecoveryTests(unittest.TestCase):
    def test_type_text_retries_after_prompt_dismissal(self):
        from bot import computer

        page = FakePage()
        with patch("bot.computer._ensure_page_locked", return_value=page), \
             patch("bot.computer._touch_session_locked"), \
             patch("bot.computer._reset_browser_locked") as reset_mock:
            result = computer.type_text("#search", "30W charger", press_enter=True)

        self.assertIn("Typed into `#search`", result)
        self.assertEqual(page.prompt_clicks, 1)
        self.assertEqual(page.fills, [("#search", "30W charger")])
        reset_mock.assert_not_called()

    def test_list_interactives_formats_visible_elements(self):
        from bot import computer

        page = FakePage()
        with patch("bot.computer._ensure_page_locked", return_value=page), \
             patch("bot.computer._touch_session_locked"), \
             patch("bot.computer._reset_browser_locked") as reset_mock:
            result = computer.list_interactives(max_items=5)

        self.assertIn("#twotabsearchtextbox", result)
        self.assertIn("Continue shopping", result)
        reset_mock.assert_not_called()

    def test_type_text_reads_secret_from_env_reference(self):
        from bot import computer

        page = FakePage()
        page.prompt_dismissed = True
        with patch.dict(os.environ, {"TEST_BROWSER_SECRET": "s3cr3t"}, clear=False), \
             patch("bot.computer._ensure_page_locked", return_value=page), \
             patch("bot.computer._touch_session_locked"), \
             patch("bot.computer._reset_browser_locked") as reset_mock:
            result = computer.type_text("#search", "env:TEST_BROWSER_SECRET", press_enter=False)

        self.assertIn("Typed into `#search`", result)
        self.assertEqual(page.fills, [("#search", "s3cr3t")])
        reset_mock.assert_not_called()


class BrowserbaseBackendTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "BROWSER_BACKEND": "browserbase",
                "BROWSERBASE_API_KEY": "bb_test_key",
                "BROWSERBASE_PROJECT_ID": "proj_test",
                "BROWSERBASE_REGION": "ca",
                "BROWSERBASE_CONTEXT_ID": "ctx_test",
                "BROWSERBASE_KEEP_ALIVE": "true",
                "BROWSERBASE_PROXIES": "true",
                "BROWSERBASE_ADVANCED_STEALTH": "true",
            },
            clear=False,
        )
        self.env_patcher.start()
        import bot.computer as computer

        self.computer = reload(computer)

    def tearDown(self):
        with patch("bot.computer.httpx.post", return_value=FakeResponse({})):
            self.computer.reset_browser(force_kill=False)
        self.env_patcher.stop()

    def test_open_url_uses_browserbase_session_and_cdp(self):
        page = FakePage()
        context = FakeContext([page])
        browser = FakeBrowser([context])
        chromium = FakeChromium(browser=browser)
        started_playwright = FakeStartedPlaywright(chromium=chromium)

        with patch("bot.computer.sync_playwright", return_value=FakeSyncPlaywright(started_playwright)), \
             patch("bot.computer.httpx.post", return_value=FakeResponse({"id": "sess_123", "connectUrl": "wss://browserbase.example/connect"})) as post_mock, \
             patch("bot.computer._touch_session_locked"), \
             patch("bot.computer._dismiss_common_prompts_locked", return_value=""):
            result = self.computer.open_url("https://example.com")

        self.assertIn("https://example.com", result)
        self.assertEqual(chromium.connected_url, "wss://browserbase.example/connect")
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["projectId"], "proj_test")
        self.assertEqual(payload["region"], "ca")
        self.assertTrue(payload["keepAlive"])
        self.assertTrue(payload["proxies"])
        self.assertEqual(payload["browserSettings"]["context"]["id"], "ctx_test")
        self.assertTrue(payload["browserSettings"]["context"]["persist"])
        self.assertTrue(payload["browserSettings"]["advancedStealth"])

    def test_reset_browser_releases_keep_alive_session(self):
        page = FakePage()
        context = FakeContext([page])
        browser = FakeBrowser([context])
        started_playwright = FakeStartedPlaywright(chromium=FakeChromium(browser=browser))

        self.computer._page = page
        self.computer._context = context
        self.computer._browser = browser
        self.computer._playwright = started_playwright
        self.computer._browser_session = {"id": "sess_release"}
        self.computer._active_backend = "browserbase"

        with patch("bot.computer.httpx.post", return_value=FakeResponse({})) as post_mock:
            self.computer.reset_browser(force_kill=False)

        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.args[0], "https://api.browserbase.com/v1/sessions/sess_release")
        self.assertEqual(post_mock.call_args.kwargs["json"]["status"], "REQUEST_RELEASE")
        self.assertEqual(post_mock.call_args.kwargs["json"]["projectId"], "proj_test")
        self.assertTrue(browser.closed)
        self.assertTrue(started_playwright.stopped)

    def test_keep_alive_requires_project_id(self):
        with patch.dict(os.environ, {"BROWSERBASE_PROJECT_ID": ""}, clear=False):
            import bot.computer as computer

            reloaded = reload(computer)
            with self.assertRaisesRegex(RuntimeError, "requires BROWSERBASE_PROJECT_ID"):
                reloaded._browserbase_session_payload()

    def test_create_context_saves_id_to_env(self):
        with patch("bot.computer.httpx.post", return_value=FakeResponse({"id": "ctx_saved"})), \
             patch("bot.computer._write_env_var") as write_mock:
            context_id = self.computer.create_browserbase_context(save_to_env=True)

        self.assertEqual(context_id, "ctx_saved")
        write_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.args[1], "BROWSERBASE_CONTEXT_ID")
        self.assertEqual(write_mock.call_args.args[2], "ctx_saved")


class BrowserUseBackendTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "BROWSER_BACKEND": "browser-use",
                "BROWSER_FALLBACK_BACKEND": "browserbase",
                "BROWSER_USE_API_KEY": "bu_test_key",
                "BROWSER_USE_PROFILE_ID": "profile_test",
                "BROWSER_USE_TIMEOUT_MINUTES": "12",
                "BROWSERBASE_API_KEY": "bb_test_key",
                "BROWSERBASE_PROJECT_ID": "proj_test",
            },
            clear=False,
        )
        self.env_patcher.start()
        import bot.computer as computer

        self.computer = reload(computer)

    def tearDown(self):
        with patch("bot.computer.httpx.post", return_value=FakeResponse({})):
            self.computer.reset_browser(force_kill=False)
        self.env_patcher.stop()

    def test_open_url_uses_browser_use_primary_backend(self):
        page = FakePage()
        context = FakeContext([page])
        browser = FakeBrowser([context])
        chromium = FakeChromium(browser=browser)
        started_playwright = FakeStartedPlaywright(chromium=chromium)

        with patch("bot.computer.sync_playwright", return_value=FakeSyncPlaywright(started_playwright)), \
             patch("bot.computer._touch_session_locked"), \
             patch("bot.computer._dismiss_common_prompts_locked", return_value=""):
            result = self.computer.open_url("https://example.com")

        self.assertIn("https://example.com", result)
        parsed = urlparse(chromium.connected_url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "wss")
        self.assertEqual(parsed.netloc, "connect.browser-use.com")
        self.assertEqual(params["apiKey"], ["bu_test_key"])
        self.assertEqual(params["profileId"], ["profile_test"])
        self.assertEqual(params["timeout"], ["12"])
        self.assertEqual(params["browserScreenWidth"], [str(self.computer.BROWSER_VIEWPORT_WIDTH)])
        self.assertEqual(params["browserScreenHeight"], [str(self.computer.BROWSER_VIEWPORT_HEIGHT)])

    def test_browser_use_falls_back_to_browserbase(self):
        page = FakePage()
        context = FakeContext([page])
        browser = FakeBrowser([context])
        chromium = FailoverChromium(browser=browser)
        started_playwright = FakeStartedPlaywright(chromium=chromium)

        with patch("bot.computer.sync_playwright", return_value=FakeSyncPlaywright(started_playwright)), \
             patch("bot.computer.httpx.post", return_value=FakeResponse({"id": "sess_123", "connectUrl": "wss://browserbase.example/connect"})) as post_mock, \
             patch("bot.computer._touch_session_locked"), \
             patch("bot.computer._dismiss_common_prompts_locked", return_value=""):
            result = self.computer.open_url("https://example.com")

        self.assertIn("https://example.com", result)
        self.assertEqual(len(chromium.connect_calls), 2)
        self.assertIn("connect.browser-use.com", chromium.connect_calls[0])
        self.assertEqual(chromium.connect_calls[1], "wss://browserbase.example/connect")
        self.assertEqual(post_mock.call_args.args[0], "https://api.browserbase.com/v1/sessions")


if __name__ == "__main__":
    unittest.main()
