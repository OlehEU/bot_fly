# main.py — TERMINATOR 2026 | Исправленный вариант
import os
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
import math
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oz-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
binance_client = httpx.AsyncClient(timeout=60.0)

# ====================== TELEGRAM ======================
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ====================== ПОДПИСЬ ======================
def _create_signature(params: Dict[str, Any], secret: str) -> str:
    # Все значения как строки, отсортировано по ключу
    query_string = '&'.join(f"{k}={str(params[k])}" for k in sorted(params.keys()))
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        signature = _create_signature(params, BINANCE_API_SECRET)
        params["signature"] = signature
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if method.upper() == "GET":
            response = await binance_client.get(url, params=params, headers=headers)
        else:
            response = await binance_client.post(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        try:
            err = e.response.json()
            await tg_send(f"<b>BINANCE ERROR</b>\n<code>{err.get('code', '')}: {err.get('msg', str(e))}</code>")
        except:
            await tg_send(f"<b>BINANCE КРИТИЧКА</b>\n<code>{str(e)[:500]}</code>")
        raise

# ====================== КЭШ SYMBOL ======================
SYMBOL_DATA = {}

async def get_symbol_data(symbol: str):
    if symbol in SYMBOL_DATA:
        return SYMBOL_DATA[symbol]

    info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            step_size = 0.0
            min_qty = 0.0
            precision = s.get("quantityPrecision", 3)
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                    break
            SYMBOL_DATA[symbol] = {"step_size": step_size, "min_qty": min_qty, "precision": precision}
            # Устанавливаем плечо
            try:
                await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": str(LEVERAGE)})
            except:
                pass
            return SYMBOL_DATA[symbol]
    raise Exception(f"Символ не найден: {symbol}")

async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

# ====================== ROUND QTY ======================
def round_step_size(qty: float, step: float) -> str:
    qty = math.floor(qty / step) * step
    return f"{qty:.8f}".rstrip("0").rstrip(".")

async def calc_qty(symbol: str) -> str:
    data = await get_symbol_data(symbol)
    price = await get_price(symbol)
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round_step_size(raw_qty, data["step_size"])
    if float(qty) < data["min_qty"]:
        qty = str(data["min_qty"])
    return qty

# ====================== OPEN LONG ======================
async def open_long(symbol: str):
    try:
        qty = await calc_qty(symbol)
        oid = f"oz_{int(time.time()*1000)}"
        entry = await get_price(symbol)

        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": oid,
            "positionSide": "LONG"
        }

        start = time.time()
        response = await binance_request("POST", "/fapi/v1/order", params)
        if not response.get("orderId"):
            raise Exception(f"Нет orderId: {response}")
        took = round(time.time() - start, 2)

        await tg_send(f"""
<b>LONG {symbol} ОТКРЫТ</b> за {took}с
${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.6f}</code>
Кол-во: {qty}
        """.strip())
    except Exception as e:
        await tg_send(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}</b>\n<code>{str(e)}</code>")

# ====================== CLOSE LONG ======================
async def close_long(symbol: str):
    try:
        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = 0.0
        for p in pos:
            if p.get("symbol") == symbol and p.get("positionSide") == "LONG":
                amt = float(p.get("positionAmt", 0))
                break
        if abs(amt) < 0.001:
            await tg_send(f"{symbol} LONG уже закрыт")
            return
        qty = round_step_size(abs(amt), (await get_symbol_data(symbol))["step_size"])
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
            "positionSide": "LONG"
        })
        await tg_send(f"<b>{symbol} LONG ЗАКРЫТ</b>")
    except Exception as e:
        await tg_send(f"<b>ОШИБКА ЗАКРЫТИЯ {symbol}</b>\n<code>{str(e)}</code>")

# ====================== FASTAPI ======================
app = FastAPI()

@app.on_event("startup")
async def startup():
    await tg_send("<b>TERMINATOR 2026 ЗАПУЩЕН</b>\nГотов к бою!")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>TERMINATOR 2026<br>ONLINE</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    data = await request.json()
    raw_symbol = data.get("symbol", "").upper()
    action = data.get("direction", "").upper()
    symbol = raw_symbol if raw_symbol.endswith("USDT") else raw_symbol + "USDT"

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))

    return {"status": "ok"}
