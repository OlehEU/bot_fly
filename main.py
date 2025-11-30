import os
import time
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot

# ====================== CONFIG ======================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY      = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET   = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "supersecret123")

FIXED_USDT = float(os.getenv("FIXED_AMOUNT_USDT", "30"))
LEVERAGE   = int(os.getenv("LEVERAGE", "10"))  # у тебя 10x

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20.0)
BASE = "https://fapi.binance.com"

active = {}  # символ → кол-во в позиции

def sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return httpx._utils.timed_hmac_digest(BINANCE_SECRET.encode(), query.encode(), "sha256").hex()

async def api(method: str, path: str, params: dict = None, signed: bool = True):
    url = BASE + path
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    r = await client.request(method, url, params=p, headers=headers)
    r.raise_for_status()
    return r.json()

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except: pass

async def get_price(symbol: str) -> float:
    data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

# ====================== ОТКРЫТЬ LONG ======================
async def open_long(symbol: str):
    if symbol in active:
        return

    # Плечо 10x (у тебя Hedge mode включён — должно пройти)
    try:
        await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
    except: pass

    price = await get_price(symbol)
    qty = round((FIXED_USDT * LEVERAGE) / price, 6)
    qty = max(qty, 0.001)

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    })

    active[symbol] = qty
    await tg(f"<b>OPEN LONG ×{LEVERAGE}</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\nРазмер: ${FIXED_USDT} → {qty} монет")

# ====================== ЗАКРЫТЬ ======================
async def close_position(symbol: str):
    if symbol not in active:
        return

    qty = active.pop(symbol)
    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    })
    await tg(f"<b>CLOSE {symbol.replace('USDT','/USDT')}</b>\nПозиция закрыта по рынку")

# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "OZ BOT ЖИВ", "positions": len(active)}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(403)

    sym = data.get("symbol", "").replace("/", "").upper()
    if not sym.endswith("USDT"):
        sym += "USDT"

    sig = data.get("signal", "").upper()

    if sig == "LONG":
        await open_long(sym)
    elif sig == "CLOSE":
        await close_position(sym)

    return {"ok": True}
