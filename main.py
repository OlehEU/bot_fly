# main.py — BINANCE XRP BOT — УЛЬТРА-НАДЁЖНЫЙ, УЛЬТРА-БЫСТРЫЙ, БЕЗ ТАЙМАУТОВ
import os
import math
import time
import logging
import asyncio
import traceback
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional

import httpx
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

BINANCE_BASE_URL = "https://fapi.binance.com"
SYMBOL_BINANCE = f"{BASE_COIN.upper()}USDT"
SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MARKET = None
MIN_QTY = 0.0
QTY_PRECISION = 3
position_active = False

binance_client = httpx.AsyncClient(timeout=60.0)

def _create_signature(params: Dict[str, Any], secret: str) -> str:
    normalized = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            normalized[k] = str(v).lower()
        elif isinstance(v, (int, float)):
            normalized[k] = str(v)
        else:
            normalized[k] = str(v)

    query_string = urllib.parse.urlencode(normalized)

    return hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    url = f"{BINANCE_BASE_URL}{endpoint}"
    params = params or {}
    
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        signature = _create_signature(params, BINANCE_API_SECRET)
        params["signature"] = signature
    
    headers = {
        "X-MBX-APIKEY": BINANCE_API_KEY,
    }
    
    try:
        logger.info(f"Binance {method} {url} с параметрами: {params}")
        if method == "GET":
            response = await binance_client.get(url, params=params, headers=headers, timeout=60.0)
        else:
            response = await binance_client.post(url, params=params, headers=headers, timeout=60.0)
        
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        try:
            error_data = e.response.json()
            error_msg = error_data.get("msg", str(error_data))
            logger.error(f"Binance API error: {e.response.status_code} - {error_msg}")
            raise Exception(f"Binance API error: {error_msg}")
        except:
            error_text = e.response.text if hasattr(e.response, 'text') else str(e.response.content)
            logger.error(f"Binance API error: {e.response.status_code} - {error_text}")
            raise Exception(f"Binance API error: {e.response.status_code} - {error_text}")
    except httpx.TimeoutException as e:
        logger.error(f"Binance API timeout: {e}")
        raise
    except Exception as e:
        logger.error(f"Binance request error: {e}")
        raise

async def preload():
    global MARKET, MIN_QTY, QTY_PRECISION
    try:
        try:
            exchange_info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
            symbol_info = None
            for s in exchange_info.get("symbols", []):
                if s.get("symbol") == SYMBOL_BINANCE:
                    symbol_info = s
                    break
            
            if symbol_info:
                for f in symbol_info.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        MIN_QTY = float(f.get("minQty", 0))
                    elif f.get("filterType") == "PRICE_FILTER":
                        price_precision = len(str(f.get("tickSize", "0.01")).split(".")[-1].rstrip("0"))
                QTY_PRECISION = symbol_info.get("quantityPrecision", 3)
            else:
                MIN_QTY = 0.0
                QTY_PRECISION = 3
        except:
            MIN_QTY = 0.0
            QTY_PRECISION = 3
        
        MARKET = {
            "limits": {
                "amount": {
                    "min": MIN_QTY
                }
            }
        }
        
        try:
            await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL_BINANCE, "leverage": str(LEVERAGE)})
            logger.info(f"Плечо установлено: {LEVERAGE}x")
        except Exception as e:
            logger.warning(f"Не удалось установить плечо: {e}")
        
        logger.info(f"Preload завершён: {SYMBOL} | min_qty={MIN_QTY} | qty_precision={QTY_PRECISION}")
        
        try:
            account = await binance_request("GET", "/fapi/v2/account")
            logger.info(f"API ключ работает, баланс получен: {bool(account)}")
        except Exception as e:
            logger.warning(f"Не удалось получить баланс - проверьте права API ключа: {e}")
    except Exception as e:
        logger.error(f"Ошибка при preload: {e}")
        raise

async def get_price() -> float:
    try:
        data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL_BINANCE}, signed=False)
        return float(data.get("price", 0))
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        raise

async def get_qty() -> float:
    price = await get_price()
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw_qty, QTY_PRECISION)
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

        await asyncio.sleep(0.25)

        entry = await get_price()
        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(qty),
            "newClientOrderId": oid,
        }

        start = time.time()

        try:
            logger.info(f"Создаю ордер: {params}")
            response = await binance_request("POST", "/fapi/v1/order", params)
            order = {"id": response.get("orderId")}
        except (httpx.TimeoutException, asyncio.TimeoutError):
            await tg_send("Таймаут Binance, пробую ещё раз...")
            await asyncio.sleep(1)
            response = await binance_request("POST", "/fapi/v1/order", params)
            order = {"id": response.get("orderId")}

        if not order or not order.get("id"):
            raise Exception(f"Binance не вернул ID ордера: {response}")

        took = round(time.time() - start, 2)
        logger.info(f"LONG открыт: {order.get('id')} | {took}s")

        position_active = True

        await asyncio.sleep(0.2)
        
        for price, name in [(tp, "tp"), (sl, "sl")]:
            try:
                tp_sl_params = {
                    "symbol": SYMBOL_BINANCE,
                    "side": "SELL",
                    "type": "TAKE_PROFIT_MARKET" if name == "tp" else "STOP_MARKET",
                    "quantity": str(qty),
                    "stopPrice": str(price),
                    "reduceOnly": "true",
                    "newClientOrderId": f"{name}_{oid}",
                }
                tp_sl_response = await binance_request("POST", "/fapi/v1/order", tp_sl_params)
                if "orderId" in tp_sl_response:
                    order_id = tp_sl_response.get("orderId")
                    logger.info(f"Ордер {name} создан: {order_id}")
                else:
                    logger.warning(f"Ордер {name} не создан: {tp_sl_response}")
            except Exception as e:
                logger.warning(f"Ошибка при создании {name}: {e}")

        await tg_send(f"""
<b>LONG ОТКРЫТ</b> за {took}с
${FIXED_AMOUNT_USD} × {LEVERAGE}x | {SYMBOL}
Entry: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code> (+{TP_PERCENT}%)
SL: <code>{sl:.4f}</code> (-{SL_PERCENT}%)
        """.strip())

    except Exception as e:
        logger.error(traceback.format_exc())
        await tg_send(f"Ошибка LONG:\n<code>{str(e)}</code>")
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
    await binance_client.aclose()

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
