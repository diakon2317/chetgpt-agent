import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from browser import BrowserManager
from db import init_db, save_response, get_history

load_dotenv()

browser = BrowserManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await browser.start()
    yield
    await browser.stop()


app = FastAPI(title="ChatGPT Proxy API", lifespan=lifespan)


class AskRequest(BaseModel):
    prompt: str
    chat_id: str | None = None


class CookiesRequest(BaseModel):
    cookies: list


@app.get("/health")
async def health():
    return {"status": "ok", "logged_in": browser.logged_in}


@app.post("/login/cookies")
async def import_cookies(req: CookiesRequest):
    """
    Import cookies exported from Cookie Editor browser extension.
    Bypasses Cloudflare by reusing an existing authenticated browser session.
    """
    try:
        await browser.import_cookies(req.cookies)
        return {"message": "Cookies imported. Use /ask to verify the session works."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/ask")
async def ask(req: AskRequest):
    """
    Send a prompt to ChatGPT.
    Pass chat_id to continue an existing conversation.
    Returns response and chat_id (use it in the next request to continue the chat).
    """
    if not browser.logged_in:
        raise HTTPException(status_code=401, detail="Not logged in. Import cookies via POST /login/cookies first.")
    async with browser.lock:
        try:
            response, chat_id = await browser.send_prompt(req.prompt, req.chat_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    save_response(req.prompt, response)
    return {"response": response, "chat_id": chat_id}


@app.get("/history")
async def history(limit: int = 20):
    return {"items": get_history(limit)}


@app.get("/debug/screenshot")
async def debug_screenshot():
    img = await browser.screenshot()
    return Response(content=img, media_type="image/png")


@app.get("/debug/html")
async def debug_html():
    html = await browser.page_html()
    return Response(content=html, media_type="text/html")
