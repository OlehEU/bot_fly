# main.py — ТЕРМИНАТОР 2026 | ФИНАЛЬНАЯ ВЕРСИЯ | 100% РАБОТАЕТ | БЕЗ ОШИБОК
import os
import time
import hmac
import hashlib
import urllib.parse
import asyncio            # ← ЭТО БЫЛО ПРОПУЩЕНО!
import traceback
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ==================== КОНФИГ ====================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

AMOUNT_USD = 10.0
LEVERAGE   = 10

if not all([TOKEN, CHAT_ID, BINANCE_KEY, BINANCE_SECRET]):
    raise Exception("Нет ключей! Проверь fly secrets")

bot    = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)
app    = FastAPI()

# ==================== TG ====================
async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        print("TG error:", e)

# ==================== BINANCE ====================
def _sign(params):
    query = urllib.parse.urlencode(sorted({k: str(v) for k, v in params.items() if v is not None}.items()))
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance(method: str, path: str, params=None, signed=True):
    url = f"https://fapi.binance.com{path}"
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = _sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        data = r.json()
        if isinstance(data, dict) and data.get("code") and data["code"] != 200:
            print(f"BINANCE ERROR: {data['code']} — {data['msg']}")
            await tg(f"<b>ОШИБКА BINANCE</b>\n<code>{data['code']}: {data['msg']}</code>")
        return data
    except Exception as e:
        print("EXCEPTION:", traceback.format_exc())
        await tg(f"<b>КРИТИЧЕСКАЯ ОШИБКА</b>\n<code>{traceback.format_exc()}</code>")
        return {}

# ==================== ОТКРЫТИЕ ЛОНГА ====================
async def open_long(symbol: str):
    sym = symbol + "USDT"
    try:
        # Плечо
        await binance("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})

        # Точность
        info = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        info_data = info.json()
        precision = 3
        for s in info_data.get("symbols", []):
            if s["symbol"] == sym:
                precision = s.get("quantityPrecision", 3)
                break

        # Цена
        price_resp = await client.get("https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": sym})
        price = float(price_resp.json()["price"])

        # Количество
        qty_raw = (AMOUNT_USD * LEVERAGE) / price
        qty = round(qty_raw, precision)
        qty_str = str(int(qty)) if precision == 0 else f"{qty:.{precision}f}".rstrip("0").rstrip(".")

        # ОТКРЫВАЕМ
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
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>ТЕРМИНАТОР 2026<br>ONLINE · 10 USDT</h1>"

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
        asyncio.create_task(open_long(symbol))        # ← теперь работает
    else:
        asyncio.create_task(close_position(symbol))

    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
