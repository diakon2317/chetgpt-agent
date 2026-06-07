import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

SESSION_FILE = "session.json"
LOGIN_URL = "https://chatgpt.com/auth/login"
CHAT_URL = "https://chatgpt.com/"

# Реальный user-agent чтобы не детектили headless
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class BrowserManager:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.lock = asyncio.Lock()
        self.logged_in = False

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        if Path(SESSION_FILE).exists():
            self._context = await self._browser.new_context(
                storage_state=SESSION_FILE,
                user_agent=USER_AGENT,
            )
            self._page = await self._context.new_page()
            self.logged_in = await self._check_session()
        else:
            self._context = await self._browser.new_context(user_agent=USER_AGENT)
            self._page = await self._context.new_page()

    async def _check_session(self) -> bool:
        await self._page.goto(CHAT_URL)
        try:
            await self._page.wait_for_selector("textarea#prompt-textarea", timeout=10000)
            return True
        except PlaywrightTimeout:
            return False

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(full_page=True)

    async def page_html(self) -> str:
        return await self._page.content()

    async def start_login(self):
        await self._page.goto(LOGIN_URL, wait_until="networkidle")

        # Кнопка "Log in" может отсутствовать — тогда email-поле уже видно
        try:
            await self._page.click("button:has-text('Log in')", timeout=5000)
        except PlaywrightTimeout:
            pass
        try:
            await self._page.click("a:has-text('Log in')", timeout=3000)
        except PlaywrightTimeout:
            pass

        await self._page.wait_for_selector("input[name='email']", timeout=20000)
        await self._page.fill("input[name='email']", os.getenv("CHATGPT_EMAIL"))
        await self._page.click("button[type='submit']")
        await self._page.wait_for_selector("input[name='code']", timeout=20000)

    async def verify_login(self, code: str):
        await self._page.fill("input[name='code']", code)
        await self._page.click("button[type='submit']")
        await self._page.wait_for_url("https://chatgpt.com/**", timeout=30000)
        await self._context.storage_state(path=SESSION_FILE)
        self.logged_in = True

    async def send_prompt(self, prompt: str) -> str:
        await self._page.goto(CHAT_URL)
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
