import os
import json
import asyncio
import logging
from functools import wraps
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
import ccxt.async_support as ccxt

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = os.getenv("SYMBOL", "XRP/USDT:USDT")
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC V3 ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'sandbox': False,
    'version': 'v3',  # V3 API
    'options': {'defaultType': 'swap'},
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === RETRY ===
def retry_on_error(max_retries: int = 5, delay: int = 3):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if any(code in str(e) for code in ["403", "429", "1002"]) and attempt < max_retries - 1:
                        logger.warning(f"{e} — повтор через {delay}s")
                        await asyncio.sleep(delay)
                        continue
                    logger.error(f"Критическая ошибка: {e}")
                    raise
            return None
        return wrapper
    return decorator

# === Баланс ===
@retry_on_error()
async def check_balance() -> float:
    try:
        bal = await exchange.fetch_balance()
        usdt = bal['total'].get('USDT', 0)
        logger.info(f"Баланс USDT: {usdt:.4f}")
        return float(usdt)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        return 0.0

# === РАСЧЁТ qty ===
async def calculate_qty(usd_amount: float) -> float:
    try:
        markets = await exchange.load_markets()
        market = markets[SYMBOL]
        min_qty = market['limits']['amount']['min']
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
        raw_qty = usd_amount / price
        qty = exchange.amount_to_precision(SYMBOL, raw_qty)
        qty = max(float(qty), min_qty)
        logger.info(f"qty={qty} price={price}")
        return qty
    except Exception as e:
        logger.error(f"Ошибка qty: {e}")
        return 0.0

# === Старт ===
@app.on_event("startup")
async def startup_notify():
    try:
        logger.info("=== ЗАПУСК БОТА ===")
        balance = await asyncio.wait_for(check_balance(), timeout=15)
        msg = f"MEXC Бот запущен!\nСимвол: {SYMBOL}\nРиск: {RISK_PERCENT}%\nПлечо: {LEVERAGE}x\nБаланс: {balance:.2f} USDT"
        await asyncio.wait_for(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg), timeout=10)
        logger.info("Стартовое уведомление отправлено.")
    except asyncio.TimeoutError:
        logger.error("Таймаут при старте")
    except Exception as e:
        logger.error(f"ОШИБКА ПРИ СТАРТЕ: {e}")

# === Главная ===
@app.get("/", response_class=HTMLResponse)
async def home():
    global last_trade_info, active_position
    last = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет"
    status = "Активна" if active_position else "Нет"
    return f"""<html><head><title>MEXC Bot</title><meta charset="utf-8"></head>
    <body style="font-family:Arial;background:#1e1e1e;color:#e0e0e0;padding:20px;">
      <h2 style="color:#00b894;">MEXC Futures Bot</h2>
      <ul><li><b>Символ:</b> {SYMBOL}</li><li><b>Риск:</b> {RISK_PERCENT}%</li><li><b>Плечо:</b> {LEVERAGE}×</li><li><b>Позиция:</b> {status}</li></ul>
      <h3>Последняя сделка:</h3><pre style="background:#2d2d2d;padding:10px;">{last}</pre>
      <p><b>Webhook:</b> <code>POST /webhook</code> <b>Header:</b> <code>Authorization: Bearer {WEBHOOK_SECRET}</code></p>
      <a href="/balance">Баланс</a>
    </body></html>"""

# === Баланс ===
@app.get("/balance", response_class=HTMLResponse)
async def get_balance():
    bal = await check_balance()
    req = bal * RISK_PERCENT / 100 * 1.1
    return f"""<html><body style="font-family:Arial;background:#1e1e1e;color:#e0e0e0;padding:20px;">
      <h2>Баланс: {bal:.2f} USDT</h2>
      <p>Требуется: {req:.2f} USDT</p>
      <a href="/">Назад</a></body></html>"""

# === Открытие позиции ===
async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position
    if active_position:
        return

    try:
        bal = await check_balance()
        usd = amount_usd or (bal * RISK_PERCENT / 100)
        if usd < 5:
            raise ValueError("Маленький лот")

        qty = await calculate_qty(usd)
        side = "buy" if signal == "buy" else "sell"
        pos_type = 1 if side == "buy" else 2

        await exchange.set_leverage(LEVERAGE, SYMBOL, params={'openType': 1, 'positionType': pos_type})

        pos = await exchange.fetch_positions([SYMBOL])
        for p in pos:
            if p['contracts'] > 0:
                cs = 'sell' if p['side'] == 'long' else 'buy'
                await exchange.create_market_order(
                    SYMBOL, cs, p['contracts'],
                    params={'openType': 1, 'positionType': pos_type, 'leverage': LEVERAGE}
                )

        order = await exchange.create_market_order(
            SYMBOL, side, qty,
            params={'openType': 1, 'positionType': pos_type, 'leverage': LEVERAGE}
        )
        entry = order['average'] or order['price']
        tp = round(entry * (1.015 if side == "buy" else 0.985), 6)
        sl = round(entry * (0.99 if side == "buy" else 1.01), 6)

        await exchange.create_order(
            SYMBOL, 'limit', 'sell' if side == "buy" else 'buy', qty, tp,
            params={'openType': 1, 'positionType': pos_type, 'leverage': LEVERAGE, 'reduceOnly': True}
        )
        await exchange.create_order(
            SYMBOL, 'limit', 'sell' if side == "buy" else 'buy', qty, sl,
            params={'openType': 1, 'positionType': pos_type, 'leverage': LEVERAGE, 'reduceOnly': True}
        )

        active_position = True
        last_trade_info = {"signal": signal, "qty": qty, "entry": entry, "tp": tp, "sl": sl}
        msg = f"{side.upper()} {qty} {SYMBOL}\nEntry: ${entry}\nTP: ${tp} | SL: ${sl}\nБаланс: {bal:.2f}"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)

    except Exception as e:
        err = f"Ошибка {signal}: {e}\nБаланс: {await check_balance():.2f}"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err)
        logger.error(err)
        active_position = False

# === Webhook ===
@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401)
    data = await request.json()
    signal = data.get("signal")
    amount = data.get("amount")
    if signal not in ["buy", "sell"]:
        return {"status": "error"}
    asyncio.create_task(open_position(signal, amount))
    return {"status": "ok"}

# === Graceful shutdown ===
@app.on_event("shutdown")
async def shutdown():
    await exchange.close()

# === Запуск ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
