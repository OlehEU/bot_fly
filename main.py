# main.py — ТЕРМИНАТОР 2026 | УЛЬТРА-МИНИМАЛ | 10$ НА СДЕЛКУ
import os
import time
import hmac
import hashlib
import urllib.parse
import httpx
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot

# ==================== ENV ====================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not all([TOKEN, CHAT_ID, BINANCE_KEY, BINANCE_SECRET, WEBHOOK_SECRET]):
    raise Exception("Не хватает переменных окружения!")

bot = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=10.0)
app = FastAPI()

# ==================== BINANCE ====================
def sign(params: dict):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    signature = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    return params

async def binance_request(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params = sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    if method == "POST":
        r = await client.post(url, headers=headers, params=params)
    else:
        r = await client.get(url, headers=headers, params=params)
    return r.json()

async def get_position(symbol: str):
    data = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol + "USDT"})
    for pos in data:
        if pos["symbol"] == symbol + "USDT":
            return float(pos["positionAmt"]), pos["entryPrice"]
    return 0.0, "0"

async def close_position(symbol: str):
    amt, _ = await get_position(symbol)
    if abs(amt) < 0.001): return
    side = "SELL" if amt > 0 else "BUY"
    await binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol + "USDT",
        "side": side,
        "type": "MARKET",
        "quantity": f"{abs(amt):.3f}".rstrip('0').rstrip('.'),
        "reduceOnly": "true"
    })

async def open_long(symbol: str, usdt_amount: float = 10.0):  # ← 10$ по умолчанию
    await close_position(symbol)  # на всякий случай чистим шорт
    await binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol + "USDT",
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": str(usdt_amount)
    })

# ==================== TG ====================
async def tg(text: str):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print("TG error:", e)

# ==================== ВЕБХУК ====================
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)

    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    symbol = data.get("symbol", "").replace("/USDT", "").replace("USDT", "").upper()
    direction = data.get("direction", "").upper()

    if not symbol or direction not in ["LONG", "CLOSE"]:
        raise HTTPException(400)

    if direction == "LONG":
        await open_long(symbol, usdt_amount=10.0)  # ← всегда 10$
        await tg(f"ОТКРЫЛ LONG {symbol}USDT\nOZ SCANNER дал сигнал!\nРазмер: 10 USDT")
    else:  # CLOSE
        await close_position(symbol)
        await tg(f"ЗАКРЫЛ позицию {symbol}USDT\nПо сигналу OZ SCANNER")

    return {"status": "ok", "symbol": symbol, "action": direction}

@app.get("/")
async def root():
    return {"status": "ТЕРМИНАТОР 2026 ЖИВ | 10$ на сделку"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
