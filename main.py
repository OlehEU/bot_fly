# main.py — ТЕРМИНАТОР 2026 | ЧИСТЫЙ ВХОД | 10 USDT | ЛЮБАЯ МОНЕТА | БЕЗ TP/SL
import os
import time
import hmac
import hashlib
import urllib.parse
import logging
import asyncio
import traceback
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("terminator")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "supersecret123")

AMOUNT_USD  = float(os.getenv("AMOUNT_USD", "10"))   # 10 USDT на сделку
LEVERAGE    = int(os.getenv("LEVERAGE", "10"))      # 10x по умолчанию

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=60.0)

async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== ПОДПИСЬ — ТВОЯ РАБОЧАЯ ======================
def _sign(params: dict) -> str:
    normalized = {k: str(v) for k, v in params.items() if v is not None}
    query = urllib.parse.urlencode(normalized)
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance(method: str, endpoint: str, params: dict = None, signed: bool = True):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        resp = await (client.post if method == "POST" else client.get)(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response"):
            try: msg = e.response.json()
            except: msg = e.response.text[:500]
        logger.error(f"BINANCE ERROR: {msg}")
        return {"code": -1, "msg": msg}

# ====================== УСТАНОВКА ПЛЕЧА ======================
async def set_leverage(symbol: str):
    try:
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol+"USDT", "leverage": str(LEVERAGE)})
    except:
        pass

# ====================== ОТКРЫТИЕ ЛОНГА (БЕЗ TP/SL) ======================
async def open_long(symbol: str):
    await set_leverage(symbol)
    symbol_bin = symbol + "USDT"
    try:
        # 1. Точность количества
        info = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
        precision = 3
        for s in info.get("symbols", []):
            if s["symbol"] == symbol_bin:
                precision = s.get("quantityPrecision", 3)
                break

        # 2. Цена
        price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol_bin}, signed=False)
        price = float(price_data["price"])

        # 3. Количество с правильной точностью
        qty_raw = (AMOUNT_USD * LEVERAGE) / price
        qty = round(qty_raw, precision)
        qty_str = str(int(qty)) if precision == 0 else f"{qty:.{precision}f}".rstrip("0").rstrip(".")

        # 4. Ордер
        order = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol_bin,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty_str
        })

        if "orderId" not in order:
            await tg_send(f"<b>ОШИБКА {symbol}USDT</b>\n<code>{order}</code>")
            return

        entry = float(order.get("avgPrice", price))
        await tg_send(f"""
<b>LONG {symbol}USDT ОТКРЫТ</b>
${AMOUNT_USD} × {LEVERAGE}x = ${(AMOUNT_USD*LEVERAGE):.1f}
Entry: <code>{entry:.6f}</code>
Кол-во: {qty_str} {symbol}
        """.strip())

    except Exception as e:
        await tg_send(f"<b>КРИТИЧЕСКАЯ ОШИБКА {symbol}</b>\n<code>{traceback.format_exc()}</code>")

# ====================== ЗАКРЫТИЕ ======================
async def close_position(symbol: str):
    try:
        pos = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol+"USDT"})
        amt = 0.0
        for p in pos if isinstance(pos, list) else []:
            if p["symbol"] == symbol+"USDT":
                amt = float(p["positionAmt"])
                break
        if abs(amt) < 0.001:
            await tg_send(f"{symbol}USDT — позиция уже закрыта")
            return
        side = "SELL" if amt > 0 else "BUY"
        qty_str = f"{abs(amt):.6f}".rstrip("0").rstrip(".")
        await binance("POST", "/fapi/v1/order", {
            "symbol": symbol+"USDT",
            "side": side,
            "type": "MARKET",
            "quantity": qty_str,
            "reduceOnly": "true"
        })
        await tg_send(f"<b>{symbol}USDT ЗАКРЫТ</b>")
    except Exception as e:
        await tg_send(f"<b>Ошибка закрытия {symbol}</b>\n<code>{e}</code>")

# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/")
async def root():
    return HTMLResponse("<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>ТЕРМИНАТОР 2026<br>ONLINE · ТОРГУЕТ НА 10$</h1>")

@app.on_event("startup")
async def start():
    await tg_send("<b>ТЕРМИНАТОР 2026 АКТИВИРОВАН</b>\nГотов принимать сигналы OZ SCANNER")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    symbol = data.get("symbol", "").upper().replace("USDT", "")
    action = data.get("direction", "").upper()

    if not symbol or action not in ["LONG", "CLOSE"]:
        return {"error": "bad data"}

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    else:
        asyncio.create_task(close_position(symbol))

    return {"status": "ok", "symbol": symbol, "action": action}
