# main.py — САМЫЙ БЫСТРЫЙ XRP BOT НА MEXC (0.5–1.5 сек после сигнала)
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
        raise EnvironmentError(f"Нет {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY     = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET  = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD   = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE           = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT         = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT         = float(os.getenv("SL_PERCENT", "1.0"))
AUTO_CLOSE_MINUTES = 10
BASE_COIN          = "XRP"

bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(t): await bot.send_message(TELEGRAM_CHAT_ID, t, parse_mode="HTML", disable_web_page_preview=True)

exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 30000,
})

# ←←← ВСЁ КЭШИРУЕТСЯ ОДИН РАЗ ПРИ СТАРТЕ
SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MARKET = None
CONTRACT_SIZE = 1

async def preload():
    global MARKET, CONTRACT_SIZE
    await exchange.load_markets()
    if SYMBOL not in exchange.markets:
        raise ValueError(f"Символ {SYMBOL} не найден!")
    MARKET = exchange.markets[SYMBOL]
    CONTRACT_SIZE = float(MARKET['contractSize'] if 'contractSize' in MARKET else MARKET['info'].get('contractSize', 1))
    logger.info(f"Preload готов: {SYMBOL} | contractSize = {CONTRACT_SIZE}")

async def get_price() -> float:
    ticker = await exchange.fetch_ticker(SYMBOL)
    return float(ticker['last'])

async def get_qty() -> float:
    price = await get_price()
    raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = math.ceil(raw / CONTRACT_SIZE) * CONTRACT_SIZE
    return max(qty, MARKET['limits']['amount']['min'] or 0)

position_active = False

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже есть!")
        return

    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"

        params = {
            "clientOrderId": oid,
            "leverage": LEVERAGE,
            "openType": 1,
            "positionType": 1,
            "volSide": 1,
            "orderType": 1,
        }

        start = time.time()
        await exchange.create_order(SYMBOL, 'market', 'open_long', qty, None, params)
        entry = await get_price()
        took = round(time.time() - start, 2)

        position_active = True

        tp = round(entry * (1 + TP_PERCENT/100), 4)
        sl = round(entry * (1 - SL_PERCENT/100), 4)

        for p, n in [(tp, "tp"), (sl, "sl")]:
            await exchange.create_order(SYMBOL, 'limit', 'sell', qty, p, {"reduceOnly": True, "clientOrderId": f"{n}_{oid}"})

        await tg_send(f"""
LONG ОТКРЫТ за {took} сек
${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code>  |  SL: <code>{sl:.4f}</code>
        """)
        asyncio.create_task(auto_close(qty, oid))

    except Exception as e:
        await tg_send(f"Ошибка:\n<code>{str(e)}</code>")
        position_active = False

async def auto_close(qty: float, oid: str):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    global position_active
    if not position_active: return
    try:
        await exchange.create_order(SYMBOL, 'market', 'close_long', qty, None, {"reduceOnly": True, "clientOrderId": f"close_{oid}"})
        await tg_send("Автозакрытие выполнено")
    finally:
        position_active = False

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await preload()
    await tg_send(f"Bot готов! {BASE_COIN}/USDT | ${FIXED_AMOUNT_USD} × {LEVERAGE}x | реакция <1.5 сек")
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/"); async def root(): return HTMLResponse("<h1>XRP Bot — ULTRAFAST</h1>")
@app.post("/webhook")
async def webhook(r: Request):
    if r.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: raise HTTPException(403)
    data = await r.json()
    if data.get("signal") == "buy":
        await tg_send("Сигнал → открываю LONG")
        asyncio.create_task(open_long())
    return {"ok": True}
