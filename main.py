# main.py — TERMINATOR 2026 HEDGE MODE — НАДЕЖНЫЙ XRP BOT

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
TP_PERCENT      = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT      = float(os.getenv("SL_PERCENT", "1.0"))
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
def create_signature(params: dict) -> str:
    # параметры в виде списка кортежей
    p = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        p.append((k, str(v)))

    # timestamp
    p.append(("timestamp", str(int(time.time() * 1000))))

    # сортировка
    p_sorted = sorted(p, key=lambda x: x[0])

    # query string
    query_string = "&".join(f"{k}={v}" for k, v in p_sorted)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

    # добавить signature в конец
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
async def open_long():
    global position_active
    if position_active:
        await tg_send("Позиция уже открыта!")
        return
    try:
        qty = await get_qty()
        oid = f"xrp_{int(time.time()*1000)}"
        entry = await get_price()
        tp = round(entry * (1 + TP_PERCENT/100), 4)
        sl = round(entry * (1 - SL_PERCENT/100), 4)

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

        # TP / SL
        for price, tp_type in [(tp, "TAKE_PROFIT_MARKET"), (sl, "STOP_MARKET")]:
            tp_params = {
                "symbol": SYMBOL_BINANCE,
                "side": "SELL",
                "type": tp_type,
                "quantity": str(qty),
                "positionSide": "LONG",
                "reduceOnly": "true",
                "stopPrice": str(price),
                "newClientOrderId": f"{tp_type.lower()}_{oid}"
            }
            await binance_request("POST", "/fapi/v1/order", tp_params)

        await tg_send(f"""
<b>LONG OPENED</b>
Entry: <code>{entry:.4f}</code>
TP: <code>{tp:.4f}</code>
SL: <code>{sl:.4f}</code>
Qty: {qty}
        """.strip())

    except Exception:
        await tg_send(f"<b>OPEN LONG ERROR</b>\n<code>{traceback.format_exc()}</code>")
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
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    data = await request.json()
    if data.get("signal") == "obuy":  # сигнал BUY
        await tg_send("Signal BUY — opening LONG")
        asyncio.create_task(open_long())
    return {"ok": True}
