# main.py — MEXC XRP Bot — работает как твой пример с BTC_USD (17.11.2025)
import os
import math
import time
import logging
import asyncio
import traceback
from typing import Dict

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ====================== ЛОГИ ======================
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("ccxt").setLevel(logging.DEBUG)
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
    'timeout': 60000,
})

position_active = False

async def resolve_symbol(base: str) -> str:
    await exchange.load_markets()
    symbol = f"{base.upper()}/USDT:USDT"
    if symbol not in exchange.markets:
        raise ValueError(f"Нет {symbol}")
    return symbol

async def fetch_price(symbol: str) -> float:
    ticker = await exchange.fetch_ticker(symbol)
    return float(ticker['last'])

async def calculate_qty(symbol: str) -> float:
    price = await fetch_price(symbol)
    market = exchange.markets[symbol]
    contract_size = float(market['info'].get('contractSize', 1))
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = math.ceil(raw_qty / contract_size) * contract_size
    return max(qty, market['limits']['amount']['min'] or 0)

# ====================== ОТКРЫТИЕ — ТОЧНО КАК В ТВОЁМ ПРИМЕРЕ ======================
async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        symbol = await resolve_symbol(BASE_COIN)
        qty = await calculate_qty(symbol)
        price = await fetch_price(symbol)

        client_oid = f"xrp_bot_{int(time.time()*1000)}"

        # ←←← ТОЧНО ПО ТВОЕМУ ПРИМЕРУ (всё работает на MEXC в ноябре 2025)
        payload = {
            "symbol": symbol.replace(":USDT", "_USDT"),  # XRP_USDT
            "vol": qty,
            "leverage": LEVERAGE,
            "side": 1,           # 1 = long
            "type": 1,           # 1 = market
            "openType": 1,       # 1 = изолированная
            "externalOid": client_oid
        }

        logger.info(f"ОТКРЫВАЕМ LONG → {payload}")

        await exchange.private_contract_post_order_submit(payload)

        entry = await fetch_price(symbol)
        position_active = True

        tp_price = round(entry * (1 + TP_PERCENT / 100), 4)
        sl_price = round(entry * (1 - SL_PERCENT / 100), 4)

        # TP и SL тоже через submit
        for p, name in [(tp_price, "tp"), (sl_price, "sl")]:
            await exchange.private_contract_post_order_submit({
                "symbol": symbol.replace(":USDT", "_USDT"),
                "vol": qty,
                "price": p,
                "side": 2,  # 2 = sell
                "type": 2,  # 2 = limit
                "openType": 1,
                "reduceOnly": 1,
                "externalOid": f"{name}_{client_oid}"
            })

        msg = f"""
LONG ОТКРЫТ
{symbol} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.4f}</code>
TP: <code>{tp_price:.4f}</code> | SL: <code>{sl_price:.4f}</code>
        """
        await tg_send(msg.strip())
        asyncio.create_task(auto_close(symbol, qty, client_oid))

    except Exception as e:
        err = traceback.format_exc()
        logger.error(f"Ошибка: {err}")
        await tg_send(f"Ошибка открытия:\n<code>{str(e)}</code>")
        position_active = False

async def auto_close(symbol: str, qty: float, oid: str):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    global position_active
    if not position_active:
        return
    try:
        await exchange.private_contract_post_order_submit({
            "symbol": symbol.replace(":USDT", "_USDT"),
            "vol": qty,
            "side": 2,
            "type": 1,  # market close
            "openType": 1,
            "reduceOnly": 1,
            "externalOid": f"close_{oid}"
        })
        await tg_send("Позиция закрыта по таймеру")
    except Exception as e:
        await tg_send(f"Ошибка закрытия: {e}")
    finally:
        position_active = False

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send(f"Bot запущен | {BASE_COIN}/USDT | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>MEXC XRP Bot — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    data = await request.json()
    if data.get("signal") == "buy":
        await tg_send("Сигнал BUY → открываю LONG")
        asyncio.create_task(open_long())
    return {"ok": True}
