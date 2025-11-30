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

# Синхронизация времени с Binance (очень помогает от -1022)
TIME_OFFSET = 0

async def sync_time():
    global TIME_OFFSET
    try:
        r = await client.get(f"{BASE}/fapi/v1/time")
        server_time = r.json()["serverTime"]
        TIME_OFFSET = server_time - int(time.time() * 1000)
    except:
        pass  # если не получилось — будет работать и без этого

# ================= TELEGRAM =====================
async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except:
        pass

# ================= SIGNATURE =====================
def make_signature(params: Dict) -> str:
    # Только те параметры, которые реально отправляются (без signature)
    clean_params = {k: v for k, v in params.items() if k != "signature"}
    query = "&".join(f"{k}={v}" for k, v in sorted(clean_params.items()))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ================= BINANCE API ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    url = BASE + path
    p = params.copy() if params else {}

    if signed:
        p["timestamp"] = int(time.time() * 1000) + TIME_OFFSET
        p["recvWindow"] = 60000  # увеличил до 60 сек — надёжнее

        # ВАЖНО: считаем подпись ДО добавления самой подписи в словарь
        p["signature"] = make_signature(p)

    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        if r.status_code != 200:
            full_error = r.text
            if len(full_error) > 3800:
                full_error = full_error[:3800] + "...(обрезано)"
            await tg(f"<b>BINANCE ERROR {r.status_code} {path}</b>\n<code>{full_error}</code>")
            return None
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ QTY ROUND =======================
def fix_qty(symbol: str, qty: float) -> str:
    high_precision = ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "1000PEPEUSDT", "BONKUSDT", "FLOKIUSDT", "1000SATSUSDT"]
    if symbol in high_precision:
        return str(int(qty))
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ OPEN LONG =======================
async def open_long(sym: str):
    symbol = sym.upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if symbol in active:
        await tg(f"<b>{symbol}</b> — позиция уже открыта")
        return

    # 1. Устанавливаем Cross Margin
    resp_margin = await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    if not resp_margin:
        await tg(f"<b>Не удалось установить CROSS для {symbol}</b>")
        return

    # 2. Леверидж
    resp_lev = await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
    if not resp_lev:
        await tg(f"<b>Не удалось установить ×{LEV} для {symbol}</b>")
        return

    # 3. Текущая цена
    data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not data or "price" not in data:
        await tg(f"<b>Не удалось получить цену {symbol}</b>")
        return
    price = float(data["price"])

    # 4. Расчёт количества
    raw_qty = AMOUNT * LEV / price
    qty = fix_qty(symbol, raw_qty)

    # 5. Открываем LONG в Hedge Mode
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty
    })

    if order and order.get("orderId"):
        active.add(symbol)
        await tg(
            f"<b>LONG ×{LEV} (Cross + Hedge)</b>\n"
            f"<code>{symbol}</code>\n"
            f"{qty} шт ≈ ${AMOUNT} @ {price:.8f}".strip()
        )
    else:
        await tg(f"<b>ОШИБКА ОТКРЫТИЯ LONG {symbol}</b>")

# ================= CLOSE ==========================
async def close(sym: str):
    symbol = sym.upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if symbol not in active:
        await tg(f"<b>{symbol}</b> — нет открытой позиции")
        return

    pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos:
        return

    qty = None
    for p in pos:
        if p["symbol"] == symbol and p["positionSide"] == "LONG" and float(p["positionAmt"]) > 0:
            qty = p["positionAmt"]
            break

    if not qty:
        await tg(f"<b>{symbol}</b> — позиция нулевая или уже закрыта")
        active.discard(symbol)
        return

    close_order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    })

    if close_order:
        active.discard(symbol)
        await tg(f"<b>CLOSE {symbol}</b>\n{qty} шт по рынку")
    else:
        await tg(f"<b>Не удалось закрыть {symbol}</b>")

# ================= FASTAPI =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await sync_time()  # синхронизируем время с Binance
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

    return {"ok": True, "received": symbol}
