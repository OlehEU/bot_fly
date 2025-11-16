# main.py
import os
import json
import time
import traceback
import logging
import asyncio
from typing import Optional
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

BASE_SYMBOL = os.getenv("BASE_SYMBOL", "XRP")  # базовый актив
FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "2.2616"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "25"))

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mexc-bot")

# -------------------------
# Telegram helper
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
        logger.info("INFO: Сообщение отправлено в Telegram")
    except Exception as e:
        logger.error(f"ERROR: Не удалось отправить в Telegram: {e}\n{traceback.format_exc()}")

# -------------------------
# MEXC (ccxt async)
# -------------------------
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},  # фьючерсы
})

# -------------------------
# Utilities
# -------------------------
async def safe_ccxt_call(fn, *args, **kwargs):
    try:
        result = await fn(*args, **kwargs)
        logger.info(f"CCXT call success: {fn.__name__} args={args} kwargs={kwargs}")
        return result
    except ccxt.BaseError as e:
        logger.error(f"CCXT error in {fn.__name__}: {e}\n{traceback.format_exc()}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in {fn.__name__}: {e}\n{traceback.format_exc()}")
        return None

# -------------------------
# Авто-выбор символа по базовой монете
# -------------------------
def find_swap_symbol(base: str) -> Optional[str]:
    """
    Находит первую доступную USDT swap пару для базового актива
    """
    for symbol, market in exchange.markets.items():
        if market['base'] == base and market['quote'] == 'USDT' and market['type'] == 'swap':
            return symbol
    return None

async def get_correct_symbol(base_symbol: str) -> str:
    await exchange.load_markets()
    symbol = find_swap_symbol(base_symbol)
    if not symbol:
        raise Exception(f"No USDT swap contract found for {base_symbol}")
    logger.info(f"Используемый символ: {symbol}")
    return symbol

# -------------------------
# Balance / Price helpers
# -------------------------
async def fetch_balance_usdt() -> float:
    bal = await safe_ccxt_call(exchange.fetch_balance)
    if bal is None:
        return 0.0
    usdt = float(bal.get("total", {}).get("USDT", 0) or 0)
    logger.info(f"Баланс USDT: {usdt}")
    return usdt

async def fetch_price(symbol: str) -> float:
    ticker = await safe_ccxt_call(exchange.fetch_ticker, symbol)
    if ticker is None:
        return 0.0
    price = float(ticker.get("last") or ticker.get("close") or 0)
    logger.info(f"Текущая цена {symbol}: {price}")
    return price

async def get_market_info(symbol: str) -> dict:
    await exchange.load_markets()
    market = exchange.markets.get(symbol)
    if not market:
        raise Exception(f"Market {symbol} not found")
    info = market.get('info', {})
    vol_unit = float(info.get('volUnit', 1))
    min_vol = float(info.get('minVol', 1))
    price_scale = int(info.get('priceScale', 2))
    return {"vol_unit": vol_unit, "min_vol": min_vol, "price_scale": price_scale}

async def calculate_qty_for_usd(symbol: str, usd_amount: float, leverage: int) -> float:
    price = await fetch_price(symbol)
    market_info = await get_market_info(symbol)
    vol_unit = market_info['vol_unit']
    min_vol = market_info['min_vol']

    qty = (usd_amount * leverage) / price
    if qty * price < MIN_ORDER_USD:
        qty = MIN_ORDER_USD / price

    qty = math.floor(qty / vol_unit) * vol_unit
    if qty < min_vol:
        qty = min_vol
    logger.info(f"Расчитанное количество: {qty} (USD {usd_amount} * L{leverage} / price {price})")
    return qty

# -------------------------
# Leverage / Position helpers
# -------------------------
async def set_leverage_usdt(symbol: str, leverage: int, position_side: str):
    try:
        params = {"positionSide": position_side}
        await safe_ccxt_call(exchange.set_leverage, leverage, symbol, params)
        logger.info(f"Плечо установлено: {leverage}x для {position_side}")
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e} — продолжим")

# -------------------------
# Order creation
# -------------------------
async def create_market_position_usdt(symbol: str, side: str, qty: float, leverage: int):
    positionSide = "LONG" if side == "buy" else "SHORT"
    await exchange.load_markets()
    await set_leverage_usdt(symbol, leverage, positionSide)

    params = {"positionSide": positionSide}
    logger.info(f"Создаю рыночный ордер: {side} {qty} {symbol} params={params}")
    order = await safe_ccxt_call(exchange.create_market_order, symbol, side, qty, None, params)
    if order is None:
        await tg_send(f"❌ Ошибка создания рыночного ордера: {side} {qty} {symbol}")
        raise Exception(f"Market order failed: {side} {qty} {symbol}")
    return order

async def create_tp_sl_limit(symbol: str, close_side: str, qty: float, price: float, positionSide:str):
    params = {"reduceOnly": True, "positionSide": positionSide}
    logger.info(f"Создаю limit закрывающий ордер {close_side} {qty} @ {price} params={params}")
    order = await safe_ccxt_call(exchange.create_order, symbol, "limit", close_side, qty, price, params)
    if order is None:
        await tg_send(f"❌ Ошибка выставления TP/SL ордера: {close_side} {qty} @ {price} {symbol}")
    return order

# -------------------------
# Position high-level logic
# -------------------------
last_trade_info: Optional[dict] = None
active_position = False

