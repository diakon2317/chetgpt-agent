import asyncio
import json
import os
import time
from pathlib import Path
from patchright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

SESSION_FILE = "session.json"
CHAT_URL = "https://chatgpt.com/"
DEBUG_DIR = Path("debug_screenshots")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


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
            print(f"[DEBUG] Screenshot saved: {path}")
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

    async def _check_session(self) -> bool:
        print("[AUTH] Checking saved session...")
        await self._page.goto(CHAT_URL, wait_until="domcontentloaded")
        await self._save_screenshot("session_check")
        try:
            await self._page.wait_for_selector("textarea#prompt-textarea", timeout=12000)
            print("[AUTH] Session valid — already logged in")
            return True
        except PlaywrightTimeout:
            print(f"[AUTH] Session expired or invalid (URL: {self._page.url})")
            return False

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(full_page=True)

    async def page_html(self) -> str:
        return await self._page.content()

    async def import_cookies(self, cookies: list) -> bool:
        """
        Accept cookies from Cookie Editor extension (JSON export) or
        any list of dicts with at least {name, value, domain}.
        Writes session.json and reloads the context.
        """
        def _samesite(v: str) -> str:
            return {"lax": "Lax", "strict": "Strict", "no_restriction": "None", "none": "None"}.get(
                (v or "").lower(), "Lax"
            )

        pw_cookies = []
        for c in cookies:
            pw_cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".chatgpt.com"),
                "path": c.get("path", "/"),
                "expires": float(c.get("expirationDate", c.get("expires", -1))),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": _samesite(c.get("sameSite", "lax")),
            })

        storage = {"cookies": pw_cookies, "origins": []}
        Path(SESSION_FILE).write_text(json.dumps(storage), encoding="utf-8")
        print(f"[AUTH] Saved {len(pw_cookies)} cookies to {SESSION_FILE}")

        # Reload context with new session
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()

        await self._make_context(storage_state=SESSION_FILE)
        self.logged_in = await self._check_session()
        return self.logged_in

    async def send_prompt(self, prompt: str) -> str:
        await self._page.goto(CHAT_URL, wait_until="domcontentloaded")
        textarea = await self._page.wait_for_selector("textarea#prompt-textarea", timeout=20000)
        await textarea.click()
        await textarea.fill(prompt)
        await self._page.keyboard.press("Enter")

        await self._page.wait_for_selector("button[data-testid='stop-button']", timeout=15000)
        await self._page.wait_for_selector(
            "button[data-testid='stop-button']", state="detached", timeout=120000
        )

        messages = await self._page.query_selector_all("[data-message-author-role='assistant']")
        if not messages:
            raise RuntimeError("No assistant response found")
        return (await messages[-1].inner_text()).strip()

    async def stop(self):
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
