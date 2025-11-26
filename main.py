# main.py — TERMINATOR 2026 | HEDGE MODE | РАБОТАЕТ НА ЛЮБОЙ МОНЕТЕ | БЕЗ БАГОВ 2025
import os
import time
import hmac
import hashlib
import asyncio
import traceback
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ====================== CONFIG ==========================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

# Теперь бот работает с ЛЮБОЙ монетой из сигнала (XRP, SOL, DOGE и т.д.)
bot    = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))

# Глобальный кэш символов (чтобы не запрашивать exchangeInfo каждый раз)
SYMBOL_CACHE = {}

# ================= TELEGRAM =================
async def tg(msg: str):
    try:
        await bot.send_message(CHAT_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print("TG error:", e)

# ================= BINANCE SIGNATURE =================
def sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted((k, str(v)) for k, v in params.items() if v is not None))
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ================= BINANCE REQUEST =================
async def binance(method: str, path: str, params: dict = None):
    url = f"https://fapi.binance.com{path}"
    p = params or {}
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        data = r.json()
        if isinstance(data, dict) and data.get("code") and data["code"] != 200:
            await tg(f"<b>BINANCE ERROR</b>\n<code>{data['code']}: {data['msg']}</code>")
        return data
    except Exception as e:
        await tg(f"<b>КРИТИЧКА</b>\n<code>{traceback.format_exc()}</code>")
        return {}

# ================= PRECISION & LEVERAGE CACHE =================
async def get_symbol_info(symbol: str):
    if symbol in SYMBOL_CACHE:
        return SYMBOL_CACHE[symbol]
    
    info = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    data = info.json()
    for s in data.get("symbols", []):
        if s["symbol"] == symbol:
            precision = s.get("quantityPrecision", 3)
            min_qty = next((float(f["minQty"]) for f in s.get("filters", []) if f["filterType"] == "LOT_SIZE"), 0.0)
            SYMBOL_CACHE[symbol] = {"precision": precision, "min_qty": min_qty}
            # Устанавливаем плечо один раз при первом обращении
            await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
            return SYMBOL_CACHE[symbol]
    raise Exception(f"Символ {symbol} не найден")

# ================= PRICE & QTY =================
async def get_price(symbol: str) -> float:
    r = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")
    return float(r.json()["price"])

async def calc_qty(symbol: str) -> str:
    info = await get_symbol_info(symbol)
    price = await get_price(symbol)
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw_qty, info["precision"])
    if qty < info["min_qty"]:
        qty = info["min_qty"]
    return f"{qty:.{info['precision']}f}".rstrip("0").rstrip(".")

# ================= OPEN LONG =================
async def open_long(symbol: str):
    try:
        qty = await calc_qty(symbol)
        order = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "positionSide": "LONG"
        })
        if order.get("orderId"):
            price = await get_price(symbol)
            await tg(f"<b>LONG {symbol} ОТКРЫТ</b>\n"
                     f"${FIXED_AMOUNT_USD} × {LEVERAGE}x\n"
                     f"Entry: <code>{price:.6f}</code>\n"
                     f"Кол-во: {qty}")
        else:
            await tg(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}</b>\n{order}")
    except Exception as e:
        await tg(f"<b>КРИТИЧКА ОТКРЫТИЯ</b>\n<code>{traceback.format_exc()}</code>")

# ================= CLOSE LONG =================
async def close_long(symbol: str):
    try:
        pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = 0.0
        for p in pos if isinstance(pos, list) else []:
            if p.get("symbol") == symbol and p.get("positionSide") == "LONG":
                amt = float(p.get("positionAmt", 0))
                break
        if abs(amt) < 0.001:
            await tg(f"{symbol} LONG уже закрыт")
            return
        
        qty = f"{abs(amt):.8f}".rstrip("0").rstrip(".")
        await binance("POST", "/fapi/v1/order", {
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

# ================= FASTAPI =================
app = FastAPI()

@app.on_event("startup")
async def startup():
    await tg("<b>TERMINATOR 2026 ONLINE</b>\nГотов к сигналам OZ SCANNER\nHedge Mode • 10$ • 10x")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>TERMINATOR 2026<br>ONLINE · HEDGE MODE</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    
    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    symbol = data.get("symbol", "").upper()
    if symbol.endswith("USDT"):
        symbol = symbol.replace("USDT", "") + "USDT"
    else:
        symbol += "USDT"
    
    action = data.get("direction", "").upper()

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))
    else:
        return {"error": "unknown direction"}

    return {"status": "ok"}
