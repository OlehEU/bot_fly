# main.py — BINANCE XRP BOT — УЛЬТРА-БЫСТРЫЙ БОТ ДЛЯ ТОРГОВЛИ FUTURES
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

# ====================== КОНФИГ: загрузка переменных окружения ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная окружения: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY     = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET  = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

# ====================== НАСТРОЙКИ БОТА ======================
FIXED_AMOUNT_USD   = float(os.getenv("FIXED_AMOUNT_USD", "10"))  # фиксированная сумма для входа
LEVERAGE           = int(os.getenv("LEVERAGE", "10"))            # плечо
TP_PERCENT         = float(os.getenv("TP_PERCENT", "0.5"))        # тейк-профит %
SL_PERCENT         = float(os.getenv("SL_PERCENT", "1.0"))        # стоп-лосс %
AUTO_CLOSE_MINUTES = int(os.getenv("AUTO_CLOSE_MINUTES", "10"))  # авто-закрытие
BASE_COIN          = "XRP"                                       # торгуемая монета

bot = Bot(token=TELEGRAM_TOKEN)

# ====================== ОТПРАВКА СООБЩЕНИЙ В TELEGRAM ======================
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ====================== НАСТРОЙКИ BINANCE API ======================
BINANCE_BASE_URL = "https://fapi.binance.com"
SYMBOL_BINANCE = f"{BASE_COIN.upper()}USDT"       # формат Binance
SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"         # формат отображения
MARKET = None
MIN_QTY = 0.0
QTY_PRECISION = 3
position_active = False

binance_client = httpx.AsyncClient(timeout=60.0)

# ====================== СОЗДАНИЕ SIG НАПР. ДЛЯ ПОДПИСАННЫХ ЗАПРОСОВ ======================
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


# ====================== УНИВЕРСАЛЬНЫЙ ЗАПРОС К BINANCE ======================
async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    url = f"{BINANCE_BASE_URL}{endpoint}"
    params = params or {}
    
    # добавление подписи если запрос приватный
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        signature = _create_signature(params, BINANCE_API_SECRET)
        params["signature"] = signature
    
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    
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
            raise Exception(f"Binance API error: {error_msg}")
        except:
            raise Exception(f"Binance API error: {e.response.status_code}")

    except Exception as e:
        raise


# ====================== PRELOAD: загрузка инфы о рынке, плеча и т.д. ======================
async def preload():
    global MARKET, MIN_QTY, QTY_PRECISION
    try:
        # загрузка информации о символе (лот, минимальное количество)
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
            QTY_PRECISION = symbol_info.get("quantityPrecision", 3)

        MARKET = {"limits": {"amount": {"min": MIN_QTY}}}

        # установка плеча
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL_BINANCE, "leverage": str(LEVERAGE)})

    except Exception as e:
        logger.error(f"Ошибка при preload: {e}")
        raise


# ====================== ПОЛУЧЕНИЕ ТЕКУЩЕЙ ЦЕНЫ ======================
async def get_price() -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL_BINANCE}, signed=False)
    return float(data.get("price", 0))


# ====================== РАСЧЁТ КОЛИЧЕСТВА XRP ======================
async def get_qty() -> float:
    price = await get_price()
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw_qty, QTY_PRECISION)
    return max(qty, MIN_QTY)


# ====================== ОТКРЫТИЕ LONG ПО РЫНКУ ======================
async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        qty = await get_qty()                     # расчёт количества
        oid = f"xrp_{int(time.time()*1000)}"      # свой ID ордера

        entry = await get_price()                 # цена входа
        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        # параметры для MARKET-ордера
        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(qty),
            "newClientOrderId": oid,
        }

        # отправка ордера
        response = await binance_request("POST", "/fapi/v1/order", params)
        order = {"id": response.get("orderId")}

        position_active = True

        # создание TP и SL
        for price, name in [(tp, "tp"), (sl, "sl")]:
            tp_sl_params = {
                "symbol": SYMBOL_BINANCE,
                "side": "SELL",
                "type": "TAKE_PROFIT_MARKET" if name == "tp" else "STOP_MARKET",
                "quantity": str(qty),
                "stopPrice": str(price),
                "reduceOnly": "true",
                "newClientOrderId": f"{name}_{oid}",
            }
            await binance_request("POST", "/fapi/v1/order", tp_sl_params)

        await tg_send(f"LONG открыт | Entry {entry}")

    except Exception as e:
        await tg_send(f"Ошибка LONG:\n<code>{str(e)}</code>")
        position_active = False


# ====================== LIFESPAN: запуск и остановка приложения ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await preload()
    await tg_send("Bot запущен и готов!")
    yield
    await binance_client.aclose()

app = FastAPI(lifespan=lifespan)

# ====================== ГЛАВНАЯ СТРАНИЦА ======================
@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP Bot — BINANCE — ONLINE</h1>")


# ====================== WEBHOOK ДЛЯ ПРИЁМА СИГНАЛОВ ======================
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    data = await request.json()
    
    if data.get("signal") == "obuy":      # сигнал на покупку
        await tg_send("Сигнал BUY — открываю LONG")
        asyncio.create_task(open_long())

    return {"ok": True}
