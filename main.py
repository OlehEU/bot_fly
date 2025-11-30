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
active = set()  # просто множество символов

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
        await tg(f"<b>Binance error</b>\n<code>{err.get('msg','Unknown error')}</code>")
        return None
    return r.json()

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except: pass

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

    # 1. Принудительно ставим Cross margin + нужное плечо
    await api("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})

    raw_qty = (FIXED_USDT * LEVERAGE) / price

    # 2. Правильная точность количества
    if symbol in ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT","XRPUSDT","ADAUSDT"]:
        qty = str(int(raw_qty))                     # целые монеты
    else:
        qty = f"{raw_qty:.3f}".rstrip("0").rstrip(".")  # обычные

    if float(qty) < 0.001:
        qty = "0.001"

    # 3. Открываем
    result = await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    })

    if result:
        active.add(symbol)
        await tg(f"<b>LONG ОТКРЫТ ×{LEVERAGE} (Cross)</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\n{qty} монет\n≈ {price:.6f}")

async def close_position(symbol: str):
    if symbol not in active:
        return
    # Находим количество из открытых позиций (на случай если упало)
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
        "type": "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    })
    active.discard(symbol)
    await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}")

# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "БОТ ЖИВ И В CROSS", "active": list(active)}

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
    elif signal == "CLOSE" or signal == "CLOSE_ALL":
        await close_position(sym)

    return {"ok": True}
