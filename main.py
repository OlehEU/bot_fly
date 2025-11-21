# main.py — BINANCE XRP BOT — УЛЬТРА-БЫСТРЫЙ БОТ ДЛЯ ТОРГОВЛИ FUTURES
import os
import time
import logging
import asyncio
import httpx
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("binance-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная окружения: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# ====================== НАСТРОЙКИ ТОРГОВЛИ ======================
FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))      # сколько $ на сделку
LEVERAGE = int(os.getenv("LEVERAGE", "10"))                        # плечо
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))                 # тейк-профит в %
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))                 # стоп-лосс в %
AUTO_CLOSE_MINUTES = int(os.getenv("AUTO_CLOSE_MINUTES", "10"))   # не используется пока
BASE_COIN = "XRP"

# ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
# НОВАЯ ПЕРЕМЕННАЯ — ОТКЛЮЧЕНИЕ TP/SL
# Добавь в свой .env строку:
# DISABLE_TPSL=true   → бот НЕ будет ставить TP и SL
# DISABLE_TPSL=false  → будет ставить как обычно (по умолчанию false)
DISABLE_TPSL = os.getenv("DISABLE_TPSL", "true").lower() == "true"
# →→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→

bot = Bot(token=TELEGRAM_TOKEN)

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
BINANCE_BASE_URL = "https://fapi.binance.com"
SYMBOL_BINANCE = f"{BASE_COIN.upper()}USDT"
SYMBOL = f"{BASE_COIN.upper()}/USDT:USDT"
MIN_QTY = 0.0
QTY_PRECISION = 3
position_active = False
binance_client = httpx.AsyncClient(timeout=60.0)

# ====================== ОТПРАВКА В ТЕЛЕГУ ======================
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ====================== ПОДПИСЬ ДЛЯ BINANCE ======================
def _create_signature(params: Dict[str, Any], secret: str) -> str:
    normalized = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items() if v is not None}
    query_string = urllib.parse.urlencode(normalized)
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

# ====================== УНИВЕРСАЛЬНЫЙ ЗАПРОС К BINANCE ======================
async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True):
    url = f"{BINANCE_BASE_URL}{endpoint}"
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _create_signature(params, BINANCE_API_SECRET)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        response = await (binance_client.get if method == "GET" else binance_client.post)(
            url, params=params, headers=headers
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("msg", str(e.response.json()))
        except:
            msg = e.response.text
        raise Exception(f"Binance API error: {msg}")
    except Exception as e:
        raise e

# ====================== ЗАГРУЗКА ИНФЫ О СИМВОЛЕ И ПЛЕЧЕ ======================
async def preload():
    global MIN_QTY, QTY_PRECISION
    try:
        info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info.get("symbols", []):
            if s["symbol"] == SYMBOL_BINANCE:
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        MIN_QTY = float(f["minQty"])
                QTY_PRECISION = s.get("quantityPrecision", 3)
                break
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL_BINANCE, "leverage": LEVERAGE})
        await tg_send("XRP Bot запущен и готов!\nTP/SL: " + ("ВЫКЛЮЧЕНЫ" if DISABLE_TPSL else "включены"))
    except Exception as e:
        await tg_send(f"Ошибка запуска: {e}")

# ====================== ЦЕНА И КОЛИЧЕСТВО ======================
async def get_price() -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL_BINANCE}, signed=False)
    return float(data["price"])

async def get_qty() -> float:
    price = await get_price()
    raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw, QTY_PRECISION)
    return max(qty, MIN_QTY)

