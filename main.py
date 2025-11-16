# main.py
import os
import json
import time
import traceback
import logging
import asyncio
from typing import Optional

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

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

SYMBOL = os.getenv("SYMBOL", "XRP/USDT")
FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "2.2616"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "25"))
CONTRACT_SIZE = float(os.getenv("CONTRACT_SIZE", "0.01"))

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mexc-bot")

# -------------------------
# Telegram helper
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    def sync_send():
        try:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
            logger.info("INFO: Сообщение отправлено в Telegram")
        except Exception as e:
            logger.error(f"ERROR: Не удалось отправить в Telegram: {e}")

    await asyncio.to_thread(sync_send)

# -------------------------
# MEXC (ccxt async)
# -------------------------
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# -------------------------
# Utilities
# -------------------------
def short_exc() -> str:
    return traceback.format_exc()

async def safe_call(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        logger.error(f"Exception in {fn.__name__}: {e}\n{traceback.format_exc()}")
        raise

# -------------------------
# Balance / Price helpers
# -------------------------
async def fetch_balance_usdt() -> float:
    bal = await exchange.fetch_balance()
    usdt = float(bal.get("total", {}).get("USDT", 0) or 0)
    logger.info(f"Баланс USDT: {usdt}")
    return usdt

async def fetch_price(symbol: str) -> float:
    ticker = await exchange.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or 0)
    logger.info(f"Текущая цена {symbol}: {price}")
    return price

async def amount_precision(symbol: str, amount: float) -> float:
    try:
        await exchange.load_markets()
        if symbol in exchange.markets:
            prec_str = exchange.markets[symbol].get('precision', {}).get('amount')
            if prec_str is not None:
                return float(exchange.amount_to_precision(symbol, amount))
    except Exception:
        pass
    return round(amount, 6)

# -------------------------
# Leverage / Position helpers
# -------------------------
async def set_leverage_usdt(symbol: str, leverage: int, position_side: str):
    try:
        params = {"positionSide": position_side}
        await exchange.set_leverage(leverage, symbol, params)
        logger.info(f"Плечо установлено: {leverage}x для {position_side}")
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e} — продолжим (не критично)")

# -------------------------
# Order creation (market + TP/SL)
# -------------------------
async def create_market_position_usdt(symbol: str, side: str, qty: float, leverage: int):
    positionSide = "LONG" if side == "buy" else "SHORT"
    await exchange.load_markets()
    await set_leverage_usdt(symbol, leverage, positionSide)
    params = {"positionSide": positionSide}
    logger.info(f"Создаю рыночный ордер: {side} {qty} {symbol} params={params}")
    order = await exchange.create_market_order(symbol, side, qty, None, params)
    logger.info(f"Order response: {order}")
    return order

async def create_tp_sl_limit(symbol: str, close_side: str, qty: float, price: float, positionSide: str):
    params = {"reduceOnly": True, "positionSide": positionSide}
    logger.info(f"Создаю limit закрывающий ордер {close_side} {qty} @ {price} params={params}")
    order = await exchange.create_order(symbol, "limit", close_side, qty, price, params)
    logger.info(f"TP/SL order response: {order}")
    return order

# -------------------------
# Position high-level logic
# -------------------------
last_trade_info: Optional[dict] = None
active_position = False

async def calculate_qty_for_usd(symbol: str, usd_amount: float, leverage: int) -> float:
    price = await fetch_price(symbol)
    qty = (usd_amount * leverage) / price
    if qty * price < MIN_ORDER_USD:
        qty = (MIN_ORDER_USD / price)
    qty = await amount_precision(symbol, qty)
    if qty < 0.000001:
        qty = 0.000001
    logger.info(f"Расчитанное количество: {qty} (USD {usd_amount} * L{leverage} / price {price})")
    return qty

async def open_position_from_signal(signal: str, fixed_amount_usd: Optional[float] = None):
    global active_position, last_trade_info
    try:
        if active_position:
            logger.info("Позиция уже активна — пропускаем открытие.")
            await tg_send("⚠️ Позиция уже активна — новый сигнал проигнорирован.")
            return

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

        if side == "buy":
            tp_price = round(entry_price * 1.015, 6)
            sl_price = round(entry_price * 0.99, 6)
        else:
            tp_price = round(entry_price * 0.985, 6)
            sl_price = round(entry_price * 1.01, 6)

        try:
            await create_tp_sl_limit(SYMBOL, close_side, qty, tp_price, positionSide)
            await create_tp_sl_limit(SYMBOL, close_side, qty, sl_price, positionSide)
        except Exception as e:
            logger.warning(f"Не удалось выставить TP/SL: {e}\n{traceback.format_exc()}")

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
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info("🚀 ЗАПУСК БОТА (lifespan startup)")
        try:
            balance = await fetch_balance_usdt()
        except Exception as e:
            balance = None
            logger.warning(f"Не удалось получить баланс при старте: {e}")
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
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое сообщение в tg: {e}")
        yield
    finally:
        logger.info("🛑 ОСТАНОВКА БОТА (lifespan shutdown)")
        try:
            await exchange.close()
        except Exception as e:
            logger.warning(f"Ошибка при закрытии exchange: {e}")
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
    balance = await fetch_balance_usdt()
    price = await fetch_price(SYMBOL)
    status = "АКТИВНА" if active_position else "НЕТ"
    html = f"""
    <html>
    <head><meta charset="utf-8"/><title>MEXC Futures Bot</title></head>
    <body>
    <h1>🤖 MEXC Futures Bot</h1>
    <p>Баланс: {balance}</p>
    <p>Цена: {price}</p>
    <p>Позиция: {status}</p>
    <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.post("/webhook")
async def webhook(request: Request):
    provided = request.headers.get("X-Webhook-Secret") or request.headers.get("Authorization")
    if provided is None:
        raise HTTPException(403, "No webhook secret provided")
    if provided.startswith("Bearer "):
        provided = provided.split(" ", 1)[1]
    if provided != WEBHOOK_SECRET:
        logger.warning("Invalid webhook secret")
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
    logger.info(f"Webhook accepted: {signal}")
    await tg_send(f"📨 Received signal: {signal.upper()}. Открытие позиции запланировано.")
    return {"status": "accepted", "signal": signal}

@app.get("/health")
async def health():
    try:
        price = await fetch_price(SYMBOL)
        balance = await fetch_balance_usdt()
        return {"status": "ok", "symbol": SYMBOL, "price": price, "balance": balance, "active_position": active_position, "timestamp": time.time()}
    except Exception as e:
        logger.error(f"Health failed: {e}")
        return {"status": "error", "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
