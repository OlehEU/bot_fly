# main.py — TERMINATOR 2026 (обновлённый и исправленный)
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
from telegram.request import AiohttpSession   # <<< ВАЖНОЕ ИСПРАВЛЕНИЕ

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

# <<< Telegram теперь полностью асинхронный
bot = Bot(token=TELEGRAM_TOKEN, request=AiohttpSession())

binance_client = httpx.AsyncClient(timeout=60.0)

async def tg_send(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ====================== BINANCE TIME OFFSET ======================
BINANCE_TIME_OFFSET = 0

async def sync_time():
    global BINANCE_TIME_OFFSET
    try:
        r = await binance_client.get("https://fapi.binance.com/fapi/v1/time")
        server = r.json()["serverTime"]
        local = int(time.time() * 1000)
        BINANCE_TIME_OFFSET = server - local
        logger.info(f"Time synced. Offset = {BINANCE_TIME_OFFSET}")
    except:
        pass

# ====================== ПОДПИСЬ ======================
def _sign(params: Dict[str, Any], secret: str) -> str:
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}

    if signed:
        params["timestamp"] = int(time.time() * 1000 + BINANCE_TIME_OFFSET)

        signature = _sign(params, BINANCE_API_SECRET)
        params["signature"] = signature

    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

    try:
        if method == "GET":
            r = await binance_client.get(url, params=params, headers=headers)
        else:
            # <<< PARAMS ДОЛЖНЫ ИДТИ В ТЕЛЕ, А НЕ В QUERY !!!
            r = await binance_client.post(url, data=params, headers=headers)

        r.raise_for_status()
        return r.json()

    except Exception as e:
        try:
            err = e.response.json()
            await tg_send(f"<b>BINANCE ERROR</b>\n<code>{err.get('code')} — {err.get('msg')}</code>")
        except:
            await tg_send(f"<b>BINANCE КРИТИЧКА</b>\n<code>{str(e)[:400]}</code>")

        raise

# ====================== EXCHANGE INFO CACHE ======================
SYMBOL_DATA = {}

async def get_symbol_data(symbol: str):
    if symbol in SYMBOL_DATA:
        return SYMBOL_DATA[symbol]

    ex = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in ex["symbols"]:
        if s["symbol"] == symbol:

            precision = int(s["quantityPrecision"])
            min_qty = 0.0
            step = 0.0

            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    min_qty = float(f["minQty"])
                    step = float(f["stepSize"])

            SYMBOL_DATA[symbol] = {
                "precision": precision,
                "min_qty": min_qty,
                "step": step
            }

            # Установка плеча
            try:
                await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
            except:
                pass

            return SYMBOL_DATA[symbol]

    raise Exception(f"Symbol not found: {symbol}")

# ====================== PRICE ======================
async def get_price(symbol: str) -> float:
    r = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(r["price"])

# ====================== QTY ======================
def round_step(qty: float, step: float) -> float:
    return ((qty // step) * step)

async def calc_qty(symbol: str) -> str:
    d = await get_symbol_data(symbol)
    price = await get_price(symbol)

    raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round_step(raw, d["step"])

    if qty < d["min_qty"]:
        qty = d["min_qty"]

    return f"{qty:.8f}".rstrip("0").rstrip(".")

# ====================== OPEN LONG ======================
async def open_long(symbol: str):
    try:
        qty = await calc_qty(symbol)
        oid = f"oz_{int(time.time()*1000)}"
        entry = await get_price(symbol)

        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "positionSide": "LONG",
            "newClientOrderId": oid,
            "quantity": qty
        }

        t0 = time.time()
        r = await binance_request("POST", "/fapi/v1/order", params)
        dt = round(time.time() - t0, 2)

        await tg_send(
            f"<b>LONG {symbol} ОТКРЫТ</b> за {dt}s\n"
            f"${FIXED_AMOUNT_USD} × {LEVERAGE}x\n"
            f"Entry: <code>{entry}</code>\n"
            f"Qty: {qty}"
        )

    except Exception as e:
        await tg_send(f"<b>ОШИБКА ОТКРЫТИЯ</b>\n<code>{str(e)}</code>")

# ====================== CLOSE LONG ======================
async def close_long(symbol: str):
    try:
        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = 0.0
        for p in pos:
            if p["symbol"] == symbol and p["positionSide"] == "LONG":
                amt = float(p["positionAmt"])
                break

        if abs(amt) < 0.0001:
            return await tg_send(f"{symbol} LONG уже закрыт")

        qty = f"{abs(amt):.8f}".rstrip("0").rstrip(".")

        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "positionSide": "LONG",
            "reduceOnly": "true",
            "quantity": qty
        })

        await tg_send(f"<b>{symbol} LONG ЗАКРЫТ</b>")

    except Exception as e:
        await tg_send(f"<b>ОШИБКА ЗАКРЫТИЯ</b>\n<code>{str(e)}</code>")

# ====================== FASTAPI ======================
app = FastAPI()

@app.on_event("startup")
async def startup():
    await sync_time()
    await tg_send("TERMINATOR 2026 запущен.\nВремя синхронизировано.\nГотов к работе.")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)

    data = await request.json()
    raw_symbol = data.get("symbol", "").upper()
    action = data.get("direction", "").upper()
    symbol = raw_symbol if raw_symbol.endswith("USDT") else raw_symbol + "USDT"

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))

    return {"status": "ok"}
