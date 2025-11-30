import os
import time
import hashlib
import hmac
import httpx
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot

# ====================== CONFIG ======================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY      = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET   = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "supersecret123")
FIXED_USDT       = float(os.getenv("FIXED_AMOUNT_USDT", "30"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20.0)
BASE = "https://fapi.binance.com"
active = {}

# Простейший и 100% рабочий способ обрезать quantity под Binance
def fix_quantity(symbol: str, qty: float) -> str:
    # Для мем-коинов — только целые числа
    if symbol in ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "1000PEPEUSDT", "BONKUSDT", "FLOKIUSDT"]:
        return str(int(qty))
    # Для остальных — 3 знака
    return f"{qty:.3f}".rstrip("0").rstrip(".")

async def api(method: str, path: str, params: dict = None, signed: bool = True):
    url = BASE + path
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
        p["signature"] = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    r = await client.request(method, url, params=p, headers=headers)
    r.raise_for_status()
    return r.json()

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except: pass

async def get_price(symbol: str) -> float:
    data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

async def open_long(symbol: str):
    if symbol in active:
        await tg(f"<b>УЖЕ ЕСТЬ</b> {symbol.replace('USDT','/USDT')}")
        return

    # Плечо 10x
    try:
        await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
    except: pass

    price = await get_price(symbol)
    raw_qty = (FIXED_USDT * LEVERAGE) / price
    qty = fix_quantity(symbol, raw_qty)

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    })

    active[symbol] = qty
    await tg(f"<b>LONG ×{LEVERAGE}</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\n${FIXED_USDT} → {qty} монет\n≈ {price:.6f}")

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
    await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}")

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "БОТ ЖИВ", "positions": list(active.keys())}

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

    signal = data.get("signal", "").upper()

    if signal == "LONG":
        await open_long(sym)
    elif signal == "CLOSE":
        await close_position(sym)

    return {"ok": True}
