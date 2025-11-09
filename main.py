from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pybit.unified_trading import HTTP
import os
import asyncio
from datetime import datetime

# === Настройки из переменных окружения ===
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === Инициализация клиентов ===
client = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
app = FastAPI()

last_signal = {"signal": None, "time": None}


@app.get("/", response_class=HTMLResponse)
async def home():
    """Главная страница статуса"""
    html = f"""
    <html>
        <head>
            <title>Bybit Trading Bot</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    background: #f4f4f9;
                    color: #333;
                    text-align: center;
                    padding: 40px;
                }}
                .card {{
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                    display: inline-block;
                    padding: 20px 40px;
                }}
                .status-ok {{ color: green; }}
                .status-err {{ color: red; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>🤖 Bybit Trading Bot</h1>
                <p>Status: <b class="status-ok">Running</b></p>
                <p>Last signal: {last_signal['signal'] or '—'}</p>
                <p>Last update: {last_signal['time'] or '—'}</p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/webhook")
async def webhook(request: Request):
    """Прием сигнала от TradingView"""
    data = await request.json()
    signal = data.get("signal")

    if signal not in ["buy", "sell"]:
        return JSONResponse({"status": "error", "message": "Invalid signal"})

    last_signal["signal"] = signal
    last_signal["time"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Отправка ордера
    try:
        client.place_order(
            category="spot",
            symbol="SOLUSDT",
            side="Buy" if signal == "buy" else "Sell",
            orderType="Market",
            qty="0.1"
        )
        status = "ok"
    except Exception as e:
        status = "error"
        print(f"Ошибка при создании ордера: {e}")

    return JSONResponse({"status": status, "signal": signal})
