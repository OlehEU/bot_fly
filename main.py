# main.py — ТЕРМИНАТОР 2026 | ФИКС ОШИБКИ -4061 | ХЕДЖ МОД | ЛЮБАЯ МОНЕТА
import os
import time
import hmac
import hashlib
import urllib.parse
import datetime
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

if not all([TOKEN, CHAT_ID, BINANCE_KEY, BINANCE_SECRET]):
    raise Exception("Нет ключей!")

bot = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=15.0)
app = FastAPI()

def create_signature(query_string: str) -> str:
    return hmac.new(BINANCE_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    base_params = {k: str(v) for k, v in params.items() if v is not None}
    query_parts = sorted(base_params.items())
    query_string = urllib.parse.urlencode(query_parts)
    timestamp = int(time.time() * 1000)
    if query_string:
        query_string += "&"
    query_string += f"timestamp={timestamp}"
    signature = create_signature(query_string)
    query_string += f"&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        full_url = url + "?" + query_string
        resp = await client.request(method, full_url, headers=headers)
        data = resp.json()
        if isinstance(data, dict) and data.get("code"):
            print(f"BINANCE ОШИБКА: {data['code']} - {data['msg']}")
        return data
    except Exception as e:
        print(f"Ошибка Binance: {e}")
        return {}

async def get_position(symbol: str):
    data = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol + "USDT"})
    for p in data if isinstance(data, list) else []:
        if p.get("symbol") == symbol + "USDT":
            return float(p.get("positionAmt", 0)), p.get("entryPrice", "0")
    return 0.0, "0"

async def close_position(symbol: str):
    amt, _ = await get_position(symbol)
    if abs(amt) < 0.001:
        return
    side = "SELL" if amt > 0 else "BUY"
    await binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol + "USDT",
        "side": side,
        "type": "MARKET",
        "quantity": f"{abs(amt):.6f}".rstrip("0").rstrip("."),
        "reduceOnly": "true",
        "positionSide": "BOTH"  # ← ФИКС -4061: указываем режим позиции
    })

async def open_long(symbol: str, usd: float = 10.0):
    await close_position(symbol)
    await binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol + "USDT",
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": usd,
        "positionSide": "BOTH"  # ← ФИКС -4061: для One-way mode
    })

async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"TG error: {e}")

@app.on_event("startup")
async def startup():
    await tg("<b>ТОРГОВЫЙ БОТ OZ 2026 ЗАПУЩЕН</b>\nГотов к бою на 10 USDT")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>ТЕРМИНАТОР 2026<br>ONLINE · ТОРГУЕТ</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)

    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    symbol = data.get("symbol", "").upper().replace("USDT", "").replace("/", "")
    direction = data.get("direction", "").upper()

    if not symbol or direction not in ["LONG", "CLOSE"]:
        return {"error": "bad data"}

    if direction == "LONG":
        await open_long(symbol, 10.0)
        await tg(f"<b>ОТКРЫЛ LONG {symbol}USDT</b>\nOZ SCANNER дал сигнал\n10 USDT в деле")
    else:
        await close_position(symbol)
        await tg(f"<b>ЗАКРЫЛ {symbol}USDT</b>\nOZ SCANNER дал сигнал")

    return {"status": "ok", "symbol": symbol, "action": direction}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
