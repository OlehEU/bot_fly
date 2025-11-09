from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from telegram import Bot
import os

app = FastAPI()

# Секреты Fly.io
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# Переменные для мини-страницы
last_signal = "нет данных"
status = "бот запущен"

@app.get("/", response_class=HTMLResponse)
async def home():
    html_content = f"""
    <html>
        <head>
            <title>Статус бота</title>
        </head>
        <body>
            <h1>Бот работает</h1>
            <p>Статус: {status}</p>
            <p>Последний сигнал: {last_signal}</p>
        </body>
    </html>
    """
    return html_content

@app.post("/webhook")
async def webhook(request: Request):
    global last_signal
    data = await request.json()
    signal = data.get("signal", "неизвестно")
    
    last_signal = signal  # Обновляем переменную для страницы

    # Отправляем сообщение в Telegram
    try:
        await bot.send_message(chat_id=CHAT_ID, text=f"Новый сигнал: {signal}")
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

    return {"status": "ok", "signal": signal}
