import asyncio
import json
import os
import time
from pathlib import Path
from patchright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

SESSION_FILE = "session.json"
CHAT_URL = "https://chatgpt.com/"
DEBUG_DIR = Path("debug_screenshots")

USER_AGENT = os.getenv(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36",
)

CHAT_SELECTORS = [
    "div#prompt-textarea",
    "textarea#prompt-textarea",
    "[data-testid='send-button']",
]


def _ts() -> str:
    return str(int(time.time()))


class BrowserManager:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.lock = asyncio.Lock()
        self.logged_in = False
        DEBUG_DIR.mkdir(exist_ok=True)

    async def _save_screenshot(self, name: str):
        try:
            path = DEBUG_DIR / f"{_ts()}_{name}.png"
            await self._page.screenshot(path=str(path), full_page=True)
            print(f"[DEBUG] Screenshot: {path}")
        except Exception as e:
            print(f"[DEBUG] Screenshot failed: {e}")

    async def _make_context(self, storage_state=None):
        kwargs = dict(user_agent=USER_AGENT, viewport={"width": 1280, "height": 800})
        if storage_state:
            kwargs["storage_state"] = storage_state
        self._context = await self._browser.new_context(**kwargs)
        self._page = await self._context.new_page()

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--lang=en-US"],
        )
        if Path(SESSION_FILE).exists():
            await self._make_context(storage_state=SESSION_FILE)
            self.logged_in = await self._check_session()
        else:
            await self._make_context()

    async def _handle_cf(self) -> bool:
        """
        Try to get past Cloudflare challenge.
        Attempts auto-solve wait, then tries clicking the Turnstile checkbox.
        Returns True if CF is no longer blocking.
        """
        for attempt in range(4):
            title = await self._page.title()
            url = self._page.url
            print(f"[CF] Attempt {attempt+1}: title='{title}', url={url}")

            if "just a moment" not in title.lower():
                return True

            await self._save_screenshot(f"cf_attempt_{attempt+1}")

            # Try clicking the Turnstile checkbox inside the CF iframe
            try:
                cf_el = await self._page.wait_for_selector(
                    "iframe[src*='challenges.cloudflare.com']", timeout=3000
                )
                cf_frame = await cf_el.content_frame()
                if cf_frame:
                    cb = await cf_frame.wait_for_selector("input[type='checkbox']", timeout=2000)
                    await cb.click()
                    print(f"[CF] Clicked Turnstile checkbox")
            except Exception as e:
                print(f"[CF] Checkbox not found: {e}")

            await asyncio.sleep(8)

        title = await self._page.title()
        return "just a moment" not in title.lower()

    async def _navigate_and_wait(self, url: str) -> bool:
        """Navigate to URL and handle CF challenge. Returns True if chat UI is reachable."""
        await self._page.goto(url, wait_until="domcontentloaded")
        cf_passed = await self._handle_cf()
        if not cf_passed:
            await self._save_screenshot("nav_cf_blocked")
            return False
        return True

    async def _check_session(self) -> bool:
        print("[AUTH] Checking saved session...")
        ok = await self._navigate_and_wait(CHAT_URL)
        if not ok:
            print("[AUTH] Session check failed — CF blocked")
            return False

        for sel in CHAT_SELECTORS:
            try:
                await self._page.wait_for_selector(sel, timeout=8000)
                print(f"[AUTH] Session valid ({sel})")
                return True
            except PlaywrightTimeout:
                continue

        print(f"[AUTH] Session check failed — no chat UI. URL: {self._page.url}")
        return False

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(full_page=True)

    async def page_html(self) -> str:
        return await self._page.content()

    async def import_cookies(self, cookies: list) -> bool:
        def _samesite(v: str) -> str:
            return {"lax": "Lax", "strict": "Strict", "no_restriction": "None", "none": "None"}.get(
                (v or "").lower(), "Lax"
            )

        pw_cookies = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".chatgpt.com"),
                "path": c.get("path", "/"),
                "expires": float(c.get("expirationDate", c.get("expires", -1))),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": _samesite(c.get("sameSite", "lax")),
            }
            for c in cookies
        ]

        await self._context.add_cookies(pw_cookies)
        print(f"[AUTH] Added {len(pw_cookies)} cookies to context")

        await self._context.storage_state(path=SESSION_FILE)
        print(f"[AUTH] Session saved to {SESSION_FILE}")

        self.logged_in = True
        return True

    async def send_prompt(self, prompt: str, chat_id: str | None = None) -> tuple[str, str | None]:
        """
        Send a prompt to ChatGPT.
        If chat_id is given, continues that conversation.
        Returns (response_text, chat_id).
        """
        target_url = f"https://chatgpt.com/c/{chat_id}" if chat_id else CHAT_URL

        ok = await self._navigate_and_wait(target_url)
        if not ok:
            self.logged_in = False
            raise RuntimeError("Cloudflare is blocking. Re-import cookies via POST /login/cookies.")

        try:
            textarea = await self._page.wait_for_selector(
                "textarea#prompt-textarea, div#prompt-textarea", timeout=20000
            )
        except PlaywrightTimeout:
            title = await self._page.title()
            await self._save_screenshot("no_textarea")
            raise RuntimeError(f"Chat input not found. Title: '{title}', URL: {self._page.url}")

        await textarea.click()
        await textarea.fill(prompt)
        await self._page.keyboard.press("Enter")

        # Wait for generation to start
        await self._page.wait_for_selector("button[data-testid='stop-button']", timeout=15000)

        # Wait for generation to finish (stop → send button)
        await self._page.wait_for_selector("button[data-testid='send-button']", timeout=90000)

        messages = await self._page.query_selector_all("[data-message-author-role='assistant']")
        if not messages:
            raise RuntimeError("No assistant response found")

        response_text = (await messages[-1].inner_text()).strip()

        # Extract chat_id from URL  (https://chatgpt.com/c/<uuid>)
        current_url = self._page.url
        new_chat_id = current_url.split("/c/")[-1].split("?")[0] if "/c/" in current_url else chat_id

        return response_text, new_chat_id

    async def stop(self):
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
