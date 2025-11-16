# main.py
import os
import logging
import asyncio
import math
from typing import Dict
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# -------------------------
# Config
# -------------------------
for var in ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]:
    if not os.getenv(var):
        raise EnvironmentError(f"Missing {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "1.0"))
AUTO_CLOSE_MINUTES = 10

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# -------------------------
# Telegram
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        logger.info("Telegram sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# -------------------------
# MEXC CCXT
# -------------------------
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
        "recvWindow": 5000,
        "adjustForTimeDifference": True
    },
    "timeout": 10000
})

# -------------------------
# Symbol
# -------------------------
_cached_markets: Dict[str, str] = {}
async def resolve_symbol(base: str) -> str:
    if not _cached_markets:
        await exchange.load_markets()
        _cached_markets = {m.split("/")[0]: m for m in exchange.markets.keys() if m.endswith(":USDT")}
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise Exception("Symbol not found")
    return symbol

# -------------------------
# Utils
# -------------------------
async def fetch_price(symbol: str) -> float:
    try:
        ticker = await asyncio.wait_for(exchange.fetch_ticker(symbol), timeout=8)
        return float(ticker.get("last", 0))
    except:
        return 0.0

async def get_market_info(symbol: str) -> dict:
    await exchange.load_markets()
    market = exchange.markets.get(symbol, {})
    info = market.get('info', {})
    return {
        "vol_unit": float(info.get('volUnit', 1)),
        "min_vol": float(info.get('minVol', 1)),
        "price_scale": int(info.get('priceScale', 2))
    }

async def calculate_qty(symbol: str, usd: float, lev: int) -> float:
    price = await fetch_price(symbol)
    if price <= 0:
        raise Exception("No price")
    info = await get_market_info(symbol)
    qty = (usd * lev) / price
    if qty * price < MIN_ORDER_USD:
        qty = MIN_ORDER_USD / price
    qty = math.ceil(qty / info['vol_unit']) * info['vol_unit']
    if qty < info['min_vol']:
        qty = info['min_vol']
    logger.info(f"Qty: {qty} (${usd} × {lev}x / {price})")
    return qty

# -------------------------
# Market Order
# -------------------------
async def create_market_position_usdt(symbol: str, qty: float, leverage: int):
    query = {
        "openType": 1,
        "positionSide": "LONG",
        "positionType": 1,
        "leverage": leverage
    }
    logger.info(f"SENDING MARKET BUY {qty} {symbol} | lev {leverage}x")

    order = None
    try:
        order = await asyncio.wait_for(
            exchange.create_order(symbol, "market", "buy", qty, None, query),
            timeout=10
        )
    except Exception as e:
        logger.warning(f"create_order failed: {e}")

    entry_price = await fetch_price(symbol)
    order_id = order.get('id') if order else "FORCED"

    logger.info(f"POSITION OPENED @ {entry_price} (ID: {order_id})")
    await tg_send(f"FORCED LONG OPENED @ {entry_price}\nQty: {qty} | ${FIXED_AMOUNT_USD} | {LEVERAGE}x")

    return {'average': entry_price, 'id': order_id}

# -------------------------
# TP + SL
# -------------------------
async def create_tp_limit(symbol: str, qty: float, price: float, leverage: int):
    query = {
        "reduceOnly": True,
        "positionSide": "LONG",
        "openType": 1,
        "positionType": 1,
        "leverage": leverage
    }
    try:
        await asyncio.wait_for(
            exchange.create_order(symbol, "limit", "sell", qty, price, query),
            timeout=8
        )
        logger.info(f"TP SET @ {price}")
    except Exception as e:
        logger.warning(f"TP failed: {e}")
        await tg_send(f"Warning: TP не установлен: {price}")

async def create_sl_limit(symbol: str, qty: float, price: float, leverage: int):
    query = {
        "reduceOnly": True,
        "positionSide": "LONG",
        "openType": 1,
        "positionType": 1,
        "leverage": leverage
    }
    try:
        await asyncio.wait_for(
            exchange.create_order(symbol, "limit", "sell", qty, price, query),
            timeout=8
        )
        logger.info(f"SL SET @ {price}")
    except Exception as e:
        logger.warning(f"SL failed: {e}")
        await tg_send(f"Warning: SL не установлен: {price}")

# -------------------------
# Auto-close
# -------------------------
async def auto_close_position():
    global active_position  # ← global В НАЧАЛЕ!
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    if not active_position:
        return

    try:
        SYMBOL = await resolve_symbol("XRP")
        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos.get('side') == 'long' and float(pos.get('contracts', 0)) > 0:
                qty = float(pos['contracts'])
                await exchange.create_order(SYMBOL, "market", "sell", qty, None, {"reduceOnly": True})
                await tg_send(f"Автозакрытие: LONG закрыт по рынку\nQty: {qty}")
                break
        active_position = False
    except Exception as e:
        await tg_send(f"Ошибка автозакрытия: {e}")

# -------------------------
# Main Logic
# -------------------------
active_position = False  # ← Объявлена ДО использования

async def open_position():
    global active_position
    if active_position:
        await tg_send("Warning: Уже открыто")
        return

    try:
        SYMBOL = await resolve_symbol("XRP")
        qty = await calculate_qty(SYMBOL, FIXED_AMOUNT_USD, LEVERAGE)
        order = await create_market_position_usdt(SYMBOL, qty, LEVERAGE)
        entry = order['average']

        info = await get_market_info(SYMBOL)
        tp = round(entry * (1 + TP_PERCENT / 100), info['price_scale'])
        sl = round(entry * (1 - SL_PERCENT / 100), info['price_scale'])

        await create_tp_limit(SYMBOL, qty, tp, LEVERAGE)
        await create_sl_limit(SYMBOL, qty, sl, LEVERAGE)

        active_position = True
        msg = (
            f"<b>LONG ОТКРЫТ</b>\n"
            f"<code>{SYMBOL}</code>\n"
            f"${FIXED_AMOUNT_USD} | {LEVERAGE}x\n"
            f"Qty: <code>{qty}</code>\n"
            f"Entry: <code>{entry:.4f}</code>\n"
            f"TP (+{TP_PERCENT}%): <code>{tp:.4f}</code>\n"
            f"SL (-{SL_PERCENT}%): <code>{sl:.4f}</code>\n"
            f"Автозакрытие: через {AUTO_CLOSE_MINUTES} мин"
        )
        await tg_send(msg)

        asyncio.create_task(auto_close_position())

    except Exception as e:
        await tg_send(f"Error: {e}")
        active_position = False

# -------------------------
# FastAPI
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send("Bot started | XRP Long | $10 | 10x | TP +0.5% | SL -1% | Автозакрытие 10 мин")
    yield
    await exchange.close()
    await tg_send("Bot stopped")

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("<h1>MEXC XRP Bot — OK</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")
    data = await request.json()
    if data.get("signal") == "buy":
        asyncio.create_task(open_position())
        await tg_send("BUY signal received")
    return {"status": "ok"}

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
