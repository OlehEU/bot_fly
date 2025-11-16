import os
import json
import asyncio
import logging
import time
import traceback
from contextlib import asynccontextmanager
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# =========================
# Логирование
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# =========================
# Секреты и настройки
# =========================
SECRETS = {
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),
    "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET"),
    "SYMBOL": os.getenv("SYMBOL", "BTC/USDT"),
    "FIXED_AMOUNT_USD": float(os.getenv("FIXED_AMOUNT_USD", 10)),
    "LEVERAGE": int(os.getenv("LEVERAGE", 5)),
}

# =========================
# Инициализация Telegram
# =========================
bot = None
if SECRETS["TELEGRAM_TOKEN"]:
    try:
        bot = Bot(token=SECRETS["TELEGRAM_TOKEN"])
    except Exception as e:
        logger.warning(f"⚠️ Telegram бот не инициализирован: {e}")

# =========================
# Инициализация MEXC
# =========================
exchange = None
if SECRETS["MEXC_API_KEY"] and SECRETS["MEXC_API_SECRET"]:
    exchange = ccxt.mexc({
        'apiKey': SECRETS["MEXC_API_KEY"],
        'secret': SECRETS["MEXC_API_SECRET"],
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'},
        'timeout': 30000,
    })

# =========================
# FastAPI
# =========================
app = FastAPI()
last_trade_info = None
active_position = False

# =========================
# Вспомогательные функции
# =========================
@asynccontextmanager
async def error_handler(operation: str):
    try:
        yield
    except Exception as e:
        msg = f"❌ Ошибка в {operation}: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        if bot:
            try:
                await bot.send_message(chat_id=int(SECRETS.get("TELEGRAM_CHAT_ID", 0)), text=msg[:4000])
            except:
                pass

async def get_current_price() -> float:
    if not exchange:
        return 0.0
    async with error_handler("get_current_price"):
        ticker = await exchange.fetch_ticker(SECRETS["SYMBOL"])
        return float(ticker['last'])

async def check_balance() -> float:
    if not exchange:
        return 0.0
    async with error_handler("check_balance"):
        balance_data = await exchange.fetch_balance()
        return float(balance_data['total'].get('USDT', 0))

async def set_leverage(symbol, leverage, side="long", margin_type="isolated"):
    if not exchange:
        return
    async with error_handler("set_leverage"):
        positionType = 1 if side.lower() == "long" else 2
        openType = 1 if margin_type == "isolated" else 2
        await exchange.set_leverage(leverage, symbol, {'openType': openType, 'positionType': positionType})
        logger.info(f"⚡ Плечо {leverage}x установлено для {symbol}, {side}, {margin_type}")

async def calculate_qty(fixed_amount_usd, leverage, price):
    qty = (fixed_amount_usd * leverage) / price
    qty = round(qty, 1)
    if qty < 1:
        qty = 1
    return qty

async def open_position(signal: str):
    global last_trade_info, active_position
    if not exchange:
        logger.warning("⚠️ MEXC не подключен. Пропускаем открытие позиции.")
        return

    async with error_handler("open_position"):
        side = "buy" if signal.lower() == "buy" else "sell"
        price = await get_current_price()
        qty = await calculate_qty(SECRETS["FIXED_AMOUNT_USD"], SECRETS["LEVERAGE"], price)
        await set_leverage(SECRETS["SYMBOL"], SECRETS["LEVERAGE"], side="long" if side=="buy" else "short")
        order = await exchange.create_market_order(
            SECRETS["SYMBOL"], side, qty, None,
            {'positionType': 1 if side=="buy" else 2}  # 1=long, 2=short
        )
        active_position = True
        last_trade_info = {
            "signal": signal,
            "side": side,
            "qty": qty,
            "price": price,
            "order": order,
            "timestamp": time.time()
        }
        msg = f"✅ {side.upper()} позиция открыта: {qty} {SECRETS['SYMBOL']} @ {price}"
        logger.info(msg)
        if bot:
            await bot.send_message(chat_id=int(SECRETS.get("TELEGRAM_CHAT_ID", 0)), text=msg)

# =========================
# FastAPI события
# =========================
@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Бот запущен")
    if bot:
        await bot.send_message(chat_id=int(SECRETS.get("TELEGRAM_CHAT_ID", 0)), text="✅ Бот запущен")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Бот остановлен")
    if exchange:
        await exchange.close()
    if bot:
        await bot.send_message(chat_id=int(SECRETS.get("TELEGRAM_CHAT_ID", 0)), text="🔴 Бот остановлен")

# =========================
# FastAPI маршруты
# =========================
@app.post("/webhook")
async def webhook(request: Request):
    if SECRETS["WEBHOOK_SECRET"]:
        if request.headers.get("Authorization") != f"Bearer {SECRETS['WEBHOOK_SECRET']}":
            raise HTTPException(401, detail="Unauthorized")
    data = await request.json()
    signal = data.get("signal")
    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
    asyncio.create_task(open_position(signal))
    return {"status": "ok", "message": f"{signal} signal received"}

@app.get("/health")
async def health_check():
    price = await get_current_price()
    balance = await check_balance()
    return {
        "status": "healthy",
        "exchange_connected": price > 0,
        "balance_available": balance > 0,
        "active_position": active_position,
        "current_price": price,
        "balance": balance,
        "symbol": SECRETS["SYMBOL"]
    }

@app.get("/")
async def home():
    price = await get_current_price()
    balance = await check_balance()
    status = "АКТИВНА" if active_position else "НЕТ"
    html = f"""
    <html>
    <head><title>MEXC Bot</title><meta charset="utf-8"></head>
    <body>
        <h1>MEXC Bot</h1>
        <p>Баланс USDT: {balance:.2f}</p>
        <p>Текущая цена: {price:.4f}</p>
        <p>Позиция: {status}</p>
        <pre>Последняя сделка: {json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
        <p><a href="/health">Health Check</a></p>
    </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
