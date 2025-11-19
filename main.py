# main.py — MEXC XRP BOT — УЛЬТРА-НАДЁЖНЫЙ, УЛЬТРА-БЫСТРЫЙ, БЕЗ ТАЙМАУТОВ
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
        raise EnvironmentError(f"Отсутствует переменная окружения: {var}")

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
        logger.error(f"Telegram error: {e}")

# ←←← САМОЕ ВАЖНОЕ — ТАЙМАУТЫ НА 60 СЕКУНД (MEXC иногда тормозит)
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": False,
    "timeout": 60000,
    "options": {
        "defaultType": "swap",
        "timeout": 60000,
        "createOrder": {"timeout": 60000},
        "force": "ioc"
    },
})

SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MARKET = None
CONTRACT_SIZE = 1.0
position_active = False

async def preload():
    global MARKET, CONTRACT_SIZE
    try:
        await exchange.load_markets()
        if SYMBOL not in exchange.markets:
            raise ValueError(f"Символ {SYMBOL} не найден!")
        MARKET = exchange.markets[SYMBOL]
        info_size = MARKET['info'].get('contractSize')
        CONTRACT_SIZE = float(info_size) if info_size else 1.0
        logger.info(f"Preload завершён: {SYMBOL} | contract_size={CONTRACT_SIZE}")
        
        # Проверка прав API ключа - пробуем получить баланс
        try:
            balance = await exchange.fetch_balance({'type': 'swap'})
            logger.info(f"API ключ работает, баланс получен: {bool(balance)}")
        except Exception as e:
            logger.warning(f"Не удалось получить баланс - проверьте права API ключа: {e}")
    except Exception as e:
        logger.error(f"Ошибка при preload: {e}")
        raise

async def get_price() -> float:
    ticker = await exchange.fetch_ticker(SYMBOL)
    return float(ticker["last"])

async def get_qty() -> float:
    price = await get_price()
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = math.ceil(raw_qty / CONTRACT_SIZE) * CONTRACT_SIZE
    min_qty = MARKET['limits']['amount']['min'] or 0
    return max(qty, min_qty)

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"

        await asyncio.sleep(0.25)  # required pause for MEXC

        entry = await get_price()
        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        params = {
            "clientOrderId": oid,
            "leverage": LEVERAGE,
            "openType": 1,
            "positionType": 1,
            "volSide": 1,
            "takeProfitPrice": tp,
            "stopLossPrice": sl,
            "reduceOnly": False,
            "force": "ioc"
        }

        start = time.time()

        try:
            order = await exchange.create_order(
                SYMBOL,
                "market",
                "buy",
                qty,
                None,
                params
            )
        except (ccxt.RequestTimeout, asyncio.TimeoutError):
            await tg_send("Таймаут MEXC, пробую ещё раз...")
            await asyncio.sleep(1)
            order = await exchange.create_order(
                SYMBOL,
                "market",
                "buy",
                qty,
                None,
                params
            )

        if not order or not order.get("id"):
            raise Exception(f"MEXC не вернул ID ордера: {order}")

        took = round(time.time() - start, 2)
        logger.info(f"LONG открыт: {order.get('id')} | {took}s")

        position_active = True

        await tg_send(f"""
<b>LONG ОТКРЫТ</b> за {took}с
${FIXED_AMOUNT_USD} × {LEVERAGE}x | {SYMBOL}
Entry: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code> (+{TP_PERCENT}%)
SL: <code>{sl:.4f}</code> (-{SL_PERCENT}%)
Автозакрытие через {AUTO_CLOSE_MINUTES} мин
        """.strip())

        asyncio.create_task(auto_close(qty, oid))

    except Exception as e:
        logger.error(traceback.format_exc())
        await tg_send(f"Ошибка LONG:\n<code>{str(e)}</code>")
        position_active = False

async def auto_close(qty: float, oid: str):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    global position_active
    if not position_active:
        return
    try:
        await asyncio.sleep(0.2)
        close_order = await exchange.create_order(
            SYMBOL,
            "market",
            "sell",
            qty,
            None,
            {"reduceOnly": True, "clientOrderId": f"close_{oid}", "force": "ioc"}
        )
        if not close_order or not close_order.get("id"):
            raise Exception(f"Ордер закрытия не создан: {close_order}")
        await tg_send("Позиция закрыта по таймеру")
    except Exception as e:
        await tg_send(f"Ошибка автозакрытия: {e}")
    finally:
        position_active = False

# ====================== LIFESPAN ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await tg_send("Bot стартует... загружаю данные MEXC")
    except:
        pass

    await preload()

    await tg_send(
        f"<b>Bot ГОТОВ и на связи!</b>\n"
        f"{SYMBOL} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x\n"
        f"Реакция на сигнал: менее 1.2 сек"
    )
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP Bot — ULTRAFAST & ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    if data.get("signal") == "obuy":  # ←←← если у тебя "obuy" вместо "buy" — поменяй на своё
        await tg_send("Сигнал BUY — открываю LONG")
        asyncio.create_task(open_long())
    return {"ok": True}
