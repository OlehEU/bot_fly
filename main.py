# main.py — TERMINATOR 2026 | FIXED VERSION (Signature + Binance Futures ORDER)
import os
import time
import logging
import asyncio
import traceback
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oz-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)

binance_client = httpx.AsyncClient(timeout=30.0)

async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ====================== ПРАВИЛЬНАЯ ПОДПИСЬ ======================
def create_signature(params: Dict[str, Any]) -> str:
    # сортировка обязательна
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

# ====================== УНИВЕРСАЛЬНЫЙ ЗАПРОС К BINANCE ======================
async def binance_request(method: str, endpoint: str, params=None, signed=True):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}

    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = create_signature(params)

    headers = {
        "X-MBX-APIKEY": BINANCE_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        if method == "GET":
            response = await binance_client.get(url, params=params, headers=headers)
        else:
            # 🔥 POST ТОЛЬКО В data, НЕ params!
            response = await binance_client.post(url, data=params, headers=headers)

        response.raise_for_status()
        return response.json()

    except httpx.HTTPStatusError as e:
        try:
            err = e.response.json()
            await tg_send(f"<b>BINANCE ERROR</b>\n<code>{err.get('code')} — {err.get('msg')}</code>")
        except:
            await tg_send(f"<b>BINANCE CRITICAL</b>\n<code>{str(e)}</code>")
        raise

# ====================== КЭШ ПО СИМВОЛАМ ======================
SYMBOL_DATA = {}

async def get_symbol_data(symbol: str):
    if symbol in SYMBOL_DATA:
        return SYMBOL_DATA[symbol]

    info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)

    for s in info["symbols"]:
        if s["symbol"] == symbol:
            prec = s.get("quantityPrecision", 3)
            min_qty = 0.0
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    min_qty = float(f["minQty"])
            SYMBOL_DATA[symbol] = {"precision": prec, "min_qty": min_qty}

            # ставим плечо
            try:
                await binance_request("POST", "/fapi/v1/leverage", {
                    "symbol": symbol,
                    "leverage": LEVERAGE
                })
            except:
                pass

            return SYMBOL_DATA[symbol]

    raise Exception(f"Символ не найден: {symbol}")

async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price",
                                 {"symbol": symbol}, signed=False)
    return float(data["price"])

async def calc_qty(symbol: str) -> str:
    d = await get_symbol_data(symbol)
    price = await get_price(symbol)

    raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw, d["precision"])
    if qty < d["min_qty"]:
        qty = d["min_qty"]

    return f"{qty:.{d['precision']}f}".rstrip("0").rstrip(".")

# ====================== LONG ======================
async def open_long(symbol: str):
    try:
        qty = await calc_qty(symbol)
        oid = f"oz_{int(time.time()*1000)}"

        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": oid,
            "positionSide": "LONG"
        }

        entry = await get_price(symbol)
        t0 = time.time()

        res = await binance_request("POST", "/fapi/v1/order", params)

        await tg_send(
            f"<b>LONG {symbol} ОТКРЫТ</b>\n"
            f"Entry: <code>{entry}</code>\n"
            f"Qty: {qty}"
        )

    except Exception as e:
        await tg_send(f"<b>ОШИБКА ОТКРЫТИЯ</b>\n<code>{e}</code>")

# ====================== CLOSE LONG ======================
async def close_long(symbol):
    try:
        pos = await binance_request(
            "GET", "/fapi/v2/positionRisk",
            {"symbol": symbol}
        )

        amt = 0.0
        for p in pos:
            if p["symbol"] == symbol and p["positionSide"] == "LONG":
                amt = float(p["positionAmt"])

        if abs(amt) < 0.0001:
            await tg_send(f"{symbol} LONG уже закрыт")
            return

        qty = str(abs(amt))

        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "reduceOnly": "true",
            "positionSide": "LONG",
            "quantity": qty
        })

        await tg_send(f"<b>{symbol} LONG ЗАКРЫТ</b>")

    except Exception as e:
        await tg_send(f"ОШИБКА ЗАКРЫТИЯ\n<code>{e}</code>")

# ====================== FASTAPI ======================
from fastapi import FastAPI
app = FastAPI()

@app.on_event("startup")
async def startup():
    await tg_send("<b>TERMINATOR 2026 ЗАПУЩЕН</b>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)

    body = await request.json()
    symbol = (body.get("symbol", "").upper() or "") + "USDT"
    action = body.get("direction", "").upper()

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))

    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1>TERMINATOR 2026 ONLINE</h1>"
