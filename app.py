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


class VerifyRequest(BaseModel):
    code: str


@app.get("/health")
async def health():
    return {"status": "ok", "logged_in": browser.logged_in}


@app.post("/login")
async def login():
    try:
        await browser.start_login()
        return {"message": "Code sent to email. Call POST /login/verify with the code."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/login/verify")
async def verify(req: VerifyRequest):
    try:
        await browser.verify_login(req.code)
        return {"message": "Logged in successfully. Session saved."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/ask")
async def ask(req: AskRequest):
    if not browser.logged_in:
        raise HTTPException(status_code=401, detail="Not logged in. Call POST /login first.")
    async with browser.lock:
        try:
            response = await browser.send_prompt(req.prompt)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    save_response(req.prompt, response)
    return {"response": response}


@app.get("/history")
async def history(limit: int = 20):
    return {"items": get_history(limit)}


@app.get("/debug/screenshot")
async def debug_screenshot():
    """Скриншот того что сейчас видит браузер — для диагностики."""
    img = await browser.screenshot()
    return Response(content=img, media_type="image/png")


@app.get("/debug/html")
async def debug_html():
    """HTML страницы что сейчас открыта в браузере — для диагностики."""
    html = await browser.page_html()
    return Response(content=html, media_type="text/html")