# ====================== ОТКРЫТИЕ LONG (С ОПЦИЕЙ ОТКЛЮЧЕНИЯ TP/SL) ======================
async def open_long():
    global position_active
    try:
        # Проверка: нет ли уже открытой позиции
        pos_info = await binance_request("GET", "/fapi/v2/positionRisk")
        for p in pos_info:
            if p["symbol"] == SYMBOL_BINANCE and float(p.get("positionAmt", 0)) != 0:
                await tg_send(f"Позиция уже открыта: {p['positionAmt']} XRP")
                position_active = True
                return

        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"
        entry = await get_price()
        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)

        # MARKET ордер на вход
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL_BINANCE, "side": "BUY", "type": "MARKET",
            "quantity": str(qty), "newClientOrderId": oid
        })

        position_active = True

        # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
        # ОТКЛЮЧЕНИЕ TP/SL — главная фича
        if DISABLE_TPSL:
            await tg_send(f"""
<b>LONG ОТКРЫТ БЕЗ TP/SL</b>
${FIXED_AMOUNT_USD} × {LEVERAGE}x | {SYMBOL}
Entry: <code>{entry:.4f}</code>
<i>TP и SL отключены (DISABLE_TPSL=true)</i>
            """.strip())
            return
            
        # DISABLE_TPSL=true   # → бот НЕ ставит TP и SL
        # DISABLE_TPSL=false  # → ставит как обычно (можно вообще не писать)    
        # →→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→

        # Обычная установка TP и SL
        for price, name in [(tp, "tp"), (sl, "sl")]:
            await binance_request("POST", "/fapi/v1/order", {
                "symbol": SYMBOL_BINANCE, "side": "SELL",
                "type": "TAKE_PROFIT_MARKET" if name == "tp" else "STOP_MARKET",
                "quantity": str(qty), "stopPrice": str(price),
                "reduceOnly": "true", "newClientOrderId": f"{name}_{oid}"
            })

        await tg_send(f"""
<b>LONG ОТКРЫТ</b>
${FIXED_AMOUNT_USD} × {LEVERAGE}x | {SYMBOL}
Entry: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code> (+{TP_PERCENT}%)
SL: <code>{sl:.4f}</code> (-{SL_PERCENT}%)
        """.strip())

    except Exception as e:
        await tg_send(f"Ошибка открытия LONG:\n<code>{str(e)}</code>")
        position_active = False

# ====================== ЗАКРЫТИЕ ВСЕГО ПО XRP ======================
async def close_all_xrp():
    global position_active
    try:
        positions = await binance_request("GET", "/fapi/v2/positionRisk")
        current = None
        for p in positions:
            if p["symbol"] == SYMBOL_BINANCE and float(p.get("positionAmt", 0)) != 0:
                current = p
                break

        if current:
            qty = abs(float(current["positionAmt"]))
            side = "SELL" if float(current["positionAmt"]) > 0 else "BUY"
            await binance_request("POST", "/fapi/v1/order", {
                "symbol": SYMBOL_BINANCE, "side": side, "type": "MARKET",
                "quantity": str(qty), "reduceOnly": "true"
            })

        # Отмена всех открытых ордеров (даже если TP/SL отключены — на всякий случай)
        open_orders = await binance_request("GET", "/fapi/v1/openOrders", {"symbol": SYMBOL_BINANCE})
        cancelled = 0
        if open_orders:
            for order in open_orders:
                await binance_request("DELETE", "/fapi/v1/order", {
                    "symbol": SYMBOL_BINANCE, "orderId": order["orderId"]
                })
                cancelled += 1

        position_active = False
        await tg_send(f"Позиция закрыта по рынку\nОтменено ордеров: {cancelled}")

    except Exception as e:
        await tg_send(f"Ошибка close_all: {str(e)}")

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await preload()
    yield
    await binance_client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP Binance Bot — ONLINE</h1>")

# ====================== WEBHOOK ======================
@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if data.get("secret") != "supersecret123":
        raise HTTPException(status_code=403, detail="Wrong secret")

    signal = data.get("signal", "").lower()

    if signal in ["buy", "long", "obuy", "open"]:
        await tg_send("Сигнал BUY — открываю LONG")
        asyncio.create_task(open_long())

    elif signal == "close_all":
        await tg_send("Сигнал CLOSE_ALL — закрываю позицию и все ордера")
        asyncio.create_task(close_all_xrp())

    else:
        await tg_send(f"Неизвестный сигнал: {signal}")

    return {"ok": True, "signal": signal}
