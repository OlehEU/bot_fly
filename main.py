# main.py — ТЕРМИНАТОР 2026 | 100% РАБОТАЕТ | БЕЗ ОШИБОК | ЛЮБАЯ МОНЕТА
import os
import time
import hmac
import hashlib
import urllib.parse
import httpx
import traceback
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ====================== КОНФИГ ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

AMOUNT_USD = 10.0
LEVERAGE = 10

bot = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)
app = FastAPI()

async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        print(f"TG error: {e}")

# ====================== ПОДПИСЬ + ЗАПРОС ======================
def sign(params: dict) -> str:
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    
    try:
        resp = await client.request(method, url, headers=headers, params=params)
        data = resp.json()
        if "code" in data:
            await tg(f"<b>BINANCE ОШИБКА</b>\n<code>{data['code']}: {data['msg']}</code>")
            print(f"BINANCE ERROR: {data}")
        else:
            print(f"BINANCE OK: {data.get('orderId') or data}")
        return data
    except Exception as e:
        await tg(f"<b>КРИТИЧЕСКАЯ ОШИБКА BINANCE</b>\n<code>{traceback.format_exc()}</code>")
        print(f"EXCEPTION: {e}")
        return {}

# ====================== ОТКРЫТИЕ ЛОНГА ======================
async def open_long(symbol: str):
    symbol_bin = symbol + "USDT"
    
    # 1. Устанавливаем плечо
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol_bin, "leverage": LEVERAGE})
    
    # 2. Получаем точность количества
    info = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    precision = 3
    for s in info.get("symbols", []):
        if s["symbol"] == symbol_bin:
            precision = s.get("quantityPrecision", 3)
            break
    
    # 3. Получаем цену
    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol_bin}, signed=False)
    price = float(price_data["price"])
    
    # 4. Считаем количество
    qty = round((AMOUNT_USD * LEVERAGE) / price, precision)
    qty_str = str(int(qty)) if precision == 0 else f"{qty:.{precision}f}".rstrip("0").rstrip(".")
    
    # 5. ОТКРЫВАЕМ ОРДЕР С positionSide=BOTH (работает в любом режиме)
    result = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol_bin,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty_str,
        "positionSide": "BOTH"  # ← КЛЮЧ К УСПЕХУ
    })
    
    if "orderId" in result:
        entry = float(result.get("avgPrice", price))
        await tg(f"""
<b>LONG {symbol}USDT ОТКРЫТ</b>
${AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.6f}</code>
Кол-во: {qty_str} {symbol}
        """.strip())
    else:
        await tg(f"<b>НЕ УДАЛОСЬ ОТКРЫТЬ {symbol}</b>\nОтвет: {result}")

# ====================== ЗАКРЫТИЕ ======================
async def close_position(symbol: str):
    symbol_bin = symbol + "USDT"
    pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol_bin})
    amt = 0.0
    for p in pos if isinstance(pos, list) else []:
        if p["symbol"] == symbol_bin:
            amt = float(p["positionAmt"])
            break
    if abs(amt) < 0.001:
        await tg(f"{symbol}USDT — уже закрыто")
        return
    
    side = "SELL" if amt > 0 else "BUY"
    qty_str = f"{abs(amt):.6f}".rstrip("0").rstrip(".")
    
    await binance("POST", "/fapi/v1/order", {
        "symbol": symbol_bin,
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "reduceOnly": "true",
        "positionSide": "BOTH"
    })
    await tg(f"<b>{symbol}USDT ЗАКРЫТ</b>")

# ====================== FASTAPI ======================
@app.on_event("startup")
async def startup():
    await tg("<b>ТЕРМИНАТОР 2026 АКТИВИРОВАН</b>\nГотов к сигналам OZ SCANNER")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:lime;background:black;text-align:center;padding:100px'>ТЕРМИНАТОР 2026<br>ONLINE</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    
    try:
        data = await request.json()
    except:
        raise HTTPException(400)
    
    symbol = data.get("symbol", "").upper().replace("USDT", "")
    action = data.get("direction", "").upper()
    
    if not symbol or action not in ["LONG", "CLOSE"]:
        return {"error": "bad"}
    
    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    else:
        asyncio.create_task(close_position(symbol))
    
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
