# main.py — УЛЬТРА-БЫСТРЫЙ XRP BOT НА MEXC (17.11.2025)
import os
import math
import time
import logging
import asyncio
import traceback

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной окружения: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY     = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET  = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD   = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE           = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT         = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT         = float(os.getenv("SL_PERCENT", "1.0"))
AUTO_CLOSE_MINUTES = int(os.getenv("AUTO_CLOSE_MINUTES", "10"))
BASE_COIN          = "XRP"

bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 30000,
})

# Глобальные переменные — кэшируем при старте
SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MARKET = None
CONTRACT_SIZE = 1.0

async def preload():
    global MARKET, CONTRACT_SIZE
    await exchange.load_markets()
    if SYMBOL not in exchange.markets:
        raise ValueError(f"Символ {SYMBOL} не найден на MEXC!")
    MARKET = exchange.markets[SYMBOL]
    info_size = MARKET['info'].get('contractSize')
    CONTRACT_SIZE = float(info_size) if info_size else 1.0
    logger.info(f"Preload готов: {SYMBOL} | contract_size={CONTRACT_SIZE}")

async def get_price() -> float:
    ticker = await exchange.fetch_ticker(SYMBOL)
    return float(ticker['last'])

async def get_qty() -> float:
    price = await get_price()
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = math.ceil(raw_qty / CONTRACT_SIZE) * CONTRACT_SIZE
    min_qty = MARKET['limits']['amount']['min'] or 0
    return max(qty, min_qty)

position_active = False

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"

        params = {
            "clientOrderId": oid,
            "leverage": LEVERAGE,
            "openType": 1,       # изолированная
            "positionType": 1,
            "volSide": 1,        # long
            "orderType": 1,      # market
        }

        start_time = time.time()
        await exchange.create_order(SYMBOL, 'market', 'open_long', qty, None, params)
        entry = await get_price()
        took = round(time.time() - start_time, 2)

        position_active = True

        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        for price, name in [(tp, "tp"), (sl, "sl")]:
            await exchange.create_order(
                SYMBOL, 'limit', 'sell', qty, price,
                {"reduceOnly": True, "clientOrderId": f"{name}_{oid}"}
            )

        await tg_send(f"""
LONG ОТКРЫТ за {took}с
${FIXED_AMOUNT_USD} × {LEVERAGE}x | {SYMBOL}
Entry: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code> (+{TP_PERCENT}%)
SL: <code>{sl:.4f}</code> (-{SL_PERCENT}%)
        """.strip())

        asyncio.create_task(auto_close(qty, oid))

    except Exception as e:
        err = traceback.format_exc()
        logger.error(f"Ошибка открытия: {err}")
        await tg_send(f"Ошибка LONG:\n<code>{str(e)}</code>")
        position_active = False

async def auto_close(qty: float, oid: str):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    if not position_active: return
    try:
        await exchange.create_order(SYMBOL, 'market', 'close_long', qty, None, {
            "reduceOnly": True,
            "clientOrderId": f"close_{oid}"
        })
        await tg_send("Автозакрытие — позиция закрыта")
    except Exception as e:
        await tg_send(f"Ошибка закрытия: {e}")
    finally:
        global position_active
        position_active = False

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await preload()
    await tg_send(f"Bot ULTRAFAST запущен!\n{SYMBOL} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x\nРеакция: <1.2 сек")
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP Bot — ULTRAFAST & ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    data = await request.json()
    if data.get("signal") == "buy":
        await tg_send("Сигнал BUY — открываю LONG")
        asyncio.create_task(open_long())
    return {"ok": True}
