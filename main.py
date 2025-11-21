# main.py — BINANCE XRP BOT — ULTRA-НАДЁЖНЫЙ, ULTRA-БЫСТРЫЙ, ФИКСИРОВАННОЕ КОЛИЧЕСТВО XRP
import os
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("xrp-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная окружения: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD  = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE          = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT        = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT        = float(os.getenv("SL_PERCENT", "1.0"))
BASE_COIN         = "XRP"
FIXED_QTY         = 3  # Жёстко заданное количество XRP

bot = Bot(token=TELEGRAM_TOKEN)
binance_client = httpx.AsyncClient(timeout=60.0)

BINANCE_BASE_URL = "https://fapi.binance.com"
SYMBOL_BINANCE = f"{BASE_COIN.upper()}USDT"
QTY_PRECISION = 3  # для XRP обычно 3 знака после запятой

position_active = False

# ====================== HELPERS ======================
def create_signature(params: Dict[str, Any]) -> str:
    query_string = urllib.parse.urlencode(params)
    return hmac.new(BINANCE_API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    url = f"{BINANCE_BASE_URL}{endpoint}"
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = create_signature(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if method == "GET":
            r = await binance_client.get(url, params=params, headers=headers)
        else:
            r = await binance_client.post(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Binance error: {e}")
        raise

async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

async def get_price() -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL_BINANCE}, signed=False)
    return float(data.get("price", 0))

# ====================== OPEN LONG ======================
async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        qty = f"{FIXED_QTY:.{QTY_PRECISION}f}"  # фиксированное количество XRP с корректной точностью
        entry = await get_price()
        if entry <= 0:
            await tg_send("Не удалось получить цену XRP")
            return

        tp = round(entry * (1 + TP_PERCENT / 100), 4)
        sl = round(entry * (1 - SL_PERCENT / 100), 4)
        oid = f"xrp_{int(time.time() * 1000)}"
        start = time.time()

        # MARKET BUY
        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": oid,
        }
        response = await binance_request("POST", "/fapi/v1/order", params)
        order_id = response.get("orderId")
        if not order_id:
            raise Exception(f"Binance не вернул ID ордера: {response}")

        # TP и SL
        for price, name in [(tp, "tp"), (sl, "sl")]:
            tp_sl_params = {
                "symbol": SYMBOL_BINANCE,
                "side": "SELL",
                "type": "TAKE_PROFIT_MARKET" if name == "tp" else "STOP_MARKET",
                "quantity": qty,
                "stopPrice": f"{price:.4f}",
                "reduceOnly": "true",
                "newClientOrderId": f"{name}_{oid}",
            }
            await binance_request("POST", "/fapi/v1/order", tp_sl_params)

        took = round(time.time() - start, 2)
        position_active = True

        await tg_send(f"""
<b>LONG XRP открыт</b> за {took}s
Количество: {qty} XRP
Вход: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code>
SL: <code>{sl:.4f}</code>
        """.strip())

    except Exception as e:
        position_active = False
        await tg_send(f"Ошибка открытия LONG:\n<code>{str(e)}</code>")

# ====================== LIFESPAN ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send("XRP Bot стартует...")
    # Установка плеча
    try:
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL_BINANCE, "leverage": LEVERAGE})
        logger.info(f"Плечо установлено: {LEVERAGE}x")
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e}")
    yield
    await binance_client.aclose()

# ====================== FASTAPI ======================
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    price = await get_price()
    return HTMLResponse(f"<h1>XRP Bot — Цена {price:.4f} USDT</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    signal = data.get("signal", "").lower()
    if signal in ["buy", "long", "obuy", "go", "лонг", "вход"]:
        await tg_send("Сигнал BUY — открываю LONG XRP")
        asyncio.create_task(open_long())
    return {"ok": True}
