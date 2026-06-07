import os
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from db import init_db, save_response

load_dotenv()

LOGIN_URL = "https://chatgpt.com/auth/login"
CHAT_URL = "https://chatgpt.com/"


def login(page):
    page.goto(LOGIN_URL)
    page.click("button:has-text('Log in')")
    page.wait_for_selector("input[name='email']", timeout=15000)

    page.fill("input[name='email']", os.getenv("CHATGPT_EMAIL"))
    page.click("button[type='submit']")

    # Ждём поле для кода из письма (без пароля)
    page.wait_for_selector("input[name='code']", timeout=20000)
    print("\n>>> Код отправлен на почту. Проверьте письмо и введите код здесь:")
    code = input("Код из письма: ").strip()
    page.fill("input[name='code']", code)
    page.click("button[type='submit']")

    page.wait_for_url("https://chatgpt.com/**", timeout=30000)
    print("Авторизация успешна")


def send_prompt(page, prompt: str) -> str:
    page.goto(CHAT_URL)

    # Ждём поле ввода
    textarea = page.wait_for_selector("textarea#prompt-textarea", timeout=20000)
    textarea.click()
    textarea.fill(prompt)

    # Отправляем
    page.keyboard.press("Enter")

    # Ждём пока появится ответ и кнопка "стоп" исчезнет (генерация завершена)
    # Кнопка остановки генерации присутствует во время ответа
    page.wait_for_selector("button[data-testid='stop-button']", timeout=15000)
    page.wait_for_selector("button[data-testid='stop-button']", state="detached", timeout=120000)

    # Берём последний блок ответа ассистента
    messages = page.query_selector_all("[data-message-author-role='assistant']")
    if not messages:
        raise RuntimeError("No assistant response found")

    response_text = messages[-1].inner_text()
    return response_text.strip()


def run(prompts: list[str]):
    init_db()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headless=True для фонового режима
        context = browser.new_context()
        page = context.new_page()

        login(page)

        for prompt in prompts:
            print(f"\nSending: {prompt[:80]}...")
            try:
                response = send_prompt(page, prompt)
                print(f"Response received ({len(response)} chars)")
                save_response(prompt, response)
            except PlaywrightTimeout:
                print(f"Timeout waiting for response to: {prompt[:50]}")
            except Exception as e:
                print(f"Error: {e}")

        browser.close()


if __name__ == "__main__":
    questions = [
        "Что такое машинное обучение? Объясни в двух предложениях.",
        "Напиши функцию на Python для сортировки списка пузырьком.",
    ]
    run(questions)
