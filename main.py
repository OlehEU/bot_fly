# main.py — XRP Futures Bot 2025 — Финальная боевая версия
import os
import time
import hmac
import hashlib
import asyncio
import logging
from typing import Optional
import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse
from telegram import Bot

# ====================== ЛОГИ ======================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("XRP-BOT")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY          = os.getenv("BINANCE_API_KEY")
API_SECRET       = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_USD   = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE    = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT  = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT  = float(os.getenv("SL_PERCENT", "1.0"))

SYMBOL = "XRPUSDT"
bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20.0)

position_active = False
last_entry_price = 0.0
current_status = "Ожидание сигнала..."

# ====================== ПОДПИСЬ ======================
def sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== API ======================
async def binance_request(method: str, endpoint: str, params: dict | None = None):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    headers = {"X-MBX-APIKEY": API_KEY}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign(params)

    try:
        r = await (client.post if method == "POST" else client.get)(url, data=params, headers=headers)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        try:
            err = e.response.json()
            msg = err.get("msg", str(err))
        except:
            msg = e.response.text[:300]
        logger.error(f"Binance error: {msg}")
        raise Exception(msg)

# ====================== ЦЕНА И КОЛИЧЕСТВО ======================
async def get_price() -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL})
    return float(data["price"])

async def get_quantity() -> str:
    price = await get_price()
    qty = (FIXED_USD * LEVERAGE) / price
    info = await binance_request("GET", "/fapi/v1/exchangeInfo")
    prec = next(s["quantityPrecision"] for s in info["symbols"] if s["symbol"] == SYMBOL)
    return f"{qty:.{prec}f}"

# ====================== ОТКРЫТИЕ ЛОНГА ======================
async def open_long():
    global position_active, last_entry_price, current_status
    if position_active:
        await tg_send("Позиция уже открыта! Дубли отклонены.")
        return

    try:
        qty = await get_quantity()
        entry_price = await get_price()
        last_entry_price = entry_price
        tp_price = round(entry_price * (1 + TP_PERCENT / 100), 5)
        sl_price = round(entry_price * (1 - SL_PERCENT / 100), 5)

        start_time = time.time()

        # 1. Рыночный LONG
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
        })

        # 2. TP +0.5%
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "TAKE_PROFIT_MARKET",
            "quantity": qty,
            "stopPrice": f"{tp_price:.5f}",
            "reduceOnly": "true",
            "workingType": "MARK_PRICE"
        })

        # 3. SL
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": qty,
            "stopPrice": f"{sl_price:.5f}",
            "reduceOnly": "true",
            "workingType": "MARK_PRICE"
        })

        took = round(time.time() - start_time, 2)
        position_active = True
        current_status = f"Позиция открыта | Вход: {entry_price:.5f}"

        await tg_send(f"""
NEW LONG XRP

<b>Сумма:</b> ${FIXED_USD} × {LEVERAGE}x = ${(FIXED_USD*LEVERAGE):.1f}
<b>Вход:</b> <code>{entry_price:.5f}</code> USDT
<b>TP +{TP_PERCENT}%:</b> <code>{tp_price:.5f}</code>
<b>SL -{SL_PERCENT}%:</b> <code>{sl_price:.5f}</code>

<b>Количество:</b> <code>{qty}</code> XRP
<b>Время отклика:</b> {took} сек

<i>Позиция закроется только по TP или SL</i>
""")

    except Exception as e:
        position_active = False
        current_status = "Ошибка"
        await tg_send(f"ОШИБКА ОТКРЫТИЯ:\n<code>{e}</code>")
        logger.error(f"Open long failed: {e}")

# ====================== КРАСИВАЯ СТРАНИЦА ======================
HTML_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XRP Bot — LIVE</title>
<style>
  body {margin:0;font-family:Segoe UI;background:linear-gradient(135deg,#0f0f23,#1a1a2e);color:#fff;height:100vh;display:flex;align-items:center;justify-content:center;}
  .card {background:rgba(255,255,255,0.05);padding:40px;border-radius:20px;border:2px solid #00ffcc;box-shadow:0 0 30px rgba(0,255,204,0.3);text-align:center;max-width:500px;}
  h1 {font-size:3.5rem;margin:0;text-shadow:0 0 20px #00ffcc;animation:pulse 3s infinite;}
  .price {font-size:2.5rem;margin:20px 0;}
  .status {font-size:1.4rem;background:rgba(0,255,204,0.1);padding:15px;border-radius:15px;margin:20px 0;}
  @keyframes pulse{0%,100%{opacity:0.7}50%{opacity:1}}
</style></head><body>
<div class="card">
  <h1>XRP BOT</h1>
  <div class="price"><b>{price}</b> USDT</div>
  <div class="status">{status}</div>
  <p>${FIXED_USD} × {LEVERAGE}x | TP +{TP_PERCENT}%</p>
</div>
</body></html>"""

# ====================== FastAPI ======================
app = FastAPI()   # ← ВАЖНО: СНАЧАЛА создаём app!

@app.get("/", response_class=HTMLResponse)
async def root():
    try:
        price = await get_price()
    except:
        price = "—"
    return HTML_PAGE.format(
        price=f"{price:.5f}" if isinstance(price, float) else price,
        status=current_status,
        FIXED_USD=FIXED_USD,
        LEVERAGE=LEVERAGE,
        TP_PERCENT=TP_PERCENT
    )

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "XRP alive", "time": int(time.time())}

@app.post("/webhook")
async def webhook(request: Request, x_secret: Optional[str] = Header(None, alias="X-Webhook-Secret")):
    if x_secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")

    try:
        payload = await request.json()
        signal = payload.get("signal", "").lower()
    except:
        text = (await request.body()).decode().strip().lower()
        signal = text

    if signal in ["obuy", "buy", "long", "go", "лонг", "вход"]:
        await tg_send("СИГНАЛ TRADINGVIEW — ОТКРЫВАЮ LONG XRP")
        asyncio.create_task(open_long())
        return {"status": "long_initiated"}
    return {"status": "ignored", "signal": signal}

@app.on_event("startup")
async def startup():
    await tg_send("XRP BOT ЗАПУЩЕН И ГОТОВ К ТОРГОВЛЕ")
    try:
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": LEVERAGE})
        logger.info(f"Плечо {LEVERAGE}x установлено")
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e}")

@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
