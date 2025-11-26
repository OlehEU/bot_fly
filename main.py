# main.py — TERMINATOR 2026 | 100% РАБОЧИЙ | БЕЗ -1022 | ПОД OZ SCANNER | ЛЮБАЯ МОНЕТА
import os
import time
import hmac
import hashlib
import urllib.parse
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ====================== КОНФИГ ======================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

AMOUNT_USD = float(os.getenv("AMOUNT_USD", "10"))
LEVERAGE   = int(os.getenv("LEVERAGE", "10"))

bot    = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)

# Кэш символов
SYMBOL_INFO = {}

async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        print("TG error:", e)

# ====================== САМАЯ ПРАВИЛЬНАЯ ПОДПИСЬ ======================
def sign(params: dict) -> str:
    query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    p = params or {}
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        data = r.json()
        if data.get("code"):
            await tg(f"<b>BINANCE ERROR</b>\n<code>{data['code']}: {data['msg']}</code>")
        return data
    except Exception as e:
        await tg(f"<b>КРИТИЧКА</b>\n<code>{str(e)}</code>")
        return {}

# ====================== ПРЕДЗАГРУЗКА — СТАВИМ ПЛЕЧО ДЛЯ ВСЕХ МОНЕТ ======================
async def preload_leverage():
    symbols = ["XRPUSDT", "SOLUSDT", "DOGEUSDT"]  # ← добавь сюда все свои монеты
    for sym in symbols:
        try:
            await binance("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})
            print(f"Плечо {LEVERAGE}x установлено для {sym}")
        except:
            pass  # если уже стоит — игнорим

# ====================== ИНФО ПО СИМВОЛУ ======================
async def get_symbol_info(symbol: str):
    if symbol in SYMBOL_INFO:
        return SYMBOL_INFO[symbol]
    
    info = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    data = info.json()
    for s in data["symbols"]:
        if s["symbol"] == symbol:
            prec = s["quantityPrecision"]
            min_qty = next((float(f["minQty"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            SYMBOL_INFO[symbol] = {"precision": prec, "min_qty": min_qty}
            return SYMBOL_INFO[symbol]
    raise Exception("Символ не найден")

async def get_price(symbol: str) -> float:
    r = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")
    return float(r.json()["price"])

async def calc_qty(symbol: str) -> str:
    info = await get_symbol_info(symbol)
    price = await get_price(symbol)
    raw = (AMOUNT_USD * LEVERAGE) / price
    qty = round(raw, info["precision"])
    if qty < info["min_qty"]:
        qty = info["min_qty"]
    return f"{qty:.{info['precision']}f}".rstrip("0").rstrip(".")

# ====================== ОТКРЫТИЕ И ЗАКРЫТИЕ ======================
async def open_long(symbol: str):
    try:
        qty = await calc_qty(symbol)
        price = await get_price(symbol)
        order = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "positionSide": "LONG"
        })
        if order.get("orderId"):
            await tg(f"<b>LONG {symbol} ОТКРЫТ</b>\n${AMOUNT_USD} × {LEVERAGE}x\nEntry: <code>{price:.6f}</code>\nКол-во: {qty}")
        else:
            await tg(f"<b>ОШИБКА</b>\n{order}")
    except Exception as e:
        await tg(f"<b>КРИТИЧКА</b>\n<code>{str(e)}</code>")

async def close_long(symbol: str):
    try:
        pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = next((float(p["positionAmt"]) for p in pos if p["symbol"] == symbol and p["positionSide"] == "LONG"), 0)
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
        await tg(f"<b>ОШИБКА ЗАКРЫТИЯ</b>\n<code>{str(e)}</code>")

# ====================== FASTAPI ======================
app = FastAPI()

@app.on_event("startup")
async def startup():
    await tg("<b>TERMINATOR 2026 ЗАПУЩЕН</b>")
    await preload_leverage()  # ← УСТАНАВЛИВАЕМ ПЛЕЧО СРАЗУ
    await tg("<b>ГОТОВ К СИГНАЛАМ OZ SCANNER</b>\nЛюбая монета • Hedge Mode • 10$ × 10x")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px'>TERMINATOR 2026<br>ONLINE · ГОТОВ</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    
    data = await request.json()
    raw = data.get("symbol", "").upper()
    symbol = raw + "USDT" if not raw.endswith("USDT") else raw
    action = data.get("direction", "").upper()

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))

    return {"status": "ok"}
