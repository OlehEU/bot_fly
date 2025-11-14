import os
import json
import asyncio
import logging
import time
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! fly secrets set {secret}=...")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = "XRP/USDT:USDT"
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === Инициализация биржи ===
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap"
    }
})

app = FastAPI()
last_trade_info = None
active_position = False


# ================================
# 📌 Вспомогательные функции
# ================================

async def get_current_price():
    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = float(ticker["last"])
        logger.info(f"Текущая цена {SYMBOL}: {price}")
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return 0.0


async def check_balance():
    try:
        balance = await exchange.fetch_balance()
        usdt = float(balance["total"].get("USDT", 0))
        logger.info(f"Баланс USDT: {usdt}")
        return usdt
    except Exception as e:
        logger.error(f"Ошибка получения баланса: {e}")
        return 0.0


async def calculate_qty(usd_amount: float):
    try:
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Цена не валидна")

        qty = usd_amount / price
        qty = round(qty, 1)

        if qty < 1:
            qty = 1.0

        return qty

    except Exception as e:
        logger.error(f"Ошибка расчета qty: {e}")
        return 0.0


# ================================
# ⚙ Правильный set_leverage для MEXC
# ================================
async def set_leverage(signal: str):
    try:
        positionType = 1 if signal == "buy" else 2  # 1=long, 2=short

        params = {
            "openType": 1,       # 1=isolated
            "positionType": positionType
        }

        result = await exchange.set_leverage(LEVERAGE, SYMBOL, params)
        logger.info(f"Плечо установлено: {result}")

    except Exception as e:
        logger.error(f"Ошибка установки плеча: {e}")


# ================================
# 📌 Открытие позиции
# ================================
async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position

    try:
        side = "buy" if signal == "buy" else "sell"
        positionSide = "LONG" if side == "buy" else "SHORT"
        positionType = 1 if side == "buy" else 2

        balance = await check_balance()
        if balance <= 5:
            raise ValueError("Недостаточно средств")

        usd = amount_usd or balance * RISK_PERCENT / 100
        if usd < 5:
            usd = 5

        qty = await calculate_qty(usd)
        if qty <= 0:
            raise ValueError("qty невалидный")

        await set_leverage(signal)

        logger.info(f"Отправляю маркет ордер {side}, qty={qty}")

        # === РАБОТАЮЩИЙ MARKET ORDER ДЛЯ MEXC FUTURES ===
        order = await exchange.create_order(
            SYMBOL,
            "market",
            side,
            qty,
            params={
                "positionSide": positionSide,
                "openType": 1,
                "positionType": positionType,
                "force": "market",
                "reduceOnly": False
            }
        )

        entry = await get_current_price()
        active_position = True

        last_trade_info = {
            "signal": signal,
            "side": side,
            "qty": qty,
            "entry": entry,
            "positionSide": positionSide,
            "params_used": {
                "openType": 1,
                "positionType": positionType,
                "force": "market"
            },
            "order": order
        }

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ Открыта позиция {side.upper()}\nQty: {qty}\nВход: {entry}"
        )

        logger.info("Позиция успешно открыта")

    except Exception as e:
        logger.error(f"Ошибка открытия позиции: {e}")
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"❌ Ошибка открытия позиции: {e}"
        )
        active_position = False


# ================================
# 🚀 Startup
# ================================
@app.on_event("startup")
async def startup_event():
    try:
        balance = await check_balance()
        price = await get_current_price()

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 MEXC Bot запущен!\nБаланс: {balance}\nЦена: {price}"
        )
    except Exception as e:
        logger.error(f"Ошибка startup: {e}")


# ================================
# 📩 WEBHOOK
# ================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")

        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be buy or sell"}

        asyncio.create_task(open_position(signal))

        return {"status": "ok", "message": f"{signal} received"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}


# ================================
# 🌐 WEB UI
# ================================
@app.get("/")
async def home():
    balance = await check_balance()
    price = await get_current_price()

    status = "АКТИВНА" if active_position else "НЕТ"

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ background: #111; color: #fff; padding:20px; font-family: Arial; }}
            .card {{ background:#222; padding:15px; margin:10px 0; border-radius:10px; }}
        </style>
    </head>
    <body>
        <h1>MEXC Futures Bot</h1>

        <div class="card">
            <b>Баланс:</b> {balance}
        </div>

        <div class="card">
            <b>Цена:</b> {price}<br>
            <b>Позиция:</b> {status}
        </div>

        <div class="card">
            <b>Последняя сделка:</b><br>
            <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет"}</pre>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html)


# ================================
# Запуск локально
# ================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
