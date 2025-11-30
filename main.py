import os
import time
import hashlib
import hmac
import httpx
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot

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
exchange_info = None  # кэшируем один раз

async def get_exchange_info():
    global exchange_info
    if exchange_info is None:
        exchange_info = await client.get(f"{BASE}/fapi/v1/exchangeInfo")
        exchange_info = exchange_info.json()
    return exchange_info

def get_quantity_precision(symbol: str, qty: float) -> str:
    info = next((s for s in exchange_info["symbols"] if s["symbol"] == symbol), None)
    if not info:
        return f"{qty:.3f}"
    precision = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"]
    precision = len(precision.rstrip('0').split('.')[-1]) if '.' in precision else 0
    return f"{qty:.{precision}f}".rstrip('0').rstrip('.') if '.' in f"{qty:.{precision}f}" else f"{int(qty)}"

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
        await tg(f"Уже есть LONG по {symbol.replace('USDT','/USDT')}")
        return

    await get_exchange_info()  # один раз загрузим

    try:
        await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
    except: pass

    price = await get_price(symbol)
    raw_qty = (FIXED_USDT * LEVERAGE) / price
    qty_str = get_quantity_precision(symbol, raw_qty)

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty_str
    })

    active[symbol] = qty_str
    await tg(f"<b>LONG ОТКРЫТ ×{LEVERAGE}</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\n${FIXED_USDT} → {qty_str} монет\nЦена: {price:.6f}")

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
    return {"status": "OZ BOT 100% РАБОТАЕТ", "positions": list(active.keys())}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(403)

    sym = data.get("symbol", "").replace("/", "").upper()
    if not sym.endswith("USDT"):
        sym += "USDT"

    signal = data.get("signal", "").upper()

    if signal == "LONG":
        await open_long(sym)
    elif signal == "CLOSE":
        await close_position(sym)

    return {"status": "ok"}
