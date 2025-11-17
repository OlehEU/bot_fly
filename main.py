# main.py — ФИНАЛЬНАЯ РАБОЧАЯ ВЕРСИЯ (17.11.2025)# main.py — ФИНАЛЬНАЯ ВЕРСИЯ (XRP LONG 10$ × 10x — работает на MEXC!)
import os
import logging
import asyncio
import math
from typing import Dict
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))
AUTO_CLOSE_MINUTES = 10
BASE_COIN = "XRP"

# ====================== ЛОГИ ======================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# ====================== TELEGRAM ======================
bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== MEXC ======================
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 20000,
})

_cached_markets: Dict[str, str] = {}

async def resolve_symbol(base: str) -> str:
    global _cached_markets
    if not _cached_markets:
        await exchange.load_markets()
        _cached_markets = {s.split("/")[0]: s for s in exchange.markets.keys() if s.endswith(":USDT")}
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise ValueError(f"Символ {base} не найден")
    return symbol

async def fetch_price(symbol: str) -> float:
    ticker = await exchange.fetch_ticker(symbol)
    return float(ticker['last'])

async def calculate_qty(symbol: str) -> float:
    price = await fetch_price(symbol)
    await exchange.load_markets()
    market = exchange.markets[symbol]
    info = market.get('info', {})
    contract_size = float(info.get('contractSize', 1))
    min_qty = float(info.get('minQuantity', 1))
    
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = math.ceil(raw_qty / contract_size) * contract_size
    return max(qty, min_qty)

# Глобальное состояние
position_active = False

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        symbol = await resolve_symbol(BASE_COIN)
        qty = await calculate_qty(symbol)

        # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: правильный set_leverage с параметрами
        await exchange.set_leverage(LEVERAGE, symbol, params={
            "openType": 1,      # 1 = изолированная
            "positionType": 1   # 1 = long
        })

        # Баланс
        bal = await exchange.fetch_balance()
        if float(bal['total'].get('USDT', 0)) < 3:
            await tg_send(f"Недостаточно USDT: {bal['total'].get('USDT', 0)}")
            return

        # Рыночный ордер LONG
        order = await exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=qty,
            params={
                "openType": 1,
                "positionType": 1
            }
        )

        entry = await fetch_price(symbol)
        tp_price = round(entry * (1 + TP_PERCENT/100), 4)
        sl_price = round(entry * (1 - SL_PERCENT/100), 4)

        # TP и SL
        await exchange.create_order(symbol, 'limit', 'sell', qty, tp_price, {'reduceOnly': True})
        await exchange.create_order(symbol, 'limit', 'sell', qty, sl_price, {'reduceOnly': True})

        position_active = True

        msg = f"""
<b>LONG ОТКРЫТ</b>
<code>{symbol}</code> | ${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.4f}</code>
TP: <code>{tp_price:.4f}</code> (+{TP_PERCENT}%)
SL: <code>{sl_price:.4f}</code> (-{SL_PERCENT}%)
Автозакрытие через {AUTO_CLOSE_MINUTES} мин
        """
        await tg_send(msg.strip())
        asyncio.create_task(auto_close(symbol, qty))

    except Exception as e:
        await tg_send(f"Ошибка открытия LONG:\n<code>{str(e)}</code>")
        position_active = False

async def auto_close(symbol: str, qty: float):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    global position_active
    if not position_active:
        return
    try:
        await exchange.create_order(symbol, 'market', 'sell', qty, params={'reduceOnly': True})
        await tg_send("Автозакрытие: позиция закрыта по рынку")
    except Exception as e:
        await tg_send(f"Ошибка автозакрытия: {e}")
    finally:
        position_active = False

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send(f"Bot запущен | {BASE_COIN} LONG | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h2>MEXC XRP Bot — ONLINE</h2>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    data = await request.json()
    if data.get("signal") == "buy":
        await tg_send("BUY сигнал получен")
        asyncio.create_task(open_long())
    return {"ok": True}

# Только для локального запуска
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
import os
import logging
import asyncio
import math
from typing import Dict
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))
AUTO_CLOSE_MINUTES = 10
BASE_COIN = "XRP"

