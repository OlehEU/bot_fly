# main.py — TERMINATOR 2026 (HEDGE MODE, FIXED)

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

# ====================== CONFIG ==========================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

AMOUNT_USD = 10.0
LEVERAGE   = 10

bot    = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)
app    = FastAPI()


# ================== TELEGRAM SENDER =====================
async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception:
        print("TG ERROR:", traceback.format_exc())


# ================= SIGNATURE (FIXED) =====================
# ✔ параметры сортируются — подпись всегда корректна
def create_signature(params: dict) -> str:
    filtered = [(k, str(v)) for k, v in params.items() if v is not None]
    filtered.sort(key=lambda x: x[0])  # сортировка обязательна
    query = urllib.parse.urlencode(filtered)
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


# ================== BINANCE REQUEST ======================
async def binance(method: str, endpoint: str, params: dict = None):
    p = params or {}
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = create_signature(p)

    url = f"https://fapi.binance.com{endpoint}"
    headers = {"X-MBX-APIKEY": BINANCE_KEY}

    try:
        r = await client.request(method, url, params=p, headers=headers)
        data = r.json()

        if isinstance(data, dict) and "code" in data and data["code"] != 0:
            await tg(f"<b>BINANCE ERROR</b>\n<code>{data['code']}: {data.get('msg')}</code>")
        return data

    except Exception:
        await tg(f"<b>CRITICAL ERROR</b>\n<code>{traceback.format_exc()}</code>")
        return {"error": True}


# ================ GET PRECISION SAFE =====================
async def get_precision(symbol: str):
    try:
        r = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        data = r.json()["symbols"]
        for s in data:
            if s["symbol"] == symbol:
                return s["quantityPrecision"]
    except:
        pass
    return 3  # fallback


# ================== OPEN LONG ============================
async def open_long(symbol: str):
    sym = symbol + "USDT"

    try:
        # ---- leverage ----
        await binance("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})

        # ---- precision ----
        precision = await get_precision(sym)

        # ---- price ----
        price_data = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}")
        price = float(price_data.json()["price"])

        # ---- quantity ----
        qty_raw = (AMOUNT_USD * LEVERAGE) / price
        qty = round(qty_raw, precision)

        if precision == 0:
            qty_str = str(int(qty))
        else:
            qty_str = f"{qty:.{precision}f}".rstrip("0").rstrip(".")

        # ---- OPEN LONG (HEDGE MODE) ----
        order = await binance("POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty_str,
            "positionSide": "LONG"
        })

        if order.get("orderId"):
            await tg(
                f"<b>OPENED LONG {sym}</b>\n"
                f"${AMOUNT_USD} × {LEVERAGE}x\n"
                f"Qty: <code>{qty_str}</code>\n"
                f"Price: <code>{price:.6f}</code>"
            )
        else:
            await tg(f"<b>FAILED OPEN LONG {sym}</b>\n{order}")

    except Exception:
        await tg(f"<b>OPEN LONG ERROR</b>\n<code>{traceback.format_exc()}</code>")


# ================== CLOSE LONG ===========================
async def close_position(symbol: str):
    sym = symbol + "USDT"

    try:
        positions = await binance("GET", "/fapi/v2/positionRisk", {"symbol": sym})
        amt = 0.0

        if isinstance(positions, list):
            for p in positions:
                if p["symbol"] == sym and p["positionSide"] == "LONG":
                    amt = float(p["positionAmt"])
                    break

        if abs(amt) < 0.0001:
            await tg(f"{sym} LONG already closed")
            return

        qty_str = f"{abs(amt):.6f}".rstrip("0").rstrip(".")

        await binance("POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_str,
            "reduceOnly": "true",
            "positionSide": "LONG"
        })

        await tg(f"<b>CLOSED LONG {sym}</b>")

    except Exception:
        await tg(f"<b>CLOSE ERROR</b>\n<code>{traceback.format_exc()}</code>")


# ==================== FASTAPI ============================
@app.on_event("startup")
async def startup():
    await tg("<b>TERMINATOR 2026 STARTED</b>\nHEDGE MODE ACTIVE")


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:60px'>TERMINATOR 2026<br>HEDGE MODE ONLINE</h1>"


@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)

    data = await request.json()

    symbol = data.get("symbol", "").upper().replace("USDT", "")
    action = data.get("direction", "").upper()

    if not symbol or action not in ("LONG", "CLOSE"):
        return {"error": "bad input"}

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    else:
        asyncio.create_task(close_position(symbol))

    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
