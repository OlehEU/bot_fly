import os
import time
import hashlib
import hmac
import httpx
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot
from decimal import Decimal, ROUND_DOWN

# ====================== CONFIG ======================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY      = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET   = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "supersecret123")
FIXED_USDT       = float(os.getenv("FIXED_AMOUNT_USDT", "30"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20.0)
BASE = "https://fapi.binance.com"
active = {}
exchange_info = None

async def get_exchange_info():
    global exchange_info
    if exchange_info is None:
        r = await client.get(f"{BASE}/fapi/v1/exchangeInfo")
        exchange_info = r.json()
    return exchange_info

def truncate_quantity(symbol: str, quantity: float) -> str:
    info = next(s for s in exchange_info["symbols"] if s["symbol"] == symbol)
    lot_filter = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    step = Decimal(lot_filter["stepSize"])
    min_qty = Decimal(lot_filter["minQty"])
    qty = Decimal(quantity)
    qty = (qty // step) * step
    qty = max(qty, min_qty)
    qty = qty.quantize(Decimal("1."), rounding=ROUND_DOWN) if '.' in lot_filter["stepSize"] else qty.to_integral_value()
    return str(qty)

async def api(method: str, path: str, params: dict = None, signed: bool = True):
    url = BASE + path
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
        p["signature"] = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    r = await client.request(method, url, params=p, headers=headers)
    r.raise_for_status()
    return r.json()

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except: pass

async def get_price(symbol: str) -> float:
    data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

# ====================== LONG ======================
async def open_long(symbol: str):
    if symbol in active:
        await tg(f"<b>Уже открыт LONG</b> {symbol.replace('USDT','/USDT')}")
        return

    await get_exchange_info()

    try:
        await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
    except Exception as e:
        await tg(f"Плечо не поставилось: {str(e)[:50]}")

    price = await get_price(symbol)
    raw_qty = (FIXED_USDT * LEVERAGE) / price
    qty = truncate_quantity(symbol, raw_qty)

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    })

    active[symbol] = qty
    await tg(f"<b>LONG ОТКРЫТ ×{LEVERAGE}</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\n${FIXED_USDT} → {qty} монет\n≈ {price:.6f}")

# ====================== CLOSE ======================
async def close_position(symbol: str):
    if symbol not in active:
        return
    qty = active.pop(symbol)
    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    })
    await tg(f"<b>CLOSE {symbol.replace('USDT','/USDT')}</b>\nЗакрыто по рынку")

# ====================== API ======================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "БОТ РАБОТАЕТ", "active": list(active.keys())}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(400, "Bad JSON")

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(403, "Wrong secret")

    sym = data.get("symbol", "").replace("/", "").upper() + "USDT" if not data.get("symbol", "").upper().endswith("USDT") else data.get("symbol", "").upper()

    signal = data.get("signal", "").upper()

    if signal == "LONG":
        await open_long(sym)
    elif signal == "CLOSE":
        await close_position(sym)

    return {"status": "ok"}
