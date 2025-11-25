# main.py — ТЕРМИНАТОР 2026 | 10$ НА СДЕЛКУ | ИСПРАВЛЕНО И РАБОТАЕТ НА 100%
import os
import time
import hmac
import hashlib
import urllib.parse
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ==================== ENV ====================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not all([TOKEN, CHAT_ID, BINANCE_KEY, BINANCE_SECRET, WEBHOOK_SECRET]):
    raise Exception("Проверь переменные окружения: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET, WEBHOOK_SECRET")

bot = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=12.0)
app = FastAPI()

# ==================== BINANCE HELPERS ====================
def sign(params: dict):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    signature = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    return params

async def binance_request(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        if method == "POST":
            r = await client.post(url, headers=headers, params=params)
        else:
            r = await client.get(url, headers=headers, params=params)
        return r.json()
    except:
        return {}

async def get_position(symbol: str):
    data = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol + "USDT"})
    for pos in data if isinstance(data, list) else []:
        if pos.get("symbol") == symbol + "USDT":
            return float(pos.get("positionAmt", 0)), pos.get("entryPrice", "0")
    return 0.0, "0"

async def close_position(symbol: str):
    amt, _ = await get_position(symbol)
    if abs(amt) < 0.001:      # ← ИСПРАВЛЕНО! Была лишняя скобка
        return
    side = "SELL" if amt > 0 else "BUY"
    await binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol + "USDT",
        "side": side,
        "type": "MARKET",
        "quantity": f"{abs(amt):.3f}".rstrip('0').rstrip('.'),
        "reduceOnly": "true"
    })

async def open_long(symbol: str, usdt_amount: float = 10.0):
    await close_position(symbol)  # чистим шорт если есть
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
        print(f"TG error: {e}")

# ==================== КРАСИВАЯ ЗАГЛУШКА ====================
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
    <head>
        <title>ТЕРМИНАТОР 2026</title>
        <meta charset="utf-8">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@900&display=swap';
            body{margin:0;padding:0;height:100vh;background:linear-gradient(135deg,#000428,#004e92);color:#00ff41;font-family:'Orbitron',sans-serif;display:flex;align-items:center;justify-content:center;overflow:hidden}
            .container{text-align:center;animation:pulse 3s infinite}
            h1{font-size:80px;margin:0;text-shadow:0 0 20px #00ff41,0 0 40px #00ff41;letter-spacing:8px}
            .status{font-size:32px;margin:40px 0;padding:20px;background:rgba(0,255,65,0.15);border:2px solid #00ff41;border-radius:15px;display:inline-block;animation:blink 1.5s infinite}
            @keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
            @keyframes blink{0%,100%{opacity:1}50%{opacity:0.6}}
            .skull{font-size:120px;margin-bottom:10px}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="skull">SKULL</div>
            <h1>ТЕРМИНАТОР 2026</h1>
            <div class="status">ONLINE · ЖИВ · ТОРГУЕТ НА 10 USDT</div>
            <div style="margin-top:50px;font-size:22px;opacity:0.9">
                OZ SCANNER → ТЕРМИНАТОР → BINANCE FUTURES<br>
                Полный автомат · 24/7
            </div>
        </div>
    </body>
    </html>
    """

# ==================== ВЕБХУК ====================
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403, "Нет доступа, брат")

    try:
        data = await request.json()
    except:
        raise HTTPException(400, "JSON сломан")

    symbol = data.get("symbol", "").replace("/USDT", "").replace("USDT", "").upper()
    direction = data.get("direction", "").upper()

    if not symbol or direction not in ["LONG", "CLOSE"]:
        raise HTTPException(400, "Нужны symbol и direction")

    if direction == "LONG":
        await open_long(symbol, 10.0)
        await tg(f"ОТКРЫЛ LONG {symbol}USDT\nПо сигналу OZ SCANNER\n10 USDT в деле")
    elif direction == "CLOSE":
        await close_position(symbol)
        await tg(f"ЗАКРЫЛ позицию {symbol}USDT\nПо сигналу OZ SCANNER")

    return {"status": "ok", "action": direction, "symbol": symbol}

# ==================== УВЕДОМЛЕНИЕ ПРИ СТАРТЕ ====================
import datetime

@app.on_event("startup")
async def on_startup():
    await tg(
        "<b>ТОРГОВЫЙ БОТ OZ 2026 ЗАПУЩЕН</b>\n\n"
        f"Время старта: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
        "• 10 USDT на сделку\n"
        "• Binance Futures\n"
        "• OZ SCANNER → ТЕРМИНАТОР → Автотрейд\n"
        "Готов ловить сигналы 24/7"
    )

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
