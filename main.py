import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request
from telegram import Bot
from pybit.unified_trading import HTTP

# === Настройки из окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() == "true"
TRADE_TYPE = os.getenv("TRADE_TYPE", "linear")
LEVERAGE = int(os.getenv("LEVERAGE", 1))

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bybit-bot")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === Bybit client (PyBit v5.13+) ===
client = HTTP(
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    testnet=BYBIT_TESTNET
)

# === FastAPI ===
app = FastAPI()
last_trade_info = None


# === Отправка уведомления в Telegram при старте ===
@app.on_event("startup")
async def startup_notify():
    try:
        env = "Тестнет" if BYBIT_TESTNET else "Продакшн"
        msg = f"🤖 Бот успешно запущен!\n\n" \
              f"⚙️ Режим: {env}\n" \
              f"📈 Торговля: {TRADE_TYPE}\n" \
              f"💰 Символ: {SYMBOL}\n" \
              f"📊 Лот: {TRADE_USD} USDT\n" \
              f"⚡ Плечо: {LEVERAGE}x"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Telegram-уведомление отправлено при старте.")
    except Exception as e:
        logger.error(f"Ошибка при отправке стартового уведомления: {e}")


# === Главная страница ===
@app.get("/")
async def home():
    global last_trade_info
    last_trade_text = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет активных сделок"
    endpoint = "Тестнет" if BYBIT_TESTNET else "Продакшн"

    return f"""
    <html>
      <head><title>Bybit Bot Status</title></head>
      <body style="font-family: Arial; padding:20px; background-color:#1e1e1e; color:#e0e0e0;">
        <h2 style="color:#00b894;">🚀 Bybit Trading Bot</h2>
        <ul>
          <li><b>Mode:</b> {TRADE_TYPE.upper()}</li>
          <li><b>Symbol:</b> {SYMBOL}</li>
          <li><b>Trade USD:</b> {TRADE_USD}</li>
          <li><b>Min profit:</b> {MIN_PROFIT_USDT} USDT</li>
          <li><b>Leverage:</b> {LEVERAGE}×</li>
          <li><b>Environment:</b> {endpoint}</li>
        </ul>
        <h3>Последняя сделка:</h3>
        <pre style="background-color:#2d2d2d; padding:10px; border-radius:8px;">{last_trade_text}</pre>
        <p>Webhook URL: <code>POST /webhook</code><br>
        Пример JSON: <code>{{"signal":"buy"}}</code> или <code>{{"signal":"sell"}}</code></p>
      </body>
    </html>
    """


# === Функция для открытия позиции ===
async def open_position(signal: str, amount=None, symbol: str = SYMBOL):
    global last_trade_info
    try:
        side = "Buy" if signal.lower() == "buy" else "Sell"
        size = float(amount) if amount else TRADE_USD

        # Устанавливаем плечо
        client.set_leverage(
            category=TRADE_TYPE,
            symbol=symbol,
            buyLeverage=str(LEVERAGE),
            sellLeverage=str(LEVERAGE)
        )

        # Проверяем и закрываем открытые позиции
        positions = client.get_positions(category=TRADE_TYPE, symbol=symbol)
        for pos in positions.get("result", {}).get("list", []):
            if float(pos["size"]) > 0:
                opp_side = "Sell" if pos["side"] == "Buy" else "Buy"
                client.place_order(
                    category=TRADE_TYPE,
                    symbol=symbol,
                    side=opp_side,
                    orderType="Market",
                    qty=pos["size"],
                    timeInForce="IOC"
                )
                logger.info(f"Закрыл позицию {pos['side']} размером {pos['size']}")

        # Открываем новую позицию
        order = client.place_order(
            category=TRADE_TYPE,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=size,
            timeInForce="IOC"
        )

        last_trade_info = {
            "signal": signal,
            "side": side,
            "size": size,
            "symbol": symbol,
            "order_id": order.get("result", {}).get("orderId"),
        }

        msg = f"✅ Исполнен ордер: {side} {size} {symbol}"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(msg)

    except Exception as e:
        err_msg = f"❌ Ошибка при исполнении {signal}: {e}"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        logger.error(err_msg)


# === Webhook для сигналов ===
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    signal = data.get("signal")
    amount = data.get("amount")
    symbol = data.get("symbol", SYMBOL)

    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal должен быть 'buy' или 'sell'"}

    asyncio.create_task(open_position(signal, amount, symbol))
    return {"status": "ok", "message": f"{signal} сигнал получен"}
