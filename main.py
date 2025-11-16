# main.py
import os
import time
import traceback
import logging
import asyncio
from typing import Optional, Dict
import math
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# -------------------------
# Config / Secrets
# -------------------------
REQUIRED_SECRETS = [
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MEXC_API_KEY",
    "MEXC_API_SECRET",
    "WEBHOOK_SECRET",
]
missing = [s for s in REQUIRED_SECRETS if not os.getenv(s)]
if missing:
    raise EnvironmentError(f"ОШИБКА: не заданы секреты: {', '.join(missing)}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))   # $10
LEVERAGE = int(os.getenv("LEVERAGE", "10"))                     # 10x
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))               # +0.5%
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "2.2616"))

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mexc-bot")

# -------------------------
# Telegram
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        logger.info("Telegram: сообщение отправлено")
    except Exception as e:
        logger.error(f"Telegram error: {e}\n{traceback.format_exc()}")

# -------------------------
# MEXC CCXT
# -------------------------
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
    "timeout": 30000
})

# -------------------------
# Symbol resolve
# -------------------------
_cached_markets: Optional[Dict[str, str]] = None
async def resolve_symbol(base: str) -> str:
    global _cached_markets
    if _cached_markets is None:
        await exchange.load_markets()
        _cached_markets = {m.split("/")[0]: m for m in exchange.markets.keys() if m.endswith(":USDT")}
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise Exception(f"Symbol {base} not found")
    return symbol

# -------------------------
# Safe CCXT
# -------------------------
async def safe_ccxt_call(fn, *args, retries=3, **kwargs):
    for attempt in range(retries):
        try:
            result = await fn(*args, **kwargs)
            return result
        except (ccxt.NetworkError, ccxt.RequestTimeout):
            logger.warning(f"Network timeout #{attempt+1}")
            await asyncio.sleep(1)
        except ccxt.BaseError as e:
            logger.error(f"CCXT error: {e}")
            return None
    return None

# -------------------------
# Balance & Price
# -------------------------
async def fetch_balance_usdt() -> float:
    bal = await safe_ccxt_call(exchange.fetch_balance)
    return float(bal.get("total", {}).get("USDT", 0) or 0) if bal else 0.0

async def fetch_price(symbol: str) -> float:
    ticker = await safe_ccxt_call(exchange.fetch_ticker, symbol)
    return float(ticker.get("last") or 0) if ticker else 0.0

async def get_market_info(symbol: str) -> dict:
    await exchange.load_markets()
    market = exchange.markets.get(symbol)
    if not market:
        raise Exception("Market not found")
    info = market.get('info', {})
    return {
        "vol_unit": float(info.get('volUnit', 1)),
        "min_vol": float(info.get('minVol', 1)),
        "price_scale": int(info.get('priceScale', 2))
    }

async def calculate_qty_for_usd(symbol: str, usd_amount: float, leverage: int) -> float:
    price = await fetch_price(symbol)
    if price <= 0:
        raise Exception("Price error")
    info = await get_market_info(symbol)
    qty = (usd_amount * leverage) / price
    if qty * price < MIN_ORDER_USD:
        qty = MIN_ORDER_USD / price
    qty = math.ceil(qty / info['vol_unit']) * info['vol_unit']
    if qty < info['min_vol']:
        qty = info['min_vol']
    logger.info(f"Qty: {qty} (${usd_amount} × {leverage}x / {price})")
    return qty

# -------------------------
# Position check
# -------------------------
async def check_active_position(symbol: str) -> bool:
    positions = await safe_ccxt_call(exchange.fetch_positions, [symbol])
    if positions:
        for pos in positions:
            if float(pos.get('contracts', 0)) != 0:
                return True
    return False

# -------------------------
# Orders (CRITICAL: query + leverage)
# -------------------------
async def create_market_position_usdt(symbol: str, side: str, qty: float, leverage: int):
    positionSide = "LONG" if side == "buy" else "SHORT"
    query = {
        "openType": 1,
        "positionSide": positionSide,
        "positionType": 1 if positionSide == "LONG" else 2,
        "leverage": leverage
    }
    logger.info(f"Market order: {side} {qty} {symbol} | leverage {leverage}x")
    order = await safe_ccxt_call(
        exchange.create_order,
        symbol, "market", side, qty, None, query
    )
    if not order:
        raise Exception("Market order failed")
    avg = order.get("average") or order.get("price")
    logger.info(f"Executed: ID {order.get('id')} @ {avg}")
    return order

async def create_tp_limit(symbol: str, qty: float, price: float, leverage: int):
    query = {
        "reduceOnly": True,
        "positionSide": "LONG",
        "openType": 1,
        "positionType": 1,
        "leverage": leverage
    }
    logger.info(f"TP: sell {qty} @ {price}")
    order = await safe_ccxt_call(
        exchange.create_order,
        symbol, "limit", "sell", qty, price, query
    )
    if not order:
        await tg_send(f"Warning: TP не установлен: {price}")
    return order

# -------------------------
# Main logic
# -------------------------
active_position = False

async def open_position_from_signal(symbol_base: str = "XRP", amount_usd: float = None):
    global active_position
    try:
        SYMBOL = await resolve_symbol(symbol_base)
        if await check_active_position(SYMBOL):
            await tg_send("Warning: Позиция уже открыта")
            return

        usd = amount_usd or FIXED_AMOUNT_USD
        if usd < MIN_ORDER_USD:
            await tg_send(f"Error: {usd} < {MIN_ORDER_USD}")
            return

        qty = await calculate_qty_for_usd(SYMBOL, usd, LEVERAGE)
        order = await create_market_position_usdt(SYMBOL, "buy", qty, LEVERAGE)
        entry = order.get("average") or await fetch_price(SYMBOL)
        info = await get_market_info(SYMBOL)
        tp_price = round(entry * (1 + TP_PERCENT / 100), info['price_scale'])
        await create_tp_limit(SYMBOL, qty, tp_price, LEVERAGE)

        active_position = True
        msg = (
            f"Success: <b>LONG ОТКРЫТ</b>\n"
            f"Символ: <code>{SYMBOL}</code>\n"
            f"Сумма: <code>${usd}</code>\n"
            f"Плечо: <code>{LEVERAGE}x</code>\n"
            f"Qty: <code>{qty}</code>\n"
            f"Entry: <code>{entry:.4f}</code>\n"
            f"TP (+{TP_PERCENT}%): <code>{tp_price:.4f}</code>\n"
            f"SL: <i>не установлен</i>"
        )
        await tg_send(msg)

    except Exception as e:
        logger.error(f"Error: {e}\n{traceback.format_exc()}")
        await tg_send(f"Error: {str(e)}")
        active_position = False

# -------------------------
# FastAPI
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bot starting...")
    await tg_send("Bot started\nXRP Long Bot | $10 | 10x | TP +0.5%")
    yield
    await exchange.close()
    await tg_send("Bot stopped")

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("<h1>MEXC XRP Bot</h1><p>Status: OK</p>")

@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")

    try:
        data = await request.json()
    except:
        raise HTTPException(400, "Invalid JSON")

    signal = data.get("signal", "").lower()
    if signal != "buy":
        raise HTTPException(400, "Only 'buy'")

    asyncio.create_task(open_position_from_signal("XRP", data.get("fixed_amount_usd")))
    await tg_send("Signal received: BUY XRP")
    return {"status": "ok"}

@app.get("/health")
async def health():
    try:
        price = await fetch_price("XRPUSDT:USDT")
        pos = await check_active_position("XRPUSDT:USDT")
        return {"status": "ok", "price": price, "position": pos}
    except:
        return {"status": "error"}

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
