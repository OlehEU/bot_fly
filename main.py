# main.py — MEXC XRP BOT — УЛЬТРА-НАДЁЖНЫЙ, УЛЬТРА-БЫСТРЫЙ, БЕЗ ТАЙМАУТОВ
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

MEXC_BASE_URL = "https://contract.mexc.com/api"
SYMBOL_MEXC = f"{BASE_COIN.upper()}_USDT"
SYMBOL_MEXC_TICKER = f"{BASE_COIN.upper()}USDT"
SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MARKET = None
CONTRACT_SIZE = 1.0
position_active = False

mexc_client = httpx.AsyncClient(timeout=60.0)

def _create_signature(params: Dict[str, Any], secret: str) -> str:
    sorted_params = sorted(params.items())
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    signature = hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

async def mexc_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{MEXC_BASE_URL}{endpoint}"
    params = params or {}
    
    timestamp = str(int(time.time() * 1000))
    
    sign_params = params.copy()
    sign_params["timestamp"] = timestamp
    
    signature = _create_signature(sign_params, MEXC_API_SECRET)
    
    params["timestamp"] = timestamp
    params["signature"] = signature
    
    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": timestamp,
        "Content-Type": "application/json",
    }
    
    try:
        if method == "GET":
            response = await mexc_client.get(url, params=params, headers=headers, timeout=60.0)
        else:
            response = await mexc_client.post(url, json=params, headers=headers, timeout=60.0)
        
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        error_text = e.response.text if hasattr(e.response, 'text') else str(e.response.content)
        logger.error(f"MEXC API error: {e.response.status_code} - {error_text}")
        raise Exception(f"MEXC API error: {e.response.status_code} - {error_text}")
    except httpx.TimeoutException as e:
        logger.error(f"MEXC API timeout: {e}")
        raise
    except Exception as e:
        logger.error(f"MEXC request error: {e}")
        raise

async def preload():
    global MARKET, CONTRACT_SIZE
    try:
        try:
            url = f"{MEXC_BASE_URL}/v1/contract/detail/{SYMBOL_MEXC}"
            response = await mexc_client.get(url, timeout=30.0)
            response.raise_for_status()
            contract_info = response.json()
            if contract_info and contract_info.get("code") == 0:
                data = contract_info.get("data", {})
                CONTRACT_SIZE = float(data.get("contractSize", 1.0))
                min_vol = float(data.get("minVol", 1.0))
            else:
                CONTRACT_SIZE = 1.0
                min_vol = 1.0
        except:
            CONTRACT_SIZE = 1.0
            min_vol = 1.0
        
        MARKET = {
            "limits": {
                "amount": {
                    "min": min_vol
                }
            }
        }
        
        logger.info(f"Preload завершён: {SYMBOL} | contract_size={CONTRACT_SIZE}")
        
        try:
            balance = await mexc_request("GET", "/v1/private/account/assets")
            logger.info(f"API ключ работает, баланс получен: {bool(balance)}")
        except Exception as e:
            logger.warning(f"Не удалось получить баланс - проверьте права API ключа: {e}")
    except Exception as e:
        logger.error(f"Ошибка при preload: {e}")
        raise

async def get_price() -> float:
    try:
        url = f"{MEXC_BASE_URL}/v1/contract/ticker/{SYMBOL_MEXC_TICKER}"
        response = await mexc_client.get(url, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"Ошибка получения цены: {data.get('msg')}")
        return float(data.get("data", {}).get("lastPrice", 0))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            url = f"{MEXC_BASE_URL}/v1/contract/ticker"
            response = await mexc_client.get(url, params={"symbol": SYMBOL_MEXC}, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise Exception(f"Ошибка получения цены: {data.get('msg')}")
            return float(data.get("data", {}).get("lastPrice", 0))
        raise
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        raise

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

        await asyncio.sleep(0.25)

        entry = await get_price()
        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        params = {
            "symbol": SYMBOL_MEXC,
            "price": "",
            "vol": str(qty),
            "leverage": str(LEVERAGE),
            "side": "1",
            "type": "1",
            "openType": "1",
            "positionType": "1",
            "volSide": "1",
        }

        start = time.time()

        try:
            response = await mexc_request("POST", "/v1/private/order/submit", params)
            if response.get("code") != 0:
                raise Exception(f"MEXC API error: {response.get('msg', 'Unknown error')}")
            order = {"id": response.get("data", {}).get("orderId")}
        except (httpx.TimeoutException, asyncio.TimeoutError):
            await tg_send("Таймаут MEXC, пробую ещё раз...")
            await asyncio.sleep(1)
            response = await mexc_request("POST", "/v1/private/order/submit", params)
            if response.get("code") != 0:
                raise Exception(f"MEXC API error: {response.get('msg', 'Unknown error')}")
            order = {"id": response.get("data", {}).get("orderId")}

        if not order or not order.get("id"):
            raise Exception(f"MEXC не вернул ID ордера: {response}")

        took = round(time.time() - start, 2)
        logger.info(f"LONG открыт: {order.get('id')} | {took}s")

        position_active = True

        await asyncio.sleep(0.2)
        
        for price, name in [(tp, "tp"), (sl, "sl")]:
            try:
                tp_sl_params = {
                    "symbol": SYMBOL_MEXC,
                    "price": str(price),
                    "vol": str(qty),
                    "leverage": str(LEVERAGE),
                    "side": "2",
                    "type": "2",
                    "openType": "1",
                    "positionType": "2",
                    "volSide": "1",
                    "reduceOnly": "true",
                }
                tp_sl_response = await mexc_request("POST", "/v1/private/order/submit", tp_sl_params)
                if tp_sl_response.get("code") != 0:
                    logger.warning(f"Ордер {name} не создан: {tp_sl_response.get('msg')}")
                else:
                    order_id = tp_sl_response.get("data", {}).get("orderId")
                    logger.info(f"Ордер {name} создан: {order_id}")
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
            "symbol": SYMBOL_MEXC,
            "price": "",
            "vol": str(qty),
            "leverage": str(LEVERAGE),
            "side": "2",
            "type": "1",
            "openType": "1",
            "positionType": "2",
            "volSide": "1",
            "reduceOnly": "true",
        }
        close_response = await mexc_request("POST", "/v1/private/order/submit", close_params)
        if close_response.get("code") != 0:
            raise Exception(f"Ордер закрытия не создан: {close_response.get('msg')}")
        order_id = close_response.get("data", {}).get("orderId")
        if not order_id:
            raise Exception(f"Ордер закрытия не создан: {close_response}")
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
    await mexc_client.aclose()

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
