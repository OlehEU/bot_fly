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
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = "XRP/USDT:USDT"
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False


# === Вспомогательные функции ===
async def get_current_price() -> float:
    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.info(f"Текущая цена {SYMBOL}: {price:.6f}")
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return 0.0


async def check_balance() -> float:
    try:
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        return float(usdt)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        return 0.0


async def calculate_qty(usd_amount: float) -> float:
    try:
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Цена не получена")

        qty = usd_amount / price
        qty = round(qty, 1)

        if qty < 1:
            qty = 1.0

        return qty
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0


async def set_leverage():
    """Установить плечо"""
    try:
        await exchange.set_leverage(LEVERAGE, SYMBOL)
        logger.info(f"Плечо установлено: {LEVERAGE}x")
    except Exception as e:
        logger.error(f"Ошибка установки плеча: {e}")


async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position

    try:
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()}")

        balance = await check_balance()
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        if usd < 5:
            usd = 5

        qty = await calculate_qty(usd)
        if qty <= 0:
            raise ValueError("Ошибка в qty")

        side = "buy" if signal.lower() == "buy" else "sell"
        positionSide = "LONG" if side == "buy" else "SHORT"

        await set_leverage()

        # === ВАЖНО: правильный маркет-ордер MEXC Futures ===
        order = await exchange.create_order(
            symbol=SYMBOL,
            type="market",
            side=side,
            amount=qty,
            params={
                "positionSide": positionSide,
                "force": "market",
                "leverage": LEVERAGE
            }
        )

        entry = await get_current_price()

        active_position = True
        last_trade_info = {
            "signal": signal,
            "side": side,
            "positionSide": positionSide,
            "qty": qty,
            "entry": entry,
            "balance": balance,
            "order": order,
            "timestamp": time.time()
        }

        msg = (
            f"✅ {side.upper()} ОТКРЫТА\n"
            f"Символ: {SYMBOL}\n"
            f"Количество: {qty}\n"
            f"Вход: ${entry:.4f}\n"
            f"Плечо: {LEVERAGE}x\n"
            f"Баланс: {balance:.2f} USDT"
        )
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)

        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

    except Exception as e:
        error_text = f"❌ Ошибка открытия позиции: {e}"
        logger.error(error_text)

        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_text)
        except:
            pass

        active_position = False


# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    try:
        balance = await check_balance()
        price = await get_current_price()
        await set_leverage()

        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

💰 Баланс: {balance:.2f} USDT
📊 Символ: {SYMBOL}
📈 Цена: ${price:.4f}
⚙ Плечо: {LEVERAGE}x

Готов к работе!
"""
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)

    except Exception as e:
        logger.error(f"Ошибка старта: {e}")


@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")

        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}

        asyncio.create_task(open_position(signal))

        return {"status": "ok", "message": f"{signal} signal received"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/")
async def home():
    global last_trade_info, active_position

    balance = await check_balance()
    price = await get_current_price()
    status = "АКТИВНА" if active_position else "НЕТ"

    html = f"""
    <html>
        <head>
            <title>MEXC Futures Bot</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial; background: #1e1e1e; color: white; padding: 20px; }}
                .card {{ background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px; }}
                .success {{ color: #00b894; }}
                .warning {{ color: #fdcb6e; }}
            </style>
        </head>
        <body>
            <h1 class="success">🤖 MEXC Futures Bot</h1>

            <div class="card">
                <h3>💰 Баланс</h3>
                <p><b>USDT:</b> {balance:.2f}</p>
            </div>

            <div class="card">
                <h3>📊 Статус</h3>
                <p><b>Символ:</b> {SYMBOL}</p>
                <p><b>Цена:</b> ${price:.4f}</p>
                <p><b>Позиция:</b> <span class="{'success' if active_position else 'warning'}">{status}</span></p>
            </div>

            <div class="card">
                <h3>📈 Последняя сделка</h3>
                <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
