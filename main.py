import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request
from pybit.unified_trading import HTTP  # исправленный импорт
from telegram import Bot

# --- Настройки через ENV ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() == "true"
TRADE_TYPE = os.getenv("TRADE_TYPE", "futures")
LEVERAGE = int(os.getenv("LEVERAGE", 1))

# --- Логи ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bybit-bot")

# --- Telegram ---
bot = Bot(token=TELEGRAM_TOKEN)

# --- Bybit клиент ---
BYBIT_ENDPOINT = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
client = HTTP(
    endpoint=BYBIT_ENDPOINT,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)

# --- FastAPI ---
app = FastAPI()
last_trade_info = None

# --- Главная страница ---
@app.get("/")
async def home():
    global last_trade_info
    last_trade_text = json.dumps(last_trade_info, indent=2) if last_trade_info else "нет сделок"
    return f"""
    <html>
      <head><title>Bybit Bot Status</title></head>
      <body style="font-family: Arial; padding:20px; background-color:#1e1e1e; color:#f0f0f0;">
        <h2>Bybit Trading Bot</h2>
        <ul>
          <li>Mode: {TRADE_TYPE.upper()}</li>
          <li>Symbol: {SYMBOL}</li>
          <li>Trade USD: {TRADE_USD}</li>
          <li>Min profit to sell: {MIN_PROFIT_USDT} USDT</li>
          <li>Leverage: {LEVERAGE}</li>
          <li>Bybit endpoint: {BYBIT_ENDPOINT}</li>
        </ul>
        <h3>Last trade:</h3>
        <pre>{last_trade_text}</pre>
        <p>Webhook: POST /webhook with JSON {"{signal:'buy'|'sell', optional: amount, symbol}"}</p>
      </body>
    </html>
    """

# --- Функция открытия позиции ---
async def open_position(signal, amount=None, symbol=SYMBOL):
    global last_trade_info
    size = float(amount) if amount else TRADE_USD
    try:
        # Устанавливаем левередж один раз
        client.set_leverage(symbol=symbol, leverage=LEVERAGE)

        # Получаем текущие позиции
        positions = client.get_positions(symbol=symbol)["result"]["list"]

        # Закрываем все существующие позиции
        for pos in positions:
            pos_size = float(pos["size"])
            if pos_size != 0:
                side_to_close = "Sell" if pos["side"].lower() == "Buy".lower() else "Buy"
                client.place_active_order(
                    symbol=symbol,
                    side=side_to_close,
                    order_type="Market",
                    qty=abs(pos_size),
                    time_in_force="GoodTillCancel",
                    reduce_only=True
                )
                logger.info(f"Closed existing position: {side_to_close} {abs(pos_size)} {symbol}")

        # Открываем новую позицию
        order_side = "Buy" if signal == "buy" else "Sell"
        client.place_active_order(
            symbol=symbol,
            side=order_side,
            order_type="Market",
            qty=size,
            time_in_force="GoodTillCancel"
        )

        last_trade_info = {"signal": signal, "size": size, "symbol": symbol}
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"✅ {signal.upper()} executed: {size} {symbol}")
        logger.info(f"Executed {signal} {size} {symbol}")

    except Exception as e:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"❌ Ошибка при открытии позиции: {str(e)}")
        logger.error(f"Ошибка при открытии позиции: {str(e)}")

# --- Webhook ---
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    signal = data.get("signal")
    amount = data.get("amount")
    symbol = data.get("symbol", SYMBOL)
    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal должен быть 'buy' или 'sell'"}
    asyncio.create_task(open_position(signal, amount, symbol))
    return {"status": "ok", "message": f"{signal} получен"}
