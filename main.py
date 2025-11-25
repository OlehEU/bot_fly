# main.py — TERMINATOR 2026 HEDGE MODE — XRP BOT (без TP/SL)

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
from contextlib import asynccontextmanager

# ====================== CONFIG ==========================
TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE        = int(os.getenv("LEVERAGE", "10"))
BASE_COIN       = "XRP"
SYMBOL_BINANCE  = f"{BASE_COIN}USDT"

bot = Bot(token=TOKEN)
client = httpx.AsyncClient(timeout=20.0)

# ================= TELEGRAM =================
async def tg_send(msg: str):
    try:
        await bot.send_message(CHAT_ID, msg, parse_mode="HTML")
    except Exception as e:
        print("Telegram error:", e)

# ================= BINANCE REQUEST =================
def create_signature(params: dict) -> list:
    p = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        p.append((k, str(v)))

    p.append(("timestamp", str(int(time.time() * 1000))))
    p_sorted = sorted(p, key=lambda x: x[0])
    query_string = "&".join(f"{k}={v}" for k, v in p_sorted)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    p_sorted.append(("signature", signature))
    return p_sorted

async def binance_request(method: str, endpoint: str, params: dict = None) -> dict:
    url = f"https://fapi.binance.com{endpoint}"
    signed_params = create_signature(params or {})
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    try:
        if method == "GET":
            r = await client.get(url, params=signed_params, headers=headers)
        else:
            r = await client.post(url, params=signed_params, headers=headers)
        data = r.json()
        if isinstance(data, dict) and "code" in data and data["code"] != 0:
            await tg_send(f"<b>BINANCE ERROR</b>\n<code>{data['code']}: {data['msg']}</code>")
        return data
    except Exception:
        await tg_send(f"<b>BINANCE CRITICAL ERROR</b>\n<code>{traceback.format_exc()}</code>")
        return {}

# ================== PRELOAD =================
MARKET = {"limits": {"amount": {"min": 0}}}
QTY_PRECISION = 3
position_active = False

async def preload():
    global MARKET, QTY_PRECISION
    try:
        info = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        info_json = info.json()
        for s in info_json.get("symbols", []):
            if s["symbol"] == SYMBOL_BINANCE:
                QTY_PRECISION = s.get("quantityPrecision", 3)
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        MARKET["limits"]["amount"]["min"] = float(f.get("minQty", 0))
                break
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL_BINANCE, "leverage": LEVERAGE})
        await tg_send(f"<b>PRELOAD DONE</b>\nSymbol: {SYMBOL_BINANCE}, Precision: {QTY_PRECISION}, MinQty: {MARKET['limits']['amount']['min']}")
    except Exception as e:
        await tg_send(f"<b>PRELOAD ERROR</b>\n<code>{traceback.format_exc()}</code>")

# ================== GET PRICE / QTY =================
async def get_price() -> float:
    data = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL_BINANCE}")
    return float(data.json().get("price", 0))

async def get_qty() -> float:
    price = await get_price()
    qty_raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(qty_raw, QTY_PRECISION)
    min_qty = MARKET['limits']['amount']['min']
    return max(qty, min_qty)

# ================== OPEN LONG =================
async def open_long(symbol_override=None):
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return
    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"
        entry = await get_price()

        # --- Создаём MARKET ордер на LONG без TP/SL ---
        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(qty),
            "positionSide": "LONG",
            "newClientOrderId": oid
        }

        response = await binance_request("POST", "/fapi/v1/order", params)
        if not response.get("orderId"):
            raise Exception(f"Order failed: {response}")

        position_active = True

        await tg_send(f"""
<b>LONG OPENED</b>
Entry: <code>{entry:.4f}</code>
Qty: {qty}
        """.strip())

    except Exception:
        await tg_send(f"<b>OPEN LONG ERROR</b>\n<code>{traceback.format_exc()}</code>")
        position_active = False

# ================== CLOSE POSITION =================
async def close_position(symbol_override=None):
    global position_active
    try:
        positions = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL_BINANCE})
        amt = 0.0
        for p in positions if isinstance(positions, list) else []:
            if p.get("symbol") == SYMBOL_BINANCE and p.get("positionSide") == "LONG":
                amt = float(p.get("positionAmt", 0))
                break

        if abs(amt) < 0.0001:
            await tg_send(f"{SYMBOL_BINANCE} LONG already closed")
            position_active = False
            return

        qty_str = f"{abs(amt):.{QTY_PRECISION}f}".rstrip("0").rstrip(".")
        if float(qty_str) < MARKET["limits"]["amount"]["min"]:
            qty_str = str(MARKET["limits"]["amount"]["min"])

        params = {
            "symbol": SYMBOL_BINANCE,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_str,
            "reduceOnly": "true",
            "positionSide": "LONG"
        }

        response = await binance_request("POST", "/fapi/v1/order", params)
        if not response.get("orderId"):
            raise Exception(f"Close order failed: {response}")

        await tg_send(f"<b>{SYMBOL_BINANCE} LONG CLOSED</b>")
        position_active = False

    except Exception:
        await tg_send(f"<b>CLOSE ERROR</b>\n<code>{traceback.format_exc()}</code>")
        position_active = False

# ================== LIFESPAN =================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send("Bot starting... preload Binance data")
    await preload()
    await tg_send(f"<b>Bot ready!</b>\n{SYMBOL_BINANCE} | ${FIXED_AMOUNT_USD} × {LEVERAGE}x")
    yield
    await client.aclose()

# ================== FASTAPI =================
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>XRP HEDGE BOT ONLINE</h1>")

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

# ================== RUN =================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
