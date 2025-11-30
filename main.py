# main.py — OZ BOT — МУЛЬТИСИМВОЛ, CROSS, БЕЗ ОШИБОК, 2025
import os
import time
import hmac
import hashlib
import urllib.parse
from typing import Dict, Optional
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной переменной: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")
AMOUNT_USD       = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=30.0)
BASE_URL = "https://fapi.binance.com"
active = {}

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except:
        pass

def sign(params: Dict) -> str:
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def api(method: str, path: str, params: Optional[Dict] = None, signed: bool = True):
    url = BASE_URL + path
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try: msg = e.response.json().get("msg", msg)
            except: pass
        await tg(f"<b>ОШИБКА BINANCE</b>\n<code>{msg[:400]}</code>")
        return None

async def get_price(symbol: str) -> float:
    data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"]) if data else 0.0

async def fix_qty(symbol: str, qty: float) -> str:
    data = await api("GET", "/fapi/v1/exchangeInfo", signed=False)
    if not data: return f"{qty:.3f}".rstrip("0").rstrip(".")
    for s in data["symbols"]:
        if s["symbol"] == symbol:
            prec = s.get("quantityPrecision", 3)
            step = next((float(f["stepSize"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0.001)
            min_qty = next((float(f["minQty"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0)
            qty = max((qty // step) * step, min_qty)
            return f"{qty:.{prec}f}".rstrip("0").rstrip(".")
    return f"{qty:.3f}".rstrip("0").rstrip(".")

async def open_long(sym: str):
    symbol = sym.upper() if sym.upper().endswith("USDT") else sym.upper() + "USDT"
    if symbol in active:
        await tg(f"<b>{symbol.replace('USDT','/USDT')} уже открыт</b>")
        return

    await api("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"}, signed=True)
    await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE}, signed=True)

    price = await get_price(symbol)
    if price == 0: return

    qty = await fix_qty(symbol, AMOUNT_USD * LEVERAGE / price)

    order = await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    }, signed=True)

    if order:
        active[symbol] = qty
        await tg(f"<b>LONG ОТКРЫТ ×{LEVERAGE} (Cross)</b>\n"
                 f"<code>{symbol.replace('USDT','/USDT')}</code>\n"
                 f"${AMOUNT_USD} → {qty} шт\n"
                 f"≈ {price:.6f}")

async def close_pos(sym: str):
    symbol = sym.upper() if sym.upper().endswith("USDT") else sym.upper() + "USDT"
    if symbol not in active: return

    pos = await api("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    qty = None
    if pos:
        for p in pos:
            if p["symbol"] == symbol and float(p["positionAmt"]) > 0:
                qty = p["positionAmt"]
                break
    if qty:
        await api("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true"
        }, signed=True)
        active.pop(symbol, None)
        await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg("Bot запущен...")
    await tg(f"<b>OZ BOT ГОТОВ ×{LEVERAGE}</b>\n${AMOUNT_USD} | Cross | Любой символ")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)

    data = await request.json()
    symbol = data.get("symbol", "").replace("/", "").upper()
    signal = data.get("signal", "").upper()

    if signal == "LONG":
        asyncio.create_task(open_long(symbol))
    elif signal in ["CLOSE", "CLOSE_ALL"]:
        asyncio.create_task(close_pos(symbol))

    return {"ok": True}
