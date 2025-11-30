import os
import time
import hmac
import hashlib
from typing import Dict
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ==================== CONFIG ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        raise EnvironmentError(f"Нет переменной {v}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20)
BASE = "https://fapi.binance.com"
active = set()

# ================= TELEGRAM =====================
async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except:
        pass

# ================= SIGNATURE =====================
def make_signature(params: Dict) -> str:
    clean = {k: v for k, v in params.items() if k != "signature"}
    query = "&".join(f"{k}={v}" for k, v in sorted(clean.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ================= BINANCE API ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    url = BASE + path
    p = params.copy() if params else {}

    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000
        
        # КЛЮЧЕВАЯ ПРАВКА — подпись считается ДО добавления самой подписи!
        signature = make_signature(p)   # ← вот здесь всё правильно
        p["signature"] = signature      # ← а только потом добавляем

    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        if r.status_code != 200:
            err = r.text if len(r.text) < 3800 else r.text[:3800] + "..."
            await tg(f"<b>BINANCE ERROR {r.status_code} {path}</b>\n<code>{err}</code>")
            return None
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ QTY ROUND =======================
def fix_qty(symbol: str, qty: float) -> str:
    high_prec = ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT","1000SATSUSDT"]
    if symbol in high_prec:
        return str(int(qty))
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ OPEN LONG =======================
async def open_long(sym: str):
    symbol = sym.upper().replace("/", "") + "USDT" if not sym.upper().endswith("USDT") else sym.upper()

    if symbol in active:
        await tg(f"<b>{symbol}</b> — уже открыта")
        return

    # 1. Cross Margin
    if not await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"}):
        await tg(f"<b>Не удалось установить CROSS для {symbol}</b>")
        return

    # 2. Leverage
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})

    # 3. Price + Qty
    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data:
        return
    price = float(price_data["price"])
    qty = fix_qty(symbol, AMOUNT * LEV / price)

    # 4. Open LONG
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty
    })

    if order and order.get("orderId"):
        active.add(symbol)
        await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty} шт ≈ ${AMOUNT}\n@ {price:.8f}")
    else:
        await tg(f"<b>Ошибка открытия {symbol}</b>")

# ================= CLOSE ==========================
async def close(sym: str):
    symbol = sym.upper().replace("/", "") + "USDT" if not sym.upper().endswith("USDT") else sym.upper()
    if symbol not in active:
        return

    pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos:
        return

    qty = next((p["positionAmt"] for p in pos if p["positionSide"] == "LONG" and float(p["positionAmt"]) > 0), None)
    if not qty:
        active.discard(symbol)
        return

    await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    })
    active.discard(symbol)
    await tg(f"<b>CLOSE {symbol}</b>\n{qty} шт")

# ================= FASTAPI =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg("<b>OZ BOT 2025 — ONLINE</b>\nCross Mode | Hedge Mode FIXED\nОшибки Binance → полные")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    
    data = await request.json()
    symbol = data.get("symbol", "").upper()
    signal = data.get("signal", "").upper()

    if signal == "LONG":
        asyncio.create_task(open_long(symbol))
    elif signal == "CLOSE":
        asyncio.create_task(close(symbol))

    return {"ok": True}
