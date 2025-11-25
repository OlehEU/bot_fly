# main.py — ТЕРМИНАТОР 2026 | ФИНАЛЬНАЯ ВЕРСИЯ | ОТКРЫВАЕТ С 1 КЛИКА | БЕЗ -1022
import os
import time
import hmac
import hashlib
import urllib.parse
import asyncio
import traceback
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ==================== КОНФИГ ====================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY     = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

AMOUNT_USD = 10.0
LEVERAGE   = 10

bot    = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)
app    = FastAPI()

async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        print("TG error:", e)

# ==================== РАБОЧАЯ ПОДПИСЬ ИЗ ТВОЕГО КОДА ====================
def create_signature(params: dict) -> str:
    normalized = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            normalized[k] = str(v).lower()
        elif isinstance(v, (int, float)):
            normalized[k] = str(v)
        else:
            normalized[k] = str(v)
    query_string = urllib.parse.urlencode(normalized)
    return hmac.new(BINANCE_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance(method: str, endpoint: str, params: dict = None):
    url = f"https://fapi.binance.com{endpoint}"
    p = params or {}
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = create_signature(p)          # ← ТВОЯ РАБОЧАЯ ПОДПИСЬ!
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        resp = await client.request(method, url, headers=headers, params=p)
        data = resp.json()
        if isinstance(data, dict) and data.get("code"):
            await tg(f"<b>BINANCE ОШИБКА</b>\n<code>{data['code']}: {data['msg']}</code>")
            print(f"ERROR: {data}")
        return data
    except Exception as e:
        await tg(f"<b>КРИТИЧЕСКАЯ ОШИБКА</b>\n<code>{traceback.format_exc()}</code>")
        return {}

# ==================== ОТКРЫТИЕ ЛОНГА ====================
async def open_long(symbol: str):
    sym = symbol + "USDT"
    try:
        # Плечо
        await binance("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})

        # Точность количества
        info = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        info_data = info.json()
        precision = 3
        for s in info_data.get("symbols", []):
            if s["symbol"] == sym:
                precision = s.get("quantityPrecision", 3)
                break

        # Цена
        price_resp = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}")
        price = float(price_resp.json()["price"])

        # Количество
        qty_raw = (AMOUNT_USD * LEVERAGE) / price
        qty = round(qty_raw, precision)
        qty_str = str(int(qty)) if precision == 0 else f"{qty:.{precision}f}".rstrip("0").rstrip(".")

        # ОТКРЫВАЕМ ОРДЕР
        order = await binance("POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty_str,
            "positionSide": "BOTH"
        })

        if order.get("orderId"):
            entry = float(order.get("avgPrice") or price)
            await tg(f"<b>LONG {symbol}USDT ОТКРЫТ</b>\n"
                     f"${AMOUNT_USD} × {LEVERAGE}x\n"
                     f"Entry: <code>{entry:.6f}</code>\n"
                     f"Кол-во: {qty_str}")
        else:
            await tg(f"<b>НЕ ОТКРЫЛ {symbol}</b>\n{order}")

    except Exception as e:
        await tg(f"<b>ОШИБКА {symbol}</b>\n<code>{traceback.format_exc()}</code>")

# ==================== ЗАКРЫТИЕ ====================
async def close_position(symbol: str):
    sym = symbol + "USDT"
    pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": sym})
    amt = next((float(p["positionAmt"]) for p in (pos if isinstance(pos, list) else []) if p["symbol"] == sym), 0)
    if abs(amt) < 0.001:
        await tg(f"{symbol}USDT уже закрыт")
        return
    side = "SELL" if amt > 0 else "BUY"
    qty_str = f"{abs(amt):.6f}".rstrip("0").rstrip(".")
    await binance("POST", "/fapi/v1/order", {
        "symbol": sym,
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "reduceOnly": "true",
        "positionSide": "BOTH"
    })
    await tg(f"<b>{symbol}USDT ЗАКРЫТ</b>")

# ==================== FASTAPI ====================
@app.on_event("startup")
async def startup():
    await tg("<b>ТЕРМИНАТОР 2026 ЗАПУЩЕН</b>\nГотов к сигналам OZ SCANNER")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px'>ТЕРМИНАТОР 2026<br>ONLINE</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    data = await request.json()
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
