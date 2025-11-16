import os
import json
import asyncio
import logging
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
import ccxt.async_support as ccxt

# === Проверка секретов ===
REQUIRED_SECRETS = [
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MEXC_API_KEY",
    "MEXC_API_SECRET",
    "WEBHOOK_SECRET",
]
missing = [s for s in REQUIRED_SECRETS if not os.getenv(s)]
if missing:
    raise EnvironmentError(f"Не заданы секреты: {', '.join(missing)}")

# === Config ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = os.getenv("SYMBOL", "XRP_USDT")   # <-- ИСПРАВЛЕНО
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_USD = float(os.getenv("MIN_USD", 5))

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# === Telegram Bot ===
bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    """Асинхронная отправка сообщений в Telegram."""
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Сообщение отправлено в Telegram")
    except Exception as e:
        logger.error(f"Ошибка Telegram: {e}")

# === MEXC Futures (USDT-M) ===
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},   # USDT-M Futures
})

# === Retry wrapper ===
def retry(max_retries=4, delay=2):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if i < max_retries - 1:
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Ошибка {func.__name__}: {e}")
                        raise
        return wrapper
    return decorator

# === Balance ===
@retry()
async def check_balance():
    balance = await exchange.fetch_balance()
    usdt = float(balance["total"].get("USDT", 0))
    logger.info(f"Баланс USDT: {usdt}")
    return usdt

# === Кол-во ===
@retry()
async def calculate_qty(usd_amount: float):
    ticker = await exchange.fetch_ticker(SYMBOL)
    price = ticker["last"]
    raw_qty = usd_amount / price
    qty = float(exchange.amount_to_precision(SYMBOL, raw_qty))
    return qty

# === Открытие позиции ===
last_trade_info: Optional[dict] = None
active_position = False

@retry()
async def open_position(signal: str):
    global active_position, last_trade_info

    if active_position:
        logger.info("Позиция уже активна")
        return

    balance = await check_balance()
    usd = balance * RISK_PERCENT / 100

    if usd < MIN_USD:
        await tg_send(f"❗ Объем слишком мал: {usd:.2f} USD")
        return

    qty = await calculate_qty(usd)

    side = "buy" if signal == "buy" else "sell"
    position_type = 1 if signal == "buy" else 2

    # === Установка плеча ===
    try:
        await exchange.set_leverage(
            LEVERAGE,
            SYMBOL,
            params={"positionType": position_type}
        )
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e}")

    # === Маркет ордер ===
    order = await exchange.create_order(
        SYMBOL, "market", side, qty,
        params={"positionType": position_type}
    )

    entry = order.get("average") or order["price"]

    # === TP SL ===
    if side == "buy":
        tp = round(entry * 1.015, 6)
        sl = round(entry * 0.99, 6)
    else:
        tp = round(entry * 0.985, 6)
        sl = round(entry * 1.01, 6)

    close_side = "sell" if side == "buy" else "buy"

    # TP
    await exchange.create_order(
        SYMBOL, "limit", close_side, qty, tp,
        params={"reduceOnly": True}
    )
    # SL
    await exchange.create_order(
        SYMBOL, "limit", close_side, qty, sl,
        params={"reduceOnly": True}
    )

    active_position = True
    last_trade_info = {
        "signal": signal,
        "qty": qty,
        "entry": entry,
        "tp": tp,
        "sl": sl,
    }

    await tg_send(
        f"✅ <b>{signal.upper()} — ОТКРЫТА</b>\n"
        f"Qty: {qty}\n"
        f"Entry: {entry}\n"
        f"TP: {tp}\nSL: {sl}\n"
        f"Баланс: {balance:.2f} USDT"
    )

# === FastAPI ===
app = FastAPI()

# === Telegram уведомление при запуске ===
@app.on_event("startup")
async def startup_event():
    logger.info("Бот запущен")
    balance = await check_balance()
    await tg_send(
        f"🚀 <b>Бот запущен</b>\n"
        f"Символ: {SYMBOL}\n"
        f"Плечо: {LEVERAGE}x\n"
        f"Баланс: {balance:.2f} USDT"
    )

@app.on_event("shutdown")
async def shutdown_event():
    await tg_send("🛑 Бот остановлен")

# === Главная страница ===
@app.get("/", response_class=HTMLResponse)
async def home():
    balance = await check_balance()
    return f"""
    <html>
    <body style="font-family:Arial;background:#111;color:#eee;padding:20px;">
      <h2>MEXC Futures Bot</h2>
      <p><b>Символ:</b> {SYMBOL}</p>
      <p><b>Баланс:</b> {balance:.2f} USDT</p>
      <p><b>Риск:</b> {RISK_PERCENT}%</p>
      <p><b>Плечо:</b> {LEVERAGE}x</p>
      <p><b>Позиция активна:</b> {active_position}</p>
      <h3>Последняя сделка:</h3>
      <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет"}</pre>
      <hr>
      <p>Webhook TradingView:</p>
      <code>https://bot-fly-oz.fly.dev/webhook</code>
    </body>
    </html>
    """

# === Webhook ===
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")

    data = await request.json()
    signal = data.get("signal")

    if signal not in ("buy", "sell"):
        raise HTTPException(400, "signal must be buy or sell")

    asyncio.create_task(open_position(signal))
    return {"status": "accepted", "signal": signal}
