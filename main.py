# main.py — BINANCE XRP BOT — FIXED QUANTITY
import os
import time
import hmac
import hashlib
import urllib.parse
import asyncio
import logging
from typing import Optional, Dict, Any
import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse
from telegram import Bot

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
QUANTITY = "3"  # фиксированное количество XRP
bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20.0)

position_active = False
current_status = "Ожидание сигнала..."

# ====================== HTML ======================
HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XRP BOT LIVE</title>
<style>
body {{margin:0; font-family:Segoe UI; background:linear-gradient(135deg,#0f0f23,#1a1a2e); color:#fff; height:100vh; display:flex; align-items:center; justify-content:center;}}
.card {{background:rgba(255,255,255,0.05); padding:40px; border-radius:20px; border:2px solid #00ffcc; box-shadow:0 0 30px rgba(0,255,204,0.3); text-align:center; max-width:500px; width:90%;}}
h1 {{font-size:3.5rem; margin:0; text-shadow:0 0 20px #00ffcc; animation:pulse 3s infinite;}}
.price {{font-size:2.8rem; margin:25px 0; color:#00ffcc; font-weight:bold;}}
.status {{font-size:1.5rem; background:rgba(0,255,204,0.1); padding:15px; border-radius:15px; margin:20px 0;}}
.info {{font-size:1.1rem; color:#ccc; margin-top:20px;}}
@keyframes pulse {{0%,100%{{opacity:0.7}}50%{{opacity:1}}}}
</style>
</head>
<body>
<div class="card">
<h1>XRP BOT</h1>
<div class="price">{price} USDT</div>
<div class="status">{status}</div>
<div class="info">${amount} × {leverage}x | TP +{tp}%</div>
</div>
</body>
</html>"""

# ====================== TELEGRAM ======================
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== BINANCE SIGN ======================
def sign(params: Dict[str, Any]) -> str:
    query = urllib.parse.urlencode(params)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ====================== BINANCE REQUEST ======================
async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    headers = {"X-MBX-APIKEY": API_KEY}

    if method in ["POST", "DELETE", "PUT"] or endpoint.endswith("/order"):
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = sign(params)

    try:
        if method == "POST":
            r = await client.post(url, data=params, headers=headers)
        else:
            r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        msg = "Unknown error"
        if hasattr(e, "response") and e.response is not None:
            try:
                msg = e.response.json().get("msg", e.response.text[:200])
            except:
                msg = str(e)
        logger.error(f"Binance error: {msg}")
        raise Exception(msg)

# ====================== PRICE ======================
async def get_price() -> float:
    try:
        data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL})
        return float(data.get("price", 0))
    except:
        return 0.0

# ====================== OPEN LONG ======================
async def open_long():
    global position_active, current_status
    if position_active:
        await tg_send("Позиция уже открыта!")
        return

    try:
        # Жёстко фиксированное количество XRP
        qty = "3"  # <--- количество XRP, всегда целое число
        entry = await get_price()
        if entry <= 0:
            await tg_send("Не удалось получить цену XRP")
            return

        tp_price = round(entry * (1 + TP_PERCENT / 100), 5)
        sl_price = round(entry * (1 - SL_PERCENT / 100), 5)
        start = time.time()

        # Открываем LONG
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty
        })
        # TP и SL
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "TAKE_PROFIT_MARKET",
            "quantity": qty,
            "stopPrice": f"{tp_price:.5f}",
            "reduceOnly": "true",
            "workingType": "MARK_PRICE"
        })
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": qty,
            "stopPrice": f"{sl_price:.5f}",
            "reduceOnly": "true",
            "workingType": "MARK_PRICE"
        })

        took = round(time.time() - start, 2)
        position_active = True
        current_status = f"LONG | Вход {entry:.5f}"

        await tg_send(f"""
NEW LONG XRP

<b>Сумма:</b> ${FIXED_USD} × {LEVERAGE}x
<b>Вход:</b> <code>{entry:.5f}</code>
<b>TP +{TP_PERCENT}%:</b> <code>{tp_price:.5f}</code>
<b>SL -{SL_PERCENT}%:</b> <code>{sl_price:.5f}</code>
<b>Кол-во:</b> <code>{qty}</code> XRP
<b>Время:</b> {took}s
""")
    except Exception as e:
        position_active = False
        current_status = "Ошибка"
        await tg_send(f"ОШИБКА ОТКРЫТИЯ:\n<code>{e}</code>")


# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def root():
    price = await get_price()
    price_str = f"{price:.5f}" if price > 0 else "—"
    return HTML_PAGE.format(
        price=price_str,
        status=current_status,
        amount=FIXED_USD,
        leverage=LEVERAGE,
        tp=TP_PERCENT
    )

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "XRP alive"}

@app.post("/webhook")
async def webhook(request: Request, x_secret: Optional[str] = Header(None, alias="X-Webhook-Secret")):
    if x_secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    try:
        payload = await request.json()
        signal = payload.get("signal", "").lower()
    except:
        signal = (await request.body()).decode().lower().strip()
    if signal in ["buy", "long", "obuy", "go", "лонг", "вход"]:
        await tg_send("СИГНАЛ — ОТКРЫВАЮ LONG XRP")
        asyncio.create_task(open_long())
        return {"status": "long_initiated"}
    return {"status": "ignored"}

@app.on_event("startup")
async def startup():
    await tg_send("XRP BOT ЗАПУЩЕН И ГОТОВ")
    try:
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": LEVERAGE})
        logger.info(f"Плечо {LEVERAGE}x установлено")
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e}")

@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
