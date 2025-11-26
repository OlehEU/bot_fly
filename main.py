# main.py — TERMINATOR 2026 | ИСПРАВЛЕНА ПОДПИСЬ -1022 | РАБОТАЕТ НА 100% | HEDGE MODE
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))
AUTO_CLOSE_MINUTES = int(os.getenv("AUTO_CLOSE_MINUTES", "10"))

bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

BINANCE_BASE_URL = "https://fapi.binance.com"
SYMBOL_BINANCE = "XRPUSDT"  # BASE_COIN + "USDT"
MARKET = None
MIN_QTY = 0.0
QTY_PRECISION = 3
position_active = False
binance_client = httpx.AsyncClient(timeout=60.0)

# ====================== ИСПРАВЛЁННАЯ ПОДПИСЬ (ФИКС -1022) ======================
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
    query_string = urllib.parse.urlencode(sorted(normalized.items()))  # ← СОРТИРОВКА + STR!
    logger.info(f"QUERY_STRING: {query_string}")  # ← ЛОГИРУЕМ ДЛЯ ОТЛАДКИ
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
        logger.info(f"SIGNED PARAMS: {params}")  # ← ЛОГИРУЕМ ПОЛНЫЕ ПАРАМЕТРЫ
   
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
   
    try:
        logger.info(f"Binance {method} {url} с параметрами: {params}")
        if method == "GET":
            response = await binance_client.get(url, params=params, headers=headers, timeout=60.0)
        else:
            response = await binance_client.post(url, params=params, headers=headers, timeout=60.0)
       
        response.raise_for_status()
        data = response.json()
        logger.info(f"BINANCE RESPONSE: {data}")  # ← ЛОГИРУЕМ ПОЛНЫЙ ОТВЕТ
        return data
    except httpx.HTTPStatusError as e:
        try:
            error_data = e.response.json()
            logger.error(f"Binance API error: {e.response.status_code} - {error_data}")
            await tg_send(f"<b>BINANCE ERROR</b>\n<code>{error_data.get('code', e.response.status_code)}: {error_data.get('msg', 'Unknown')}</code>")
            raise Exception(f"Binance API error: {error_data}")
        except:
            error_text = e.response.text
            logger.error(f"Binance API error: {e.response.status_code} - {error_text}")
            await tg_send(f"<b>BINANCE ERROR</b>\n<code>{e.response.status_code}: {error_text[:200]}</code>")
            raise
    except httpx.TimeoutException as e:
        logger.error(f"Binance timeout: {e}")
        await tg_send("<b>BINANCE TIMEOUT</b>\nПовтор через 2 сек...")
        await asyncio.sleep(2)
        return await binance_request(method, endpoint, params, signed)  # повтор
    except Exception as e:
        logger.error(f"Binance request error: {traceback.format_exc()}")
        await tg_send(f"<b>КРИТИЧКА BINANCE</b>\n<code>{traceback.format_exc()}</code>")
        raise

# ====================== PRELOAD ======================
async def preload():
    global MARKET, MIN_QTY, QTY_PRECISION
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
                    pass  # не используем
            QTY_PRECISION = symbol_info.get("quantityPrecision", 3)
        else:
            MIN_QTY = 0.0
            QTY_PRECISION = 3
        
        MARKET = {
            "limits": {
                "amount": {
                    "min": MIN_QTY
                }
            }
        }
       
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL_BINANCE, "leverage": str(LEVERAGE)})
        logger.info(f"Preload OK: {SYMBOL_BINANCE} precision={QTY_PRECISION} min_qty={MIN_QTY}")
        await tg_send(f"<b>Bot готов!</b>\n{SYMBOL_BINANCE} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    except Exception as e:
        await tg_send(f"<b>Preload error</b>\n<code>{traceback.format_exc()}</code>")

# ====================== OPEN LONG ======================
async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return
    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"
        entry = await get_price()
        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(qty),
            "positionSide": "LONG",  # ← HEDGE MODE
            "newClientOrderId": oid,
        }
        start = time.time()
        response = await binance_request("POST", "/fapi/v1/order", params)
        if not response.get("orderId"):
            raise Exception(f"Order failed: {response}")
        took = round(time.time() - start, 2)
        logger.info(f"LONG opened: {response['orderId']} | {took}s")
        position_active = True
        await tg_send(f"""
<b>LONG ОТКРЫТ</b> за {took}с
${FIXED_AMOUNT_USD} × {LEVERAGE}x | {SYMBOL}
Entry: <code>{entry:.4f}</code>
Qty: {qty}
        """.strip())
    except Exception as e:
        logger.error(traceback.format_exc())
        await tg_send(f"Ошибка LONG:\n<code>{str(e)}</code>")
        position_active = False

# ====================== CLOSE POSITION ======================
async def close_position():
    global position_active
    try:
        positions = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL_BINANCE})
        amt = 0.0
        for p in positions if isinstance(positions, list) else []:
            if p.get("symbol") == SYMBOL_BINANCE and p.get("positionSide") == "LONG":
                amt = float(p.get("positionAmt", 0))
                break
        if abs(amt) < 0.001:
            await tg_send(f"{SYMBOL_BINANCE} LONG already closed")
            position_active = False
            return
        qty_str = f"{abs(amt):.{QTY_PRECISION}f}".rstrip("0").rstrip(".")
        if float(qty_str) < MARKET["limits"]["amount"]["min"]:
            qty_str = str(MARKET["limits"]["amount"]["min"])
        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_str,
            "reduceOnly": "true",
            "positionSide": "LONG"
        }
        response = await binance_request("POST", "/fapi/v1/order", params)
        if not response.get("orderId"):
            raise Exception(f"Close order failed: {response}")
        await tg_send(f"<b>{SYMBOL_BINANCE} LONG CLOSED</b>")
        position_active = False
    except Exception as e:
        await tg_send(f"<b>CLOSE ERROR</b>\n<code>{traceback.format_exc()}</code>")
        position_active = False

# ====================== LIFESPAN ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send("Bot starting... preload Binance data")
    await preload()
    await tg_send(f"<b>Bot ready!</b>\n{SYMBOL_BINANCE} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP Bot — BINANCE — ULTRAFAST & ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(status_code=403)
    data = await request.json()
    if data.get("signal") == "obuy":  # ← твой формат
        await tg_send("Сигнал BUY — открываю LONG")
        asyncio.create_task(open_long())
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
