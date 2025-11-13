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

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = os.getenv("SYMBOL", "XRP/USDT:USDT")  # ← ФЬЮЧЕРСЫ!
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_USD = float(os.getenv("MIN_USD", 5.0))

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# === Telegram (в отдельном потоке) ===
bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    def _sync_send():
        try:
            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")
    try:
        await asyncio.to_thread(_sync_send)
    except Exception as e:
        logger.error(f"Telegram thread error: {e}")

# === MEXC V3 (Futures) ===
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# === Retry ===
def retry_on_error(max_retries: int = 4, delay: int = 2):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    text = str(e)
                    if attempt < max_retries and any(code in text for code in ["429", "timeout", "ETIMEDOUT"]):
                        logger.warning(f"Retry {attempt}/{max_retries}: {e}")
                        await asyncio.sleep(delay)
                        continue
                    logger.exception(f"Final error in {func.__name__}: {e}")
                    raise
            return None
        return wrapper
    return decorator

# === Баланс ===
@retry_on_error()
async def check_balance() -> float:
    bal = await exchange.fetch_balance()
    usdt = bal.get("total", {}).get("USDT", 0) or bal.get("free", {}).get("USDT", 0)
    try:
        usdt = float(usdt)
    except:
        usdt = 0.0
    logger.info(f"Баланс USDT: {usdt:.4f}")
    return usdt

# === Расчёт qty ===
@retry_on_error()
async def calculate_qty(usd_amount: float) -> float:
    await exchange.load_markets()
    market = exchange.markets[SYMBOL]
    ticker = await exchange.fetch_ticker(SYMBOL)
    price = ticker["last"]
    raw_qty = usd_amount / price
    qty = exchange.amount_to_precision(SYMBOL, raw_qty)
    qty = max(float(qty), market["limits"]["amount"]["min"])
    logger.info(f"qty: {usd_amount} USD → {qty} @ {price}")
    return qty

# === Открытие позиции ===
last_trade_info: Optional[dict] = None
active_position = False

@retry_on_error()
async def open_position(signal: str, amount_usd: Optional[float] = None):
    global last_trade_info, active_position
    if active_position:
        logger.info("Позиция уже открыта — пропуск.")
        return

    try:
        balance = await check_balance()
        usd = amount_usd or (balance * RISK_PERCENT / 100.0)
        if usd < MIN_USD:
            raise ValueError(f"Маленький лот: {usd:.2f} USD < {MIN_USD}")

        qty = await calculate_qty(usd)
        side = "buy" if signal == "buy" else "sell"
        positionSide = "LONG" if signal == "buy" else "SHORT"
        pos_type = 1 if signal == "buy" else 2

        # Плечо
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL, params={"openType": 1, "positionType": pos_type})
        except Exception as e:
            logger.warning(f"set_leverage: {e}")

        # Закрываем старую
        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            contracts = pos.get("contracts", 0)
            if contracts > 0:
                close_side = "sell" if pos["side"] == "long" else "buy"
                await exchange.create_order(
                    SYMBOL, "market", close_side, contracts,
                    params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE, "reduceOnly": True}
                )

        # Открываем новую
        order = await exchange.create_order(
            SYMBOL, "market", side, qty,
            params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE}
        )
        entry = order.get("average") or order.get("price") or (await exchange.fetch_ticker(SYMBOL))["last"]

        tp = round(float(entry) * (1.015 if side == "buy" else 0.985), 6)
        sl = round(float(entry) * (0.99 if side == "buy" else 1.01), 6)

        # TP/SL
        await exchange.create_order(
            SYMBOL, "limit", "sell" if side == "buy" else "buy", qty, tp,
            params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE, "reduceOnly": True}
        )
        await exchange.create_order(
            SYMBOL, "limit", "sell" if side == "buy" else "buy", qty, sl,
            params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE, "reduceOnly": True}
        )

        active_position = True
        last_trade_info = {"signal": signal, "qty": qty, "entry": entry, "tp": tp, "sl": sl}

        await tg_send(
            f"<b>{signal.upper()} EXECUTED</b>\n"
            f"<code>{qty}</code> <b>{SYMBOL}</b>\n"
            f"Entry: <code>{entry}</code</code>\n"
            f"TP: <code>{tp}</code> | SL: <code>{sl}</code>\n"
            f"Баланс: <code>{balance:.2f}</code> USDT"
        )

    except Exception as e:
        active_position = False
        await tg_send(
            f"<b>ERROR {signal}</b>\n"
            f"<code>{e}</code>\n"
            f"Баланс: <code>{await check_balance():.2f}</code> USDT"
        )
        logger.exception("open_position error")

# === FastAPI ===
app = FastAPI()

@app.on_event("startup")
async def startup_notify():
    try:
        await exchange.load_markets()
        balance = await check_balance()
        await tg_send(
            f"<b>MEXC Bot запущен</b>\n"
            f"Символ: <code>{SYMBOL}</code>\n"
            f"Риск: <code>{RISK_PERCENT}%</code>\n"
            f"Плечо: <code>{LEVERAGE}x</code>\n"
            f"Баланс: <code>{balance:.2f}</code> USDT"
        )
    except Exception as e:
        logger.error(f"Startup error: {e}")

@app.on_event("shutdown")
async def shutdown():
    await exchange.close()

@app.get("/", response_class=HTMLResponse)
async def home():
    last = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет"
    status = "Активна" if active_position else "Нет"
    return f"""
    <html><head><meta charset="utf-8"><title>MEXC Bot</title></head>
    <body style="font-family:Arial;background:#1e1e1e;color:#e0e0e0;padding:20px;">
      <h2 style="color:#00b894;">MEXC Futures Bot</h2>
      <ul>
        <li><b>Символ:</b> {SYMBOL}</li>
        <li><b>Риск:</b> {RISK_PERCENT}%</li>
        <li><b>Плечо:</b> {LEVERAGE}×</li>
        <li><b>Позиция:</b> {status}</li>
      </ul>
      <h3>Последняя сделка:</h3>
      <pre style="background:#2d2d2d;padding:10px;">{last}</pre>
      <p><b>Webhook:</b> POST /webhook <br>Header: <code>Authorization: Bearer {WEBHOOK_SECRET}</code></p>
      <a href="/balance">Баланс</a>
    </body></html>
    """

@app.get("/balance", response_class=HTMLResponse)
async def get_balance():
    bal = await check_balance()
    req = bal * RISK_PERCENT / 100
    return f"""
    <html><body style="font-family:Arial;background:#1e1e1e;color:#e0e0e0;padding:20px;">
      <h2>Баланс: {bal:.2f} USDT</h2>
      <p>Риск: {req:.2f} USDT</p>
      <a href="/">Назад</a>
    </body></html>
    """

@app.post("/webhook")
async def webhook(request: Request):
    auth = request.headers.get("Authorization", "")
    if WEBHOOK_SECRET and auth != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, "Unauthorized")
    data = await request.json()
    signal = data.get("signal")
    amount = data.get("amount")
    if signal not in ("buy", "sell"):
        raise HTTPException(400, "signal must be 'buy' or 'sell'")
    asyncio.create_task(open_position(signal, amount))
    return {"status": "ok"}

# === Запуск ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