# ====================== ЛОГИ ======================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# ====================== TELEGRAM ======================
bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        logger.info("Telegram sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ====================== MEXC ======================
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 15000,
})

_cached_markets: Dict[str, str] = {}

async def resolve_symbol(base: str) -> str:
    global _cached_markets
    if not _cached_markets:
        await exchange.load_markets()
        _cached_markets = {s.split("/")[0]: s for s in exchange.markets.keys() if s.endswith(":USDT")}
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise ValueError(f"Символ {base} не найден")
    return symbol

async def fetch_price(symbol: str) -> float:
    ticker = await exchange.fetch_ticker(symbol)
    return float(ticker['last'])

async def calculate_qty(symbol: str) -> float:
    price = await fetch_price(symbol)
    await exchange.load_markets()
    market = exchange.markets[symbol]
    info = market.get('info', {})
    contract_size = float(info.get('contractSize', 1))
    min_qty = max(float(info.get('minQuantity', 1)), 1)
    
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = math.ceil(raw_qty / contract_size) * contract_size
    return max(qty, min_qty)

# ====================== ГЛОБАЛЬНОЕ СОСТОЯНИЕ ======================
position_active = False  # Глобальная переменная (НЕ внутри функции!)

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        symbol = await resolve_symbol(BASE_COIN)
        qty = await calculate_qty(symbol)

        # Плечо
        await exchange.set_leverage(LEVERAGE, symbol)

        # Баланс
        bal = await exchange.fetch_balance()
        if float(bal['total'].get('USDT', 0)) < 2:
            await tg_send("Недостаточно USDT на балансе")
            return

        # Открытие LONG
        params = {'openType': 1, 'positionType': 1}
        await exchange.create_order(symbol, 'market', 'buy', qty, params=params)
        entry = await fetch_price(symbol)

        tp_price = round(entry * (1 + TP_PERCENT/100), 4)
        sl_price = round(entry * (1 - SL_PERCENT/100), 4)

        # TP/SL
        await exchange.create_order(symbol, 'limit', 'sell', qty, tp_price, {'reduceOnly': True})
        await exchange.create_order(symbol, 'limit', 'sell', qty, sl_price, {'reduceOnly': True})

        position_active = True

        msg = f"""
LONG ОТКРЫТ
{symbol} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.4f}</code>
TP (+{TP_PERCENT}%): <code>{tp_price:.4f}</code>
SL (-{SL_PERCENT}%): <code>{sl_price:.4f}</code>
Автозакрытие через {AUTO_CLOSE_MINUTES} мин
        """
        await tg_send(msg.strip())
        asyncio.create_task(auto_close(symbol, qty))

    except Exception as e:
        await tg_send(f"Ошибка открытия LONG:\n<code>{str(e)}</code>")
        position_active = False

async def auto_close(symbol: str, qty: float):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    global position_active
    if not position_active:
        return
    try:
        await exchange.create_order(symbol, 'market', 'sell', qty, params={'reduceOnly': True})
        await tg_send("Автозакрытие: позиция закрыта по рынку")
    except Exception as e:
        await tg_send(f"Ошибка автозакрытия: {e}")
    finally:
        position_active = False

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send(f"Bot запущен | {BASE_COIN} LONG | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<pre>Bot работает\nXRP LONG 10$ × 10x\nГотов к сигналам</pre>")

@app.get("/status")
async def status():
    return {"coin": BASE_COIN, "position": "Открыта" if position_active else "Нет"}

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    data = await request.json()
    if data.get("signal") == "buy":
        await tg_send("BUY сигнал получен")
        asyncio.create_task(open_long())
    return {"ok": True}

