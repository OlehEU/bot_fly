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
active = set()

def sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def api(method: str, path: str, params: dict = None, signed: bool = True):
    url = BASE + path
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    r = await client.request(method, url, params=p, headers=headers)
    if r.status_code >= 400:
        err = r.json()
        await tg(f"<b>BINANCE ОШИБКА</b>\n<code>{err.get('msg', r.text)}</code>")
        return None
    return r.json()

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except:
        pass

async def get_price(symbol: str) -> float:
    data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"]) if data else 0.0

async def open_long(symbol: str):
    if symbol in active:
        await tg(f"<b>Уже открыт</b> {symbol.replace('USDT','/USDT')}")
        return

    price = await get_price(symbol)
    if price == 0:
        return

    # Принудительно Cross + плечо
    await api("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})

    raw_qty = (FIXED_USDT * LEVERAGE) / price

    # Точная обрезка количества
    if symbol in ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT","XRPUSDT","ADAUSDT","SOLUSDT","1000SATSUSDT"]:
        qty = str(int(raw_qty))
    else:
        qty = f"{raw_qty:.3f}".rstrip("0").rstrip(".")

    if float(qty) < 0.001:
        qty = "0.001"

    result = await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    })

    if result:
        active.add(symbol)
        await tg(f"<b>LONG ОТКРЫТ ×{LEVERAGE} (Cross)</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\n{qty} монет @{price:.6f}")

async def close_position(symbol: str):
    if symbol not in active:
        return

    # Берём актуальное количество из позиции
    pos = await api("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    qty = None
    if pos:
        for p in pos:
            if p["symbol"] == symbol and float(p["positionAmt"]) > 0:
                qty = p["positionAmt"]
                break

    if not qty:
        active.discard(symbol)
        return

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    })

    active.discard(symbol)
    await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}")

# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "РАБОТАЕТ В CROSS", "active": list(active)}

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
    elif signal in ["CLOSE", "CLOSE_ALL"]:
        await close_position(sym)

    return {"ok": True}
