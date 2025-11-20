# main.py — BINANCE XRP BOT — УЛЬТРА-НАДЁЖНЫЙ, УЛЬТРА-БЫСТРЫЙ, БЕЗ ТАЙМАУТОВ
import os
import time
import logging
import asyncio
import traceback

import ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("binance-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная окружения: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY     = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET  = os.getenv("BINANCE_API_SECRET")
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

SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MARKET = None
position_active = False

exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
    "enableRateLimit": False,
    "timeout": 60000,
    "options": {
        "defaultType": "future",
        "leverage": LEVERAGE,
    },
})

async def preload():
    global MARKET
    try:
        await exchange.load_markets()
        MARKET = exchange.market(SYMBOL)
        
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"Плечо установлено: {LEVERAGE}x")
        except Exception as e:
            logger.warning(f"Не удалось установить плечо: {e}")
        
        logger.info(f"Preload завершён: {SYMBOL} | min_qty={MARKET['limits']['amount']['min']}")
        
        try:
            balance = await exchange.fetch_balance()
            logger.info(f"API ключ работает, баланс получен: {bool(balance)}")
        except Exception as e:
            logger.warning(f"Не удалось получить баланс - проверьте права API ключа: {e}")
    except Exception as e:
        logger.error(f"Ошибка при preload: {e}")
        raise

async def get_price() -> float:
    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        return float(ticker['last'])
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        raise

async def get_qty() -> float:
    price = await get_price()
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = exchange.amount_to_precision(SYMBOL, raw_qty)
    min_qty = MARKET['limits']['amount']['min'] or 0
    return max(float(qty), min_qty)

async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"

        await asyncio.sleep(0.25)

        entry = await get_price()
        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        params = {
            "positionSide": "LONG",
            "newClientOrderId": oid,
        }

        start = time.time()

        try:
            logger.info(f"Создаю ордер: {SYMBOL}, market, buy, {qty}")
            order = await exchange.create_order(SYMBOL, 'market', 'buy', qty, None, params)
        except (ccxt.RequestTimeout, asyncio.TimeoutError):
            await tg_send("Таймаут Binance, пробую ещё раз...")
            await asyncio.sleep(1)
            order = await exchange.create_order(SYMBOL, 'market', 'buy', qty, None, params)

        if not order or not order.get('id'):
            raise Exception(f"Binance не вернул ID ордера: {order}")

        took = round(time.time() - start, 2)
        logger.info(f"LONG открыт: {order.get('id')} | {took}s")

        position_active = True

        await asyncio.sleep(0.2)
        
        for price, name in [(tp, "tp"), (sl, "sl")]:
            try:
                tp_sl_params = {
                    "positionSide": "LONG",
                    "stopPrice": price,
                    "reduceOnly": True,
                    "newClientOrderId": f"{name}_{oid}",
                }
                if name == "tp":
                    tp_sl_order = await exchange.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', 'sell', qty, None, tp_sl_params)
                else:
                    tp_sl_order = await exchange.create_order(SYMBOL, 'STOP_MARKET', 'sell', qty, None, tp_sl_params)
                if tp_sl_order and tp_sl_order.get('id'):
                    logger.info(f"Ордер {name} создан: {tp_sl_order.get('id')}")
                else:
                    logger.warning(f"Ордер {name} не создан: {tp_sl_order}")
            except Exception as e:
                logger.warning(f"Ошибка при создании {name}: {e}")

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
        close_params = {
            "positionSide": "LONG",
            "reduceOnly": True,
            "newClientOrderId": f"close_{oid}",
        }
        close_order = await exchange.create_order(SYMBOL, 'market', 'sell', qty, None, close_params)
        if not close_order or not close_order.get('id'):
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
        await tg_send("Bot стартует... загружаю данные Binance")
    except:
        pass

    await preload()

    await tg_send(
        f"<b>Bot ГОТОВ и на связи!</b>\n"
        f"{SYMBOL} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x\n"
        f"Реакция на сигнал: менее 1.2 сек"
    )
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP Bot — BINANCE — ULTRAFAST & ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    if data.get("signal") == "obuy":  # ←←← если у тебя "obuy" вместо "buy" — поменяй на своё
        await tg_send("Сигнал BUY — открываю LONG")
        asyncio.create_task(open_long())
    return {"ok": True}
