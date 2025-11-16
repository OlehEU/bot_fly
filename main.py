import os
import json
import asyncio
import logging
from functools import wraps
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
    raise EnvironmentError(f"ОШИБКА: не заданы секреты: {', '.join(missing)}")

# === Config ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = os.getenv("SYMBOL", "XRP/USDT:USDT")
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_USD = float(os.getenv("MIN_USD", 5))

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# === Telegram Bot ===
bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    def sync_task():
        try:
            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    await asyncio.to_thread(sync_task)

# === MEXC Exchange (futures) ===
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# === Retry wrapper ===
def retry(max_retries=4, delay=2):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if i < max_retries - 1:
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Final error in {func.__name__}: {e}")
                        raise
        return wrapper
    return decorator

# === Balance ===
@retry()
async def check_balance():
    bal = await exchange.fetch_balance()
    usdt = float(bal["total"].get("USDT", 0))
    logger.info(f"Баланс USDT: {usdt}")
    return usdt

# === Qty calculation ===
@retry()
async def calculate_qty(usd_amount: float):
    await exchange.load_markets()
    ticker = await exchange.fetch_ticker(SYMBOL)
    price = ticker["last"]
    raw_qty = usd_amount / price
    qty = float(exchange.amount_to_precision(SYMBOL, raw_qty))
    return qty

# === Position open ===
last_trade_info: Optional[dict] = None
active_position = False

@retry()
async def open_position(signal: str, fixed_amount_usd: Optional[float] = None):
    global active_position, last_trade_info

    if active_position:
        logger.info("Позиция уже открыта")
        return

    balance = await check_balance()
    usd = fixed_amount_usd or balance * RISK_PERCENT / 100

    if usd < MIN_USD:
        await tg_send(f"❗ Слишком маленький объем: {usd:.2f} USD")
        return

    qty = await calculate_qty(usd)

    side = "buy" if signal == "buy" else "sell"
    pos_type = 1 if signal == "buy" else 2

    # Плечо
    try:
        await exchange.set_leverage(LEVERAGE, SYMBOL, params={"openType": 1, "positionType": pos_type})
    except Exception as e:
        logger.warning(f"Ошибка установки плеча: {e}")

    # Открываем сделку
    order = await exchange.create_order(
        SYMBOL, "market", side, qty,
        params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE}
    )

    entry = order.get("average") or order.get("price") or (await exchange.fetch_ticker(SYMBOL))["last"]

    tp = round(entry * (1.015 if side == "buy" else 0.985), 6)
    sl = round(entry * (0.99 if side == "buy" else 1.01), 6)

    # TP/SL
    close_side = "sell" if side == "buy" else "buy"

    await exchange.create_order(
        SYMBOL, "limit", close_side, qty, tp,
        params={"reduceOnly": True}
    )
    await exchange.create_order(
        SYMBOL, "limit", close_side, qty, sl,
        params={"reduceOnly": True}
    )

    active_position = True
    last_trade_info = {"signal": signal, "qty": qty, "entry": entry, "tp": tp, "sl": sl}

    await tg_send(
        f"✅ <b>{signal.upper()} OPENED</b>\n"
        f"Qty: <code>{qty}</code>\n"
        f"Entry: <code>{entry}</code>\n"
        f"TP: <code>{tp}</code>\nSL: <code>{sl}</code>\n"
        f"Баланс: {balance:.2f} USDT"
    )

# === FastAPI ===
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def home():
    return f"""
    <html>
    <body style="font-family:Arial;background:#111;color:#eee;padding:20px;">
      <h2>MEXC Futures Bot</h2>
      <p><b>Символ:</b> {SYMBOL}</p>
      <p><b>Риск:</b> {RISK_PERCENT}%</p>
      <p><b>Плечо:</b> {LEVERAGE}x</p>
      <p><b>Позиция активна:</b> {active_position}</p>
      <h3>Последняя сделка:</h3>
      <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет"}</pre>
      <hr>
      <p>Webhook для TradingView: <br>
      <code>https://bot-fly-oz.fly.dev/webhook</code></p>
    </body>
    </html>
    """

@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Webhook-Secret")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")

    data = await request.json()
    signal = data.get("signal")

    if signal not in ("buy", "sell"):
        raise HTTPException(400, "signal must be 'buy' or 'sell'")

    asyncio.create_task(open_position(signal))
    return {"status": "accepted", "signal": signal}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
