import os
import time
import hmac
import hashlib
from typing import Dict
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ==================== КОНФИГ ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        raise EnvironmentError(f"Нет переменной {v}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20)
BASE = "https://fapi.binance.com"
active = set()

# ================= TELEGRAM =====================
async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except:
        pass

# ================= SIGNATURE =====================
def make_signature(params: Dict) -> str:
    # Binance требует raw параметры, без URL-encode
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ================= BINANCE API ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    url = BASE + path
    p = params.copy() if params else {}

    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = make_signature(p)

    headers = {"X-MBX-APIKEY": API_KEY}

    try:
        r = await client.request(method, url, params=p, headers=headers)
        if r.status_code != 200:
            msg = r.json().get("msg", r.text)
            await tg(f"<b>BINANCE ERROR {r.status_code} {path}</b>\n<code>{msg}</code>")
            return None
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL</b>\n{str(e)[:300]}")
        return None

# ================ QTY ROUND =======================
def fix_qty(symbol: str, qty: float) -> str:
    if symbol in ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "1000PEPEUSDT", "BONKUSDT", "FLOKIUSDT"]:
        return str(int(qty))
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ OPEN LONG =======================
async def open_long(sym: str):
    symbol = sym.upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if symbol in active:
        await tg(f"<b>{symbol} уже открыт</b>")
        return

    # marginType
    await binance("POST", "/fapi/v1/marginType", {
        "symbol": symbol,
        "marginType": "cross"
    })

    # leverage
    await binance("POST", "/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": LEV
    })

    # price
    data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not data:
        return
    price = float(data["price"])

    raw_qty = AMOUNT * LEV / price
    qty = fix_qty(symbol, raw_qty)

    # Hedge Mode → positionSide=LONG
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty,
        "positionSide": "LONG"
    })

    if order:
        active.add(symbol)
        await tg(
            f"<b>LONG ×{LEV} (Cross)</b>\n"
            f"<code>{symbol}</code>\n"
            f"{qty} шт @ {price:.6f}"
        )

# ================= CLOSE ==========================
async def close(sym: str):
    symbol = sym.upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if symbol not in active:
        return

    pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos:
        return

    qty = next((p["positionAmt"] for p in pos if p["symbol"] == symbol and float(p["positionAmt"]) > 0), None)
    if not qty:
        return

    await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true",
        "positionSide": "LONG"
    })

    active.discard(symbol)
    await tg(f"<b>CLOSE</b> {symbol}")

# ================= FASTAPI =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg("<b>OZ BOT 2025 — ONLINE</b>\nCross Mode | Hedge Mode FIXED")
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
    symbol = data.get("symbol", "").upper()
    sig = data.get("signal", "").upper()

    if sig == "LONG":
        asyncio.create_task(open_long(symbol))
    elif sig == "CLOSE":
        asyncio.create_task(close(symbol))

    return {"ok": True}
