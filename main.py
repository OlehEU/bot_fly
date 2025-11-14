import ccxt.async_support as ccxt
import asyncio
import logging
import time
from aiogram import Bot
from fastapi import FastAPI
from pydantic import BaseModel
import httpx

# ============ CONFIG ============
API_KEY = ""
API_SECRET = ""
BOT_TOKEN = ""
TELEGRAM_CHAT_ID = "905530136"

SYMBOL = "XRP/USDT"               # ❗ Для MEXC Futures именно так
LEVERAGE = 10
RISK_PERCENT = 100
POSITION_COOLDOWN = 0
FAKE_MODE = False
LOG_FILE = "bot.log"

# ============ LOGGING ============
logger = logging.getLogger("mexc-bot")
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(LOG_FILE)
console_handler = logging.StreamHandler()

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# ============ INIT ============
bot = Bot(token=BOT_TOKEN)
app = FastAPI()

exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap"      # ❗ ОБЯЗАТЕЛЬНО ФЬЮЧЕРСЫ
    }
})


# ============ UTILS ============
async def get_current_price():
    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = ticker["last"]
        logger.info(f"Текущая цена {SYMBOL}: {price}")
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return 0


async def check_balance():
    try:
        balance = await exchange.fetch_balance()
        usdt = balance["USDT"]["free"]
        logger.info(f"Баланс USDT: {usdt}")
        return usdt
    except Exception as e:
        logger.error(f"Ошибка получения баланса: {e}")
        return 0


async def calculate_qty(amount_usd):
    price = await get_current_price()
    if price == 0:
        return 0

    qty = round(amount_usd / price, 3)
    return max(qty, 1)


# ============ LEVERAGE ============
async def set_leverage(signal: str):
    positionType = 1 if signal == "buy" else 2  # long = 1, short = 2

    params = {
        "openType": 1,        # isolated
        "positionType": positionType,
    }

    try:
        result = await exchange.set_leverage(LEVERAGE, SYMBOL, params)
        logger.info(f"Плечо установлено: {result}")
    except Exception as e:
        logger.error(f"Ошибка установки плеча: {e}")


# ============ OPEN POSITION ============
async def open_position(signal: str, amount_usd: float | None = None):
    side = "buy" if signal == "buy" else "sell"
    positionSide = "LONG" if side == "buy" else "SHORT"
    positionType = 1 if side == "buy" else 2

    try:
        balance = await check_balance()
        usd = amount_usd or balance * (RISK_PERCENT / 100)
        if usd < 5:
            usd = 5

        qty = await calculate_qty(usd)

        await set_leverage(signal)

        logger.info(f"Отправляю маркет ордер {side}, qty={qty}")

        order = await exchange.create_order(
            SYMBOL,
            "market",
            side,
            qty,
            params={
                "leverage": LEVERAGE,        # ❗ ОБЯЗАТЕЛЬНО
                "positionSide": positionSide,
                "openType": 1,               # isolated
                "positionType": positionType,
                "force": "market",
                "reduceOnly": False
            }
        )

        logger.info(f"Ордер открыт: {order}")

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"Открыта позиция {side.upper()}\nQty: {qty}"
        )

    except Exception as e:
        logger.error(f"❌ Ошибка открытия позиции: {e}")
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"❌ Ошибка открытия позиции: {e}"
        )


# ============ CLOSE POSITION ============
async def close_position():
    positions = await exchange.fetch_positions([SYMBOL])
    for p in positions:
        if float(p["contracts"]) > 0:
            side = "sell" if p["side"] == "long" else "buy"
            qty = float(p["contracts"])

            logger.info(f"Закрываю позицию {p['side']} qty={qty}")

            try:
                await exchange.create_order(
                    SYMBOL,
                    "market",
                    side,
                    qty,
                    params={
                        "reduceOnly": True,
                        "force": "market"
                    }
                )
                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"Позиция {p['side']} закрыта."
                )
            except Exception as e:
                logger.error(f"Ошибка закрытия позиции: {e}")


# ============ WEBHOOK ============
class WebhookSignal(BaseModel):
    signal: str
    amount: float | None = None


@app.post("/webhook")
async def webhook(data: WebhookSignal):
    if data.signal.lower() == "buy":
        await open_position("buy", data.amount)
    elif data.signal.lower() == "sell":
        await open_position("sell", data.amount)
    elif data.signal.lower() == "close":
        await close_position()

    return {"status": "ok"}


# ============ PRICE POLLING ============
async def price_loop():
    while True:
        await get_current_price()
        await asyncio.sleep(10)


# ============ STARTUP ============
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(price_loop())
