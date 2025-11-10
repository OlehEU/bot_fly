import os
import datetime
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

# === Настройки из переменных окружения ===
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === Глобальные переменные состояния ===
last_signal = {"signal": None, "time": None}


def send_telegram(signal: str):
    """Отправка уведомления в Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram токен или chat_id не заданы — пропускаем уведомление")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    text = f"📈 Новый сигнал: {signal.upper()} 🚀"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print("❌ Ошибка при отправке в Telegram:", e)


@app.get("/", response_class=HTMLResponse)
async def home():
    """Главная страница статуса"""
    signal = last_signal["signal"] or "—"
    time = last_signal["time"] or "нет данных"

    html_content = f"""
    <html>
    <head>
        <title>🤖 Bybit Trading Bot</title>
        <style>
            body {{
                font-family: 'Segoe UI', sans-serif;
                background: linear-gradient(135deg, #1e1e2f, #2a2a40);
                color: #fff;
                text-align: center;
                padding: 50px;
            }}
            .card {{
                background: #2f2f46;
                border-radius: 15px;
                padding: 30px;
                box-shadow: 0 0 15px rgba(0,0,0,0.5);
                display: inline-block;
            }}
            h1 {{ color: #4cd137; }}
            .signal {{
                font-size: 2em;
                margin: 20px 0;
                color: {('#44bd32' if signal == 'buy' else '#e84118' if signal == 'sell' else '#aaa')};
            }}
            footer {{
                margin-top: 40px;
                color: #888;
                font-size: 0.9em;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🚀 Bybit Trading Bot</h1>
            <p>Статус: <strong>Работает</strong> ✅</p>
            <p>Последний сигнал:</p>
            <div class="signal">{signal.upper()}</div>
            <p>Время: {time}</p>
        </div>
        <footer>© {datetime.datetime.now().year} • Bot running on Fly.io</footer>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/webhook")
async def webhook(request: Request):
    """Webhook для сигналов из TradingView"""
    data = await request.json()
    signal = data.get("signal")

    if signal not in ["buy", "sell"]:
        return JSONResponse({"status": "error", "signal": signal})

    # Сохраняем состояние
    last_signal["signal"] = signal
    last_signal["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Уведомление в Telegram
    send_telegram(signal)

    return JSONResponse({"status": "ok", "signal": signal})
