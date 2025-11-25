# main.py — ТЕРМИНАТОР 2026 | НА ОСНОВЕ ТВОЕГО РАБОЧЕГО КОДА | ЛЮБАЯ МОНЕТА | 10 USDT
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
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=60.0)

async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== BINANCE HELPERS — ИЗ ТВОЕГО РАБОЧЕГО КОДА ======================
def _create_signature(params: dict, secret: str) -> str:
    normalized = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            normalized[k] = str(v).lower()
        elif isinstance(v, (int, float)):
            normalized[k] = str(v)
        else:
            normalized[k] = str(v)
    query_string = urllib.parse.urlencode(normalized)
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: dict = None, signed: bool = True):
    url = f"https://fapi.binance.com{endpoint}"
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _create_signature(params, BINANCE_API_SECRET)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        resp = await (client.post if method == "POST" else client.get)(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                error_msg = e.response.json()
            except:
                error_msg = e.response.text
        logger.error(f"BINANCE ERROR: {error_msg}")
        return {"code": -1, "msg": error_msg}

# ====================== ПРЕЛОАД + ПЛЕЧО ======================
async def preload_symbol(symbol: str):
    symbol_binance = symbol + "USDT"
    try:
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol_binance, "leverage": str(LEVERAGE)})
        logger.info(f"Плечо {LEVERAGE}x установлено для {symbol_binance}")
    except:
        pass

# ====================== ОТКРЫТИЕ ЛОНГА ======================
async def open_long(symbol: str):
    await preload_symbol(symbol)
    symbol_binance = symbol + "USDT"
    try:
        # Получаем цену и qty
        price_data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol_binance}, signed=False)
        price = float(price_data["price"])
        qty = round((FIXED_AMOUNT_USD * LEVERAGE) / price, 6)

        # Рыночный ордер
        order = await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol_binance,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(qty)
        })

        if "orderId" not in order:
            await tg_send(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}USDT</b>\n{order}")
            return

        entry = float(order.get("avgPrice", price))
        tp_price = round(entry * (1 + TP_PERCENT / 100), 6)
        sl_price = round(entry * (1 - SL_PERCENT / 100), 6)

        # TP/SL
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol_binance,
            "side": "SELL",
            "type": "TAKE_PROFIT_MARKET",
            "quantity": str(qty),
            "stopPrice": str(tp_price),
            "reduceOnly": "true"
        })
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol_binance,
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": str(qty),
            "stopPrice": str(sl_price),
            "reduceOnly": "true"
        })

        await tg_send(f"""
<b>LONG {symbol}USDT ОТКРЫТ</b>
По сигналу OZ SCANNER
${FIXED_AMOUNT_USD} × {LEVERAGE}x
Entry: <code>{entry:.6f}</code>
TP: <code>{tp_price:.6f}</code> (+{TP_PERCENT}%)
SL: <code>{sl_price:.6f}</code> (-{SL_PERCENT}%)
        """.strip())
    except Exception as e:
        await tg_send(f"<b>ОШИБКА {symbol}USDT</b>\n<code>{traceback.format_exc()}</code>")

# ====================== ЗАКРЫТИЕ ======================
async def close_position(symbol: str):
    symbol_binance = symbol + "USDT"
    try:
        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol_binance})
        amt = 0.0
        for p in pos if isinstance(pos, list) else []:
            if p["symbol"] == symbol_binance:
                amt = float(p["positionAmt"])
                break
        if abs(amt) < 0.001:
            await tg_send(f"Позиция {symbol}USDT уже закрыта")
            return
        side = "SELL" if amt > 0 else "BUY"
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol_binance,
            "side": side,
            "type": "MARKET",
            "quantity": f"{abs(amt):.6f}".rstrip("0").rstrip("."),
            "reduceOnly": "true"
        })
        await tg_send(f"<b>ЗАКРЫЛ {symbol}USDT</b>\nПо сигналу OZ SCANNER")
    except Exception as e:
        await tg_send(f"<b>ОШИБКА ЗАКРЫТИЯ {symbol}</b>\n{str(e)}")

# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/")
async def root():
    return HTMLResponse("<h1 style='color:#0f0;background:#000;text-align:center;padding:100px'>ТЕРМИНАТОР 2026<br>ONLINE · ВООРУЖЁН · ТОРГУЕТ</h1>")

@app.on_event("startup")
async def startup():
    await tg_send("<b>ТЕРМИНАТОР 2026 ЗАПУЩЕН</b>\nГотов рвать рынок на 10 USDT")

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)

    try:
        data = await request.json()
    except:
        raise HTTPException(400)

    symbol = data.get("symbol", "").upper().replace("USDT", "")
    direction = data.get("direction", "").upper()

    if not symbol or direction not in ["LONG", "CLOSE"]:
        return {"error": "bad data"}

    if direction == "LONG":
        asyncio.create_task(open_long(symbol))
    else:
        asyncio.create_task(close_position(symbol))

    return {"status": "ok", "symbol": symbol, "action": direction}
