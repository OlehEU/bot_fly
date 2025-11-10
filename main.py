import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pybit.unified_trading import HTTP
import requests

# === Настройки из переменных окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "True").lower() == "true"

TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
TRADE_TYPE = os.getenv("TRADE_TYPE", "futures").lower()  # "futures" или "spot"
LEVERAGE = int(os.getenv("LEVERAGE", 1))

BYBIT_ENDPOINT = "https://api.bybit.com"  # можно поменять, если тестнет другой

# === Инициализация Bybit клиента для фьючерсов ===
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, testnet=BYBIT_TESTNET)

# === Состояние последней сделки ===
last_trade = None

# === FastAPI приложение ===
app = FastAPI()

# === Функция для отправки уведомлений в Telegram ===
def send_telegram(message: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, json=data, timeout=5)
        except Exception as e:
            print(f"Telegram send failed: {e}")

# === Главная страница / статус бота ===
@app.get("/", response_class=HTMLResponse)
def read_root():
    global last_trade
    last_trade_str = last_trade if last_trade else "null"
    html_content = f"""
    <html>
      <head><title>Bybit Bot Status</title></head>
      <body style="font-family: Arial; padding:20px;">
        <h2>Bybit Trading Bot</h2>
        <ul>
          <li>Mode: {TRADE_TYPE.upper()}</li>
          <li>Symbol: {SYMBOL}</li>
          <li>Trade USD: {TRADE_USD}</li>
          <li>Min profit to sell: {MIN_PROFIT_USDT} USDT</li>
          <li>Leverage: {LEVERAGE}</li>
          <li>Bybit endpoint: {BYBIT_ENDPOINT}</li>
          <li>Testnet: {BYBIT_TESTNET}</li>
        </ul>
        <h3>Last trade:</h3>
        <pre>{last_trade_str}</pre>
        <p>Webhook: POST /webhook with JSON {{signal: 'buy'|'sell', optional: amount, symbol}}</p>
      </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# === Вебхук для сигналов покупки/продажи ===
@app.post("/webhook")
async def webhook(request: Request):
    global last_trade
    data = await request.json()

    signal = data.get("signal")
    amount = float(data.get("amount", TRADE_USD))
    symbol = data.get("symbol", SYMBOL)

    if signal not in ["buy", "sell"]:
        return JSONResponse({"status": "error", "message": "Invalid signal", "signal": signal})

    try:
        # Закрытие текущей позиции для фьючерсов
        if TRADE_TYPE == "futures":
            positions = session.get_position_list(symbol=symbol)["result"]
            for pos in positions:
                size = float(pos["size"])
                if size != 0:
                    side = "Sell" if pos["side"].lower() == "buy" else "Buy"
                    session.place_order(
                        symbol=symbol,
                        side=side,
                        order_type="Market",
                        qty=abs(size),
                        time_in_force="GoodTillCancel",
                    )
            # Установка плеча
            session.set_leverage(symbol=symbol, buy_leverage=LEVERAGE, sell_leverage=LEVERAGE)

        # Открытие новой позиции
        side = "Buy" if signal == "buy" else "Sell"
        session.place_order(
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=amount,
            time_in_force="GoodTillCancel",
        )

        last_trade = f"{signal.upper()} {amount} USD {symbol} ({TRADE_TYPE.upper()}, leverage {LEVERAGE}x)"
        send_telegram(f"✅ {last_trade}")

        return JSONResponse({"status": "ok", "signal": signal})
    except Exception as e:
        send_telegram(f"❌ Ошибка при открытии позиции: {e}")
        return JSONResponse({"status": "error", "message": str(e), "signal": signal})
