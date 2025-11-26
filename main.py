# main.py — TERMINATOR 2026 ULTRA | ОСНОВА — ТВОЙ РАБОЧИЙ КОД | ПОД OZ SCANNER | ЛЮБАЯ МОНЕТА
import os
import time
import hmac
import hashlib
import urllib.parse
import logging
import asyncio
import traceback
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("terminator")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "supersecret123")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=60.0)

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== ПОДПИСЬ — 1 В 1 КАК В ТВОЁМ РАБОЧЕМ БОТЕ ======================
def _create_signature(params: dict) -> str:
    normalized = {}
    for k, v in params.items():
        if v is None: continue
        normalized[k] = str(v) if not isinstance(v, bool) else str(v).lower()
    query_string = urllib.parse.urlencode(sorted(normalized.items()))
    return hmac.new(BINANCE_API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: dict = None, signed: bool = True):
    url = f"https://fapi.binance.com{endpoint}"
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = _create_signature(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        resp = await client.request(method, url, params=p, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        error_msg = str(e)
        try:
            error_msg = resp.json().get("msg", error_msg)
        except: pass
        await tg(f"<b>BINANCE ОШИБКА</b>\n<code>{error_msg}</code>")
        raise

# ====================== КЭШ ИНФЫ ПО СИМВОЛАМ ======================
SYMBOL_CACHE = {}

async def get_symbol_info(symbol: str):
    if symbol in SYMBOL_CACHE:
        return SYMBOL_CACHE[symbol]
    
    data = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in data["symbols"]:
        if s["symbol"] == symbol:
            precision = s["quantityPrecision"]
            min_qty = next((float(f["minQty"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            SYMBOL_CACHE[symbol] = {"precision": precision, "min_qty": min_qty}
            await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
            return SYMBOL_CACHE[symbol]
    raise Exception(f"Символ не найден: {symbol}")

async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

async def calc_qty(symbol: str) -> str:
    info = await get_symbol_info(symbol)
    price = await get_price(symbol)
    raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw, info["precision"])
    if qty < info["min_qty"]:
        qty = info["min_qty"]
    return f"{qty:.{info['precision']}f}".rstrip("0").rstrip(".")

# ====================== ОТКРЫТИЕ LONG ======================
async def open_long(symbol: str):
    try:
        qty = await calc_qty(symbol)
        await asyncio.sleep(0.2)
        price = await get_price(symbol)
        
        order = await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "positionSide": "LONG"
        })
        
        if order.get("orderId"):
            await tg(f"<b>LONG {symbol} ОТКРЫТ</b>\n"
                     f"${FIXED_AMOUNT_USD} × {LEVERAGE}x\n"
                     f"Entry: <code>{price:.6f}</code>\n"
                     f"Кол-во: {qty}")
        else:
            await tg(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}</b>\n{order}")
    except Exception as e:
        await tg(f"<b>КРИТИЧКА ОТКРЫТИЯ</b>\n<code>{traceback.format_exc()}</code>")

# ====================== ЗАКРЫТИЕ LONG ======================
async def close_long(symbol: str):
    try:
        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = 0.0
        for p in pos:
            if p.get("symbol") == symbol and p.get("positionSide") == "LONG":
                amt = float(p.get("positionAmt", 0))
                break
        if abs(amt) < 0.001:
            await tg(f"{symbol} LONG уже закрыт")
            return
        
        qty = f"{abs(amt):.8f}".rstrip("0").rstrip(".")
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
            "positionSide": "LONG"
        })
        await tg(f"<b>{symbol} LONG ЗАКРЫТ</b>")
    except Exception as e:
        await tg(f"<b>ОШИБКА ЗАКРЫТИЯ</b>\n<code>{traceback.format_exc()}</code>")

# ====================== FASTAPI ======================
app = FastAPI()

@app.on_event("startup")
async def start():
    await tg("<b>TERMINATOR 2026 ULTRA ЗАПУЩЕН</b>\nГотов принимать сигналы от OZ SCANNER\nЛюбая монета • Hedge Mode")

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>TERMINATOR 2026 ULTRA<br>ONLINE · ГОТОВ К СИГНАЛАМ</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    
    data = await request.json()
    raw_symbol = data.get("symbol", "").upper()
    action = data.get("direction", "").upper()

    symbol = raw_symbol + "USDT" if not raw_symbol.endswith("USDT") else raw_symbol

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))

    return {"status": "ok"}
