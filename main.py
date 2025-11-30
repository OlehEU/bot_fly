# main.py — OZ TRADING BOT 2026 + ATR TRAILING (абсолютный топ)
import os
import time
import logging
import hmac
import hashlib
from typing import Dict, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oz-atr-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = "supersecret123"

# === НАСТРОЙКИ ===
FIXED_AMOUNT_USDT = float(os.getenv("FIXED_AMOUNT_USDT", "30"))
LEVERAGE = int(os.getenv("LEVERAGE", "25"))
TP_MULTIPLIER = float(os.getenv("TP_MULTIPLIER", "3.0"))        # TP = entry + 3.0 × ATR
ACTIVATION_ATR = float(os.getenv("ACTIVATION_ATR", "1.2"))      # Включаем трейлинг при +1.2× ATR
TRAIL_MULTIPLIER = float(os.getenv("TRAIL_MULTIPLIER", "1.8"))  # Стоп = current_price - 1.8× ATR
AUTO_CLOSE_MINUTES = int(os.getenv("AUTO_CLOSE_MINUTES", "120"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=30.0)
BASE_URL = "https://fapi.binance.com"

active_positions: Dict[str, dict] = {}  # symbol → данные

# ====================== BINANCE API ======================
def sign(params: Dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in params.items() if v is not None))
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def api(method: str, endpoint: str, params: Optional[Dict] = None, signed: bool = True):
    url = f"{BASE_URL}{endpoint}"
    p = params or {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        msg = str(e)
        try: msg = e.response.json().get("msg", msg)
        except: pass
        raise Exception(msg)

# ====================== ATR ======================
async def get_atr(symbol: str, timeframe: str = "5m", period: int = 14) -> float:
    try:
        klines = await api("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": timeframe, "limit": period+1}, signed=False)
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        tr_list = []
        for i in range(1, len(klines)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
        return sum(tr_list[-period:]) / period if tr_list else 0.001
    except:
        return 0.001  # fallback

# ====================== ПОЗИЦИИ ======================
async def open_long(symbol: str, entry_price: float, reason: str):
    if symbol in active_positions:
        return

    await api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
    qty = round((FIXED_AMOUNT_USDT * LEVERAGE) / entry_price, 6)
    qty = max(qty, 0.001)

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": str(qty)
    })

    atr = await get_atr(symbol)
    tp_price = round(entry_price + TP_MULTIPLIER * atr, 6)

    await api("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "quantity": str(qty), "stopPrice": str(tp_price), "reduceOnly": "true"
    })

    active_positions[symbol] = {
        "entry": entry_price,
        "qty": qty,
        "atr": atr,
        "last_trailing_stop": entry_price - 2 * atr,  # начальный стоп далеко
        "trailing_active": False,
        "open_time": time.time(),
        "reason": reason
    }

    await tg_send(f"<b>LONG ОТКРЫТ + ATR-ТРЕЙЛИНГ</b>\n"
                  f"<code>{symbol.replace('USDT','/USDT')}</code>\n"
                  f"Цена: <code>{entry_price:.6f}</code> | ATR: <code>{atr:.6f}</code>\n"
                  f"TP: +{TP_MULTIPLIER}×ATR | Трейлинг: {TRAIL_MULTIPLIER}×ATR\n"
                  f"{reason}")

async def close_position(symbol: str, reason: str):
    pos = await api("GET", "/fapi/v2/positionRisk")
    for p in pos:
        if p["symbol"] == symbol and float(p["positionAmt"]) != 0:
            qty = abs(float(p["positionAmt"]))
            await api("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": f"{qty:.8f}", "reduceOnly": "true"
            })
            pnl = float(p.get("unRealizedProfit", 0))
            await tg_send(f"<b>ПОЗИЦИЯ ЗАКРЫТА</b>\n"
                          f"{symbol.replace('USDT','/USDT')} | {reason}\n"
                          f"PnL: <code>{pnl:+.4f} USDT</code>")
            active_positions.pop(symbol, None)
            return

# ====================== ATR ТРЕЙЛИНГ ======================
async def atr_trailing_loop():
    while True:
        await asyncio.sleep(9)
        now = time.time()
        for symbol, data in list(active_positions.items()):
            try:
                price_data = await api("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
                current_price = float(price_data["price"])
                atr = await get_atr(symbol)  # обновляем ATR каждые 9 сек
                data["atr"] = atr

                profit_in_atr = (current_price - data["entry"]) / atr

                # Активация трейлинга
                if not data["trailing_active"] and profit_in_atr >= ACTIVATION_ATR:
                    data["trailing_active"] = True
                    await tg_send(f"ATR-ТРЕЙЛИНГ АКТИВИРОВАН\n"
                                  f"{symbol.replace('USDT','/USDT')}\n"
                                  f"Цена: {current_price:.6f} (+{profit_in_atr:.2f}×ATR)")

                if data["trailing_active"]:
                    new_stop = current_price - TRAIL_MULTIPLIER * atr
                    if new_stop > data["last_trailing_stop"] + atr * 0.1:  # двигаем минимум на 0.1×ATR
                        await api("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
                        await api("POST", "/fapi/v1/order", {
                            "symbol": symbol,
                            "side": "SELL",
                            "type": "STOP_MARKET",
                            "quantity": str(data["qty"]),
                            "stopPrice": f"{new_stop:.6f}",
                            "reduceOnly": "true"
                        })
                        data["last_trailing_stop"] = new_stop
                        await tg_send(f"Трейлинг обновлён\n"
                                      f"{symbol.replace('USDT','/USDT')}\n"
                                      f"Стоп: <code>{new_stop:.6f}</code> (-{TRAIL_MULTIPLIER}×ATR)")

                if now - data["open_time"] > AUTO_CLOSE_MINUTES * 60:
                    await close_position(symbol, "Авто-закрытие по времени")

            except Exception as e:
                logger.error(f"ATR trailing error {symbol}: {e}")

# ====================== СООБЩЕНИЯ ======================
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"TG error: {e}")

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send("OZ TRADING BOT 2026 + ATR-ТРЕЙЛИНГ — ЗАПУЩЕН!\n"
                  "Самый умный трейлинг в мире активирован.")
    asyncio.create_task(atr_trailing_loop())
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse(f"<h1 style='color:#0f0'>OZ BOT + ATR TRAILING 2026 — ONLINE</h1>"
                        f"<p>Позиций: {len(active_positions)}</p>")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(400, "Bad JSON")

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(403, "Wrong secret")

    symbol = data.get("symbol", "").replace("/", "")
    signal = data.get("signal", "").upper()
    price = float(data.get("price", 0))
    reason = data.get("reason", "Сигнал от сканера")
    tf = data.get("timeframe", "")

    if signal == "LONG":
        await open_long(symbol, price, f"{reason} [{tf}]")
    elif signal == "CLOSE":
        await close_position(symbol, f"{reason} [{tf}]")

    return {"status": "ok"}
