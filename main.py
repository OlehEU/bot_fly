# main.py — OZ MULTI BOT — УЛЬТРА-НАДЁЖНЫЙ, УЛЬТРА-БЫСТРЫЙ, CROSS, ЛЮБОЙ СИМВОЛ
import os
import time
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oz-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная окружения: {var}")

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

active_positions = {}  # symbol -> qty

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

def create_signature(params: Dict[str, Any]) -> str:
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def api(method: str, endpoint: str, params: Optional[Dict] = None, signed: bool = True):
    url = BASE_URL + endpoint
    p = params.copy() if params else {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = create_signature(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        try:
            msg = e.response.json().get("msg", str(e))
        except:
            msg = str(e)
        await tg(f"<b>BINANCE ОШИБКА</b>\n<code>{msg[:400]}</code>")
        return None

async def get_symbol_info(symbol: str):
    data = await api("GET", "/fapi/v1/exchangeInfo", signed=False)
    if not data: return None
    for s in data["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    return {
                        "minQty": float(f["minQty"]),
                        "stepSize": float(f["stepSize"]),
                        "precision": s.get("quantityPrecision", 3)
                    }
    return None

async def fix_quantity(symbol: str, qty: float) -> str:
    info = await get_symbol_info(symbol)
    if not info:
        return f"{qty:.3f}".rstrip("0").rstrip(".")
    step = info["stepSize"]
    precision = info["precision"]
    qty = (qty // step) * step
    qty = max(qty, info["minQty"])
    return f"{qty:.{precision}f}".rstrip("0").rstrip(".")

async def open_long(symbol: str):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if symbol in active_positions:
        await tg(f"<b>{symbol.replace('USDT','/USDT')} уже открыт</b>")
        return

    # Cross + плечо
    await api("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"}, signed=True)
    await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE}, signed=True)

    price_data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data: return
    price = float(price_data["price"])

    raw_qty = (AMOUNT_USD * LEVERAGE) / price
    qty = await fix_quantity(symbol, raw_qty)

    order = await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    }, signed=True)

    if order:
        active_positions[symbol] = qty
        await tg(f"<b>LONG ОТКРЫТ ×{LEVERAGE} (Cross)</b>\n"
                 f"<code>{symbol.replace('USDT','/USDT')}</code>\n"
                 f"${AMOUNT_USD} → {qty} монет\n"
                 f"≈ {price:.6f}")

async def close_position(symbol: str):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    if symbol not in active_positions:
        return

    pos = await api("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    qty = None
    if pos:
        for p in pos:
            if p["symbol"] == symbol and float(p["positionAmt"]) > 0:
                qty = p["positionAmt"]
                break

    if not qty:
        active_positions.pop(symbol, None)
        return

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    }, signed=True)

    active_positions.pop(symbol, None)
    await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}")

# ====================== LIFESPAN ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg("Bot стартует...")
    await tg(f"<b>OZ BOT ГОТОВ ×{LEVERAGE} (Cross)</b>\nЛюбой символ | ${AMOUNT_USD}\nРеакция <1с")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT — ONLINE ×10</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)

    data = await request.json()
    sym = data.get("symbol", "").replace("/", "").upper()
    signal = data.get("signal", "").upper()

    if signal == "LONG":
        asyncio.create_task(open_long(sym))
    elif signal == "CLOSE":
        asyncio.create_task(close_position(sym))

    return {"ok": True}
