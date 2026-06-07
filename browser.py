import asyncio
import os
import time
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

SESSION_FILE = "session.json"
LOGIN_URL = "https://chatgpt.com/auth/login"
CHAT_URL = "https://chatgpt.com/"
DEBUG_DIR = Path("debug_screenshots")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Auth0 / OpenAI могут менять селекторы — пробуем все варианты
EMAIL_SELECTORS = [
    "input[name='username']",
    "input[name='email']",
    "input[type='email']",
    "input[id='email-input']",
    "input[id='username']",
]

CODE_SELECTORS = [
    "input[name='code']",
    "input[name='otp']",
    "input[autocomplete='one-time-code']",
    "input[id='code']",
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
            print(f"[DEBUG] Screenshot saved: {path}")
        except Exception as e:
            print(f"[DEBUG] Screenshot failed: {e}")

    async def _stealth_init(self):
        """Скрываем признаки headless/automation от Cloudflare и JS-детекторов."""
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1280,800",
            ],
        )
        if Path(SESSION_FILE).exists():
            self._context = await self._browser.new_context(
                storage_state=SESSION_FILE,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )
            await self._stealth_init()
            self._page = await self._context.new_page()
            self.logged_in = await self._check_session()
        else:
            self._context = await self._browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )
            await self._stealth_init()
            self._page = await self._context.new_page()

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

    async def _find_and_fill(self, selectors: list[str], value: str, label: str) -> bool:
        for sel in selectors:
            try:
                el = await self._page.wait_for_selector(sel, timeout=3000)
                await el.click()
                await el.fill(value)
                print(f"[AUTH] Filled {label} using selector: {sel}")
                return True
            except PlaywrightTimeout:
                continue
        return False

    async def start_login(self):
        print(f"[AUTH] Navigating to {LOGIN_URL}")
        await self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await self._save_screenshot("01_login_page")
        print(f"[AUTH] Current URL: {self._page.url}")

        # Нажимаем "Log in" если кнопка есть
        for selector in ["button:has-text('Log in')", "a:has-text('Log in')", "[data-testid='login-button']"]:
            try:
                await self._page.click(selector, timeout=4000)
                print(f"[AUTH] Clicked login button: {selector}")
                await asyncio.sleep(2)
                break
            except PlaywrightTimeout:
                continue

        await self._save_screenshot("02_after_login_click")
        print(f"[AUTH] URL after login click: {self._page.url}")

        # Ищем поле email по нескольким вариантам
        found = await self._find_and_fill(EMAIL_SELECTORS, os.getenv("CHATGPT_EMAIL", ""), "email")
        if not found:
            await self._save_screenshot("02_email_field_not_found")
            # Сохраним HTML для диагностики
            html = await self._page.content()
            (DEBUG_DIR / f"{_ts()}_page.html").write_text(html, encoding="utf-8")
            raise RuntimeError(
                f"Email input not found. URL: {self._page.url}. "
                f"Tried selectors: {EMAIL_SELECTORS}. "
                "Check debug_screenshots/ for screenshots and HTML."
            )

        await self._save_screenshot("03_email_filled")

        # Submit email
        await self._page.keyboard.press("Enter")
        await asyncio.sleep(2)
        await self._save_screenshot("04_after_email_submit")
        print(f"[AUTH] URL after email submit: {self._page.url}")

        # Ждём поле для кода
        code_found = False
        for sel in CODE_SELECTORS:
            try:
                await self._page.wait_for_selector(sel, timeout=20000)
                print(f"[AUTH] Code field found: {sel}")
                code_found = True
                break
            except PlaywrightTimeout:
                continue

        await self._save_screenshot("05_code_page")
        if not code_found:
            html = await self._page.content()
            (DEBUG_DIR / f"{_ts()}_code_page.html").write_text(html, encoding="utf-8")
            raise RuntimeError(
                f"Code input not found after email submit. URL: {self._page.url}. "
                "Check debug_screenshots/ — возможно, нужен пароль, а не код."
            )

    async def verify_login(self, code: str):
        found = await self._find_and_fill(CODE_SELECTORS, code, "code")
        if not found:
            raise RuntimeError("Code input field not found on page")

        await self._page.keyboard.press("Enter")
        await self._save_screenshot("06_code_submitted")

        try:
            await self._page.wait_for_url("https://chatgpt.com/**", timeout=30000)
        except PlaywrightTimeout:
            await self._save_screenshot("06_wait_url_timeout")
            raise RuntimeError(f"Did not reach chatgpt.com after code. URL: {self._page.url}")

        await self._context.storage_state(path=SESSION_FILE)
        self.logged_in = True
        await self._save_screenshot("07_logged_in")
        print("[AUTH] Login successful, session saved")

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
