import os
import json
import asyncio
from fastapi import FastAPI, Request
from pybit import HTTP
import httpx
import logging
from telegram import Bot

# --- Настройки и секреты через ENV ---
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

# --- Настройка логов ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bybit-bot")

# --- Telegram бот ---
bot = Bot(token=TELEGRAM_TOKEN)

# --- Bybit клиент ---
BYBIT_ENDPOINT = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
client = HTTP(
    BYBIT_ENDPOINT,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)

# --- FastAPI ---
app = FastAPI()
last_trade_info = None

# --- Главная страница (красивая заглушка) ---
@app.get("/")
async def home():
    global last_trade_info
    last_trade_text = json.dumps(last_trade_info, indent=2) if last_trade_info else "нет сделок"
    return f"""
    <html>
      <head>
        <title>Bybit Bot Status</title>
      </head>
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

# --- Вспомогательная функция для открытия позиции ---
async def open_position(signal, amount=None, symbol=SYMBOL):
    global last_trade_info
    size = amount if amount else TRADE_USD
    try:
        # Получаем текущие позиции
        positions = client.get_positions(symbol=symbol)["result"]

        # Пример простой логики: если есть открытая позиция, закрываем её
        for pos in positions:
            if float(pos["size"]) > 0:
                client.close_position(symbol=symbol)

        if signal == "buy":
            client.place_active_order(
                symbol=symbol,
                side="Buy",
                order_type="Market",
                qty=size,
                time_in_force="GoodTillCancel",
                leverage=LEVERAGE
            )
        elif signal == "sell":
            client.place_active_order(
                symbol=symbol,
                side="Sell",
                order_type="Market",
                qty=size,
                time_in_force="GoodTillCancel",
                leverage=LEVERAGE
            )
        last_trade_info = {"signal": signal, "size": size, "symbol": symbol}
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"✅ {signal.upper()} executed: {size} {symbol}")
        logger.info(f"Executed {signal} {size} {symbol}")
    except Exception as e:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"❌ Ошибка при открытии позиции: {str(e)}")
        logger.error(f"Ошибка при открытии позиции: {str(e)}")

# --- Webhook для сигналов ---
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