async def open_position_from_signal(signal: str, fixed_amount_usd: Optional[float] = None):
    global active_position, last_trade_info, SYMBOL
    try:
        if active_position:
            logger.info("Позиция уже активна — пропускаем открытие.")
            await tg_send("⚠️ Позиция уже активна — новый сигнал проигнорирован.")
            return

        SYMBOL = await get_correct_symbol(BASE_SYMBOL)  # подставляем символ автоматически

        balance = await fetch_balance_usdt()
        usd_amount = fixed_amount_usd if fixed_amount_usd and fixed_amount_usd > 0 else (balance * RISK_PERCENT / 100)
        if usd_amount < MIN_ORDER_USD:
            logger.warning(f"Недостаточный объём для открытия позиции: {usd_amount} USD")
            await tg_send(f"❗ Недостаточный объём для открытия: {usd_amount:.2f} USDT (min {MIN_ORDER_USD})")
            return

        qty = await calculate_qty_for_usd(SYMBOL, usd_amount, LEVERAGE)

        side = "buy" if signal.lower() == "buy" else "sell"
        positionSide = "LONG" if side == "buy" else "SHORT"
        close_side = "sell" if side == "buy" else "buy"

        order = await create_market_position_usdt(SYMBOL, side, qty, LEVERAGE)

        entry_price = order.get("average") or order.get("price") or await fetch_price(SYMBOL)
        market_info = await get_market_info(SYMBOL)
        price_scale = market_info['price_scale']

        if side == "buy":
            tp_price = round(entry_price * 1.015, price_scale)
            sl_price = round(entry_price * 0.99, price_scale)
        else:
            tp_price = round(entry_price * 0.985, price_scale)
            sl_price = round(entry_price * 1.01, price_scale)

        await create_tp_sl_limit(SYMBOL, close_side, qty, tp_price, positionSide)
        await create_tp_sl_limit(SYMBOL, close_side, qty, sl_price, positionSide)

        active_position = True
        last_trade_info = {
            "signal": signal,
            "side": side,
            "qty": qty,
            "entry": entry_price,
            "tp": tp_price,
            "sl": sl_price,
            "order": order,
            "timestamp": time.time()
        }

        msg = (
            f"✅ <b>{side.upper()} OPENED</b>\n"
            f"Символ: <code>{SYMBOL}</code>\n"
            f"Qty: <code>{qty}</code>\n"
            f"Entry: <code>{entry_price}</code>\n"
            f"TP: <code>{tp_price}</code>\n"
            f"SL: <code>{sl_price}</code>\n"
            f"Баланс: {balance:.2f} USDT\n"
            f"Плечо: {LEVERAGE}x\n"
        )
        await tg_send(msg)
        logger.info("ПОЗИЦИЯ ОТКРЫТА и уведомление отправлено.")
    except Exception as e:
        logger.error(f"Ошибка при открытии позиции: {e}\n{traceback.format_exc()}")
        await tg_send(f"❌ Ошибка при открытии позиции: {str(e)}")
        raise

# -------------------------
# FastAPI lifespan
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        SYMBOL = await get_correct_symbol(BASE_SYMBOL)
        logger.info("🚀 ЗАПУСК БОТА (lifespan startup)")
        try:
            balance = await fetch_balance_usdt()
        except Exception:
            balance = None
        try:
            price = await fetch_price(SYMBOL)
        except Exception:
            price = None
        start_msg = (
            f"✅ Bot started\n"
            f"Символ: {SYMBOL}\n"
            f"Баланс: {balance if balance is not None else 'N/A'} USDT\n"
            f"Цена: {price if price is not None else 'N/A'}\n"
            f"Фиксированная сумма: {FIXED_AMOUNT_USD} USDT\n"
            f"Плечо: {LEVERAGE}x\n"
            f"Webhook: /webhook (X-Webhook-Secret header required)\n"
        )
        try:
            await tg_send(start_msg)
        except Exception:
            pass
        yield
    finally:
        logger.info("🛑 ОСТАНОВКА БОТА (lifespan shutdown)")
        try:
            await exchange.close()
        except Exception:
            pass
        try:
            await tg_send("🔴 Бот остановлен")
        except Exception:
            pass

app = FastAPI(lifespan=lifespan)

# -------------------------
# Routes
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    try:
        balance = await fetch_balance_usdt()
    except Exception:
        balance = None
    try:
        price = await fetch_price(SYMBOL)
    except Exception:
        price = None
    global last_trade_info, active_position
    status = "АКТИВНА" if active_position else "НЕТ"
    html = f"<html><body><h1>🤖 MEXC Futures Bot</h1><p>Символ: {SYMBOL}</p><p>Баланс: {balance}</p><p>Цена: {price}</p><p>Позиция: {status}</p></body></html>"
    return HTMLResponse(html)

@app.post("/webhook")
async def webhook(request: Request):
    provided = request.headers.get("X-Webhook-Secret") or request.headers.get("Authorization")
    if provided is None:
        raise HTTPException(403, "No webhook secret provided")
    if provided.startswith("Bearer "):
        provided = provided.split(" ", 1)[1]
    if provided != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid webhook secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    signal = payload.get("signal")
    custom_amount = payload.get("fixed_amount_usd")

    if signal not in ("buy", "sell"):
        raise HTTPException(400, "signal must be 'buy' or 'sell'")

    asyncio.create_task(open_position_from_signal(signal, fixed_amount_usd=custom_amount))
    await tg_send(f"📨 Received signal: {signal.upper()}. Открытие позиции запланировано.")
    return {"status": "accepted", "signal": signal}

@app.get("/health")
async def health():
    try:
        price = await fetch_price(SYMBOL)
        balance = await fetch_balance_usdt()
        return {
            "status": "ok",
            "symbol": SYMBOL,
            "price": price,
            "balance": balance,
            "active_position": active_position,
            "timestamp": time.time()
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
