# main.py — MEXC XRP Futures Bot (ноябрь 2025) — ВСЁ РАБОТАЕТ + ПОЛНЫЕ ЛОГИ
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

# ====================== МАКСИМАЛЬНО ПОДРОБНЫЕ ЛОГИ ======================
logging.basicConfig(level=logging.DEBUG)  # было INFO → теперь DEBUG
logging.getLogger("ccxt").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.DEBUG)   # видно тело запросов/ответов
logging.getLogger("httpcore").setLevel(logging.DEBUG)

logger = logging.getLogger("mexc-bot")
logger.setLevel(logging.DEBUG)

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной окружения: {var}")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY       = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET    = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET")

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
        logger.info("Telegram сообщение отправлено")
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

# ====================== MEXC ======================
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': 30000,
})

_cached_markets: Dict[str, str] = {}
position_active = False

async def resolve_symbol(base: str) -> str:
    global _cached_markets
    if not _cached_markets:
        await exchange.load_markets()
        _cached_markets = {
            s.split("/")[0]: s for s in exchange.markets.keys() if s.endswith(":USDT")
        }
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise ValueError(f"Символ для {base} не найден в маркетах")
    logger.info(f"Разрешён символ: {base} → {symbol}")
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
    min_qty = market['limits']['amount']['min'] or 0
    return max(qty, min_qty)

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        symbol = await resolve_symbol(BASE_COIN)
        qty = await calculate_qty(symbol)

        bal = await exchange.fetch_balance()
        usdt = float(bal['total'].get('USDT', 0))
        if usdt < 6:
            await tg_send(f"Недостаточно USDT: {usdt:.2f}")
            return

        client_order_id = f"xrp_bot_{int(time.time()*1000)}"

        order_params = {
            "clientOrderId": client_order_id,   # ← ОБЯЗАТЕЛЬНО в 2025!
            "leverage": LEVERAGE,
            "openType": 1,                      # изолированная маржа
        }

        logger.info(f"Открываем LONG: {symbol}, qty={qty}, leverage={LEVERAGE}x")
        await exchange.create_order(
            symbol=symbol,
            type='market',
            side='open_long',
            amount=qty,
            params=order_params
        )

        entry = await fetch_price(symbol)
        position_active = True

        tp_price = round(entry * (1 + TP_PERCENT / 100), 4)
        sl_price = round(entry * (1 - SL_PERCENT / 100), 4)

        # TP и SL
        for price, name in [(tp_price, "TP"), (sl_price, "SL")]:
            await exchange.create_order(
                symbol=symbol,
                type='limit',
                side='sell',
                amount=qty,
                price=price,
                params={
                    "reduceOnly": True,
                    "clientOrderId": f"{name.lower()}_{client_order_id}"
                }
            )

        msg = f"""
LONG ОТКРЫТ
{symbol} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.4f}</code>
TP (+{TP_PERCENT}%): <code>{tp_price:.4f}</code>
SL (-{SL_PERCENT}%): <code>{sl_price:.4f}</code>
Автозакрытие через {AUTO_CLOSE_MINUTES} мин
        """
        await tg_send(msg.strip())
        asyncio.create_task(auto_close(symbol, qty, client_order_id))

    except Exception as e:
        full_error = traceback.format_exc()
        logger.error(f"ОШИБКА ОТКРЫТИЯ ПОЗИЦИИ:\n{full_error}")

        err_msg = str(e)
        if hasattr(e, 'response') and getattr(e, 'response', None):
            try:
                err_msg += f"\nОтвет биржи: {e.response.json()}"
            except:
                err_msg += f"\nRaw ответ: {e.response.text[:1000]}"

        await tg_send(f"Ошибка открытия LONG:\n<code>{err_msg}</code>")
        position_active = False


async def auto_close(symbol: str, qty: float, client_order_id: str):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)

    global position_active
    if not position_active:
        return

    try:
        await exchange.create_order(
            symbol=symbol,
            type='market',
            side='close_long',
            amount=qty,
            params={
                "reduceOnly": True,
                "clientOrderId": f"close_{client_order_id}"
            }
        )
        await tg_send("Автозакрытие выполнено — позиция закрыта")
    except Exception as e:
        await tg_send(f"Ошибка автозакрытия:\n<code>{str(e)}</code>")
    finally:
        position_active = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send(f"Bot успешно запущен | {BASE_COIN}/USDT LONG | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    yield
    await exchange.close()


app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>MEXC XRP Futures Bot — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)

    data = await request.json()
    if data.get("signal") == "buy":
        await tg_send("BUY сигнал получен — открываю LONG")
        asyncio.create_task(open_long())

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
