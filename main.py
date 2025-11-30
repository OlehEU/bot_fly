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
client = httpx.AsyncClient(timeout=30.0)
BASE = "https://fapi.binance.com"
active = set()

async def tg(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except:
        pass

def make_signature(query_string: str) -> str:
    return hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

async def api(method: str, path: str, params: dict | None = None, signed: bool = False):
    url = BASE + path
    p = params.copy() if params else {} if signed else (params or {})

    if signed:
        p["timestamp"] = int(time.time() * 1000)
        query_string = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
        p["signature"] = make_signature(query_string)
    else:
        query_string = None

    headers = {"X-MBX-APIKEY": BINANCE_KEY}

    try:
        resp = await client.request(method, url, params=p, headers=headers)
        if resp.status_code >= 400:
            err = resp.json() if resp.headers.get("content-type") == "application/json" else {"msg": resp.text}
            await tg(f"<b>BINANCE ОШИБКА</b>\n<code>{err.get('msg', resp.text)[:500]}</code>")
            return None
        return resp.json()
    except Exception as e:
        await tg(f"<b>КРИТ ОШИБКА</b>\n{str(e)}")
        return None

async def get_price(symbol: str) -> float:
    data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"]) if data else 0.0

async def open_long(symbol: str):
    if symbol in active:
        await tg(f"<b>Уже есть LONG</b> {symbol.replace('USDT','/USDT')}")
        return

    price = await get_price(symbol)
    if price == 0:
        return

    # Cross + плечо (подписанные запросы)
    await api("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"}, signed=True)
    await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE}, signed=True)

    raw_qty = (FIXED_USDT * LEVERAGE) / price

    if symbol in ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT"]:
        qty = str(int(raw_qty))
    else:
        qty = f"{raw_qty:.3f}".rstrip("0").rstrip(".")

    result = await api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    }, signed=True)

    if result:
        active.add(symbol)
        await tg(f"<b>LONG ×{LEVERAGE} ОТКРЫТ (Cross)</b>\n<code>{symbol.replace('USDT','/USDT')}</code>\n{qty} монет\n≈ {price:.6f}")

async def close_position(symbol: str):
    if symbol not in active:
        return

    pos = await api("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    qty = None
    if pos:
        for p in pos:
            if p["symbol"] == symbol and float(p["positionAmt"]) > 0:
                qty = str(abs(float(p["positionAmt"])))
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
    }, signed=True)

    active.discard(symbol)
    await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}")

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "OZ BOT 100% ЖИВ", "active": list(active)}

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
