# main.py
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pybit import usdt_perpetual
import requests

# === Настройки из переменных окружения ===
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "True") == "True"
TRADE_TYPE = os.getenv("TRADE_TYPE", "futures")
LEVERAGE = int(os.getenv("LEVERAGE", 1))

# Bybit endpoint
BYBIT_ENDPOINT = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

# Клиент Bybit USDT фьючерсов
client = usdt_perpetual.HTTP(
    endpoint=BYBIT_ENDPOINT,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)

app = FastAPI()
last_trade = None

def send_telegram(msg: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram send error:", e)

@app.get("/", response_class=HTMLResponse)
async def home():
    html_content = f"""
    <html>
      <head>
        <title>Bybit Bot Status</title>
        <style>
          body {{
            background-color: #1a1a1a;
            color: #f0f0f0;
            font-family: Arial, sans-serif;
            padding: 20px;
          }}
          h2 {{ color: #00ff99; }}
          pre {{
            background-color: #333;
            padding: 10px;
            border-radius: 8px;
          }}
          ul {{ list-style-type: none; padding: 0; }}
          li {{ margin-bottom: 5px; }}
        </style>
      </head>
      <body>
        <h2>Bybit Trading Bot</h2>
        <ul>
          <li>Mode: {TRADE_TYPE.upper()}</li>
          <li>Symbol: {SYMBOL}</li>
          <li>Trade USD: {TRADE_USD}</li>
          <li>Min profit to sell: {MIN_PROFIT_USDT} USDT</li>
          <li>Leverage (futures): {LEVERAGE}</li>
          <li>Bybit endpoint: {BYBIT_ENDPOINT}</li>
        </ul>
        <h3>Last trade:</h3>
        <pre>{last_trade}</pre>
        <p>Webhook: POST /webhook with JSON {"{"}signal: 'buy'|'sell', optional: amount, symbol{"}"}</p>
      </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/webhook")
async def webhook(request: Request):
    global last_trade
    data = await request.json()
    signal = data.get("signal")
    amount = float(data.get("amount", TRADE_USD))
    symbol = data.get("symbol", SYMBOL)
    
    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal must be 'buy' or 'sell'"}

    try:
        if signal == "buy":
            # Открываем позицию
            client.place_active_order(
                symbol=symbol,
                side="Buy",
                order_type="Market",
                qty=amount,
                time_in_force="GoodTillCancel",
                reduce_only=False,
                close_on_trigger=False
            )
            msg = f"✅ Открыта BUY позиция {amount} USD {symbol} с плечом {LEVERAGE}"
        else:
            # Закрываем позицию (reduce_only)
            client.place_active_order(
                symbol=symbol,
                side="Sell",
                order_type="Market",
                qty=amount,
                time_in_force="GoodTillCancel",
                reduce_only=True
            )
            msg = f"✅ Закрыта SELL позиция {amount} USD {symbol}"

        last_trade = msg
        send_telegram(msg)
        return {"status": "ok", "signal": signal}
    
    except Exception as e:
        err_msg = f"❌ Ошибка при открытии/закрытии позиции: {e}"
        last_trade = err_msg
        send_telegram(err_msg)
        return {"status": "error", "message": str(e)}
