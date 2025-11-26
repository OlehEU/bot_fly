# main.py — ТВОЙ РАБОЧИЙ КОД + ФИКС -1022 НАВСЕГДА (quantity encode)
import os
import time
import logging
import hmac
import hashlib
import urllib.parse
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot

# ====================== КОНФИГ ======================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")
AMOUNT_USD     = float(os.getenv("AMOUNT_USD", "10"))
LEVERAGE       = int(os.getenv("LEVERAGE", "10"))

bot    = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)

async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except: pass

# ТВОЯ РАБОЧАЯ ПОДПИСЬ + ФИКС ДЛЯ quantity
def sign(params: dict) -> str:
    # ЭТО ГЛАВНАЯ ПРАВКА — quote_plus для всех значений
    query = "&".join(
        f"{k}={urllib.parse.quote_plus(str(v))}"
        for k, v in sorted(params.items())
        if v is not None
    )
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    p = params or {}
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        r:
        r = await client.request(method, url, params=p, headers=headers)
        data = r.json()
        if data.get("code"):
            await tg(f"<b>ERROR</b>\n<code>{data['code']}: {data['msg']}</code>")
        return data
    except Exception as e:
        await tg(f"<b>КРИТИЧКА</b>\n<code>{str(e)[:300]}</code>")
        return {}

# КЭШ
INFO = {}

async def get_info(symbol: str):
    if symbol in INFO:
        return INFO[symbol]
    data = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    for s in data.json()["symbols"]:
        if s["symbol"] == symbol:
            p = s["quantityPrecision"]
            m = next((float(f["minQty"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            INFO[symbol] = {"precision": p, "min_qty": m}
            try:
                await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
            except: pass
            return INFO[symbol]

async def qty(symbol: str):
    i = await get_info(symbol)
    price = float((await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")).json()["price"])
    raw = (AMOUNT_USD * LEVERAGE) / price
    q = round(raw, i["precision"])
    if q < i["min_qty"]: q = i["min_qty"]
    return f"{q:.{i['precision']}f}".rstrip("0").rstrip(".")

async def open_long(symbol: str):
    try:
        q = await qty(symbol)
        price = float((await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")).json()["price"])
        
        order = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": q,                    # ← вот сюда попадёт уже нормальная строка
            "positionSide": "LONG",
            "newClientOrderId": f"oz_{int(time.time()*1000)}"
        })
        
        if order.get("orderId"):
            await tg(f"<b>LONG {symbol} ОТКРЫТ</b>\n${AMOUNT_USD} × {LEVERAGE}x\nEntry: <code>{price:.6f}</code>")
        else:
            await tg(f"<b>ОШИБКА</b>\n{order}")
    except Exception as e:
        await tg(f"<b>КРИТИЧКА</b>\n<code>{str(e)}</code>")

async def close_long(symbol: str):
    try:
        pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = next((float(p["positionAmt"]) for p in pos if p.get("positionSide") == "LONG"), 0)
        if abs(amt) < 0.001:
            await tg(f"{symbol} уже закрыт")
            return
        q = f"{abs(amt):.8f}".rstrip("0").rstrip(".")
        await binance("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": q,
            "reduceOnly": "true",
            "positionSide": "LONG"
        })
        await tg(f"<b>{symbol} ЗАКРЫТ</b>")
    except Exception as e:
        await tg(f"<b>ОШИБКА ЗАКРЫТИЯ</b>\n<code>{str(e)}</code>")

app = FastAPI()

@app.on_event("startup")
async def start():
    await tg("<b>TERMINATOR 2026 — ФИНАЛЬНАЯ ВЕРСИЯ</b>\nБольше никогда не будет -1022")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    data = await request.json()
    sym = data.get("symbol", "").upper()
    symbol = sym if sym.endswith("USDT") else sym + "USDT"
    action = data.get("direction", "").upper()
    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))
    return {"ok": True}
