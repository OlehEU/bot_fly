# main.py — УНИВЕРСАЛЬНЫЙ МУЛЬТИКОИН БОТ 2026 (ФИНАЛЬНАЯ ВЕРСИЯ — РАБОТАЕТ НА FLY.IO)
import os
import json
import time
import logging
import asyncio
import sys
from typing import Dict, Any, Optional
import httpx
import hmac
import hashlib
import urllib.parse
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("multi-coin-bot")

# ====================== КОНФИГ ======================
required_env = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
for var in required_env:
    if not os.getenv(var):
        raise EnvironmentError(f"ОБЯЗАТЕЛЬНАЯ переменная не задана: {var}")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "supersecret123")

# Дефолтные коины
COINS = {
    "XRP":  {"amount_usd": 10,  "leverage": 10,  "tp_percent": 0.5, "sl_percent": 1.0, "enabled": True,  "disable_tpsl": True},
    "SOL":  {"amount_usd": 15,  "leverage": 20,  "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True},
    "ETH":  {"amount_usd": 20,  "leverage": 5,   "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True},
    "BTC":  {"amount_usd": 50,  "leverage": 3,   "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True},
    "DOGE": {"amount_usd": 5,   "leverage": 50,  "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True}
}

SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"

# ====================== НАСТРОЙКИ ======================
def load_settings() -> Dict:
    try:
        with open(SETTINGS_FILE, 'r') as f:
            saved = json.load(f)
        for coin, default in COINS.items():
            if coin not in saved:
                saved[coin] = default.copy()
            else:
                for k, v in default.items():
                    saved[coin].setdefault(k, v)
        return saved
    except (FileNotFoundError, json.JSONDecodeError):
        default = {c: d.copy() for c, d in COINS.items()}
        save_settings(default)
        return default

def save_settings(s: Dict): 
    with open(SETTINGS_FILE, 'w') as f: 
        json.dump(s, f, indent=2)

def load_stats() -> Dict:
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        default = {"total_pnl": 0.0, "per_coin": {c: 0.0 for c in COINS}}
        save_stats(default)
        return default

def save_stats(s: Dict): 
    with open(STATS_FILE, 'w') as f: 
        json.dump(s, f, indent=2)

settings = load_settings()
stats = load_stats()

# ====================== ГЛОБАЛЬНЫЕ ======================
binance_client = httpx.AsyncClient(timeout=60.0)
last_balance_before_trade: Dict[str, float] = {}
bot_instance: Optional[Bot] = None

# ====================== УТИЛИТЫ ======================
async def tg_send(text: str):
    if bot_instance:
        try:
            await bot_instance.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"TG send error: {e}")

async def tg_balance():
    bal = await get_futures_balance()
    await tg_send(f"<b>Баланс:</b> <code>{bal:,.2f}</code> USDT")

# ====================== BINANCE ======================
BINANCE_BASE = "https://fapi.binance.com"

def sign(params: Dict) -> str:
    query = urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items() if v is not None})
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def binance(method: str, endpoint: str, params: Optional[Dict] = None, signed: bool = True, symbol: Optional[str] = None):
    url = f"{BINANCE_BASE}{endpoint}"
    p = params or {}
    if symbol: p["symbol"] = f"{symbol}USDT"
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        r = await (binance_client.get if method == "GET" else binance_client.post)(url, params=p, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Binance error: {e}")
        return None

async def get_price(sym: str) -> float:
    data = await binance("GET", "/fapi/v1/ticker/price", signed=False, symbol=sym)
    return float(data["price"]) if data else 0.0

async def get_qty(sym: str) -> float:
    cfg = settings[sym]
    price = await get_price(sym)
    raw = (cfg["amount_usd"] * cfg["leverage"]) / price
    info = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in info.get("symbols", []):
        if s["symbol"] == f"{sym}USDT":
            prec = s.get("quantityPrecision", 3)
            min_qty = next((float(f["minQty"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0)
            qty = round(raw, prec)
            return max(qty, min_qty)
    return 0.0

async def get_futures_balance() -> float:
    data = await binance("GET", "/fapi/v2/balance", signed=True)
    for a in data or []:
        if a["asset"] == "USDT":
            return float(a["balance"])
    return 0.0

# ====================== ТОРГОВЛЯ ======================
async def open_long(coin: str):
    if not settings[coin]["enabled"]: return
    try:
        qty = await get_qty(coin)
        if qty <= 0: return
        oid = f"{coin.lower()}_{int(time.time()*1000)}"
        entry = await get_price(coin)
        await binance("POST", "/fapi/v1/order", {"side": "BUY", "type": "MARKET", "quantity": str(qty), "newClientOrderId": oid}, symbol=coin)
        last_balance_before_trade[coin] = await get_futures_balance()
        await tg_send(f"<b>OPEN LONG — {coin}</b>\n${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x\nEntry: <code>{entry:.5f}</code>")
        await tg_balance()
    except Exception as e: await tg_send(f"Ошибка открытия {coin}: {e}")

async def close_all(coin: str):
    if not settings[coin]["enabled"]: return
    try:
        pos = await binance("GET", "/fapi/v2/positionRisk", symbol=coin)
        cur = next((p for p in pos if float(p.get("positionAmt", 0)) != 0), None)
        if cur:
            qty = abs(float(cur["positionAmt"]))
            side = "SELL" if float(cur["positionAmt"]) > 0 else "BUY"
            await binance("POST", "/fapi/v1/order", {"side": side, "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"}, symbol=coin)
        await binance("GET", "/fapi/v1/openOrders", symbol=coin)
        current_bal = await get_futures_balance()
        pnl = current_bal - last_balance_before_trade.get(coin, current_bal)
        stats["per_coin"][coin] = stats["per_coin"].get(coin, 0) + pnl
        stats["total_pnl"] = stats.get("total_pnl", 0) + pnl
        save_stats(stats)
        await tg_send(f"<b>{coin} ЗАКРЫТ</b>\nПрибыль: <code>{pnl:+.2f}</code> USDT")
        await tg_balance()
    except Exception as e: await tg_send(f"Ошибка закрытия {coin}: {e}")

# ====================== TELEGRAM ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    for c in COINS:
        status = "ON" if settings[c]["enabled"] else "OFF"
        kb.append([InlineKeyboardButton(f"{c} {status} | ${settings[c]['amount_usd']} × {settings[c]['leverage']}x", callback_data=f"menu_{c}")])
    kb += [
        [InlineKeyboardButton("Баланс", callback_data="bal")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
    ]
    await update.message.reply_text("Универсальный бот 2026\nВыбери коин:", reply_markup=InlineKeyboardMarkup(kb))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("menu_"):
        coin = data[5:]
        kb = [
            [InlineKeyboardButton("ON" if not settings[coin]["enabled"] else "OFF", callback_data=f"toggle_{coin}")],
            [InlineKeyboardButton("Назад", callback_data="back")]
        ]
        await q.edit_message_text(f"Управление {coin}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("toggle_"):
        coin = data[7:]
        settings[coin]["enabled"] = not settings[coin]["enabled"]
        save_settings(settings)
        await button(update, context)

    elif data == "back":
        await start(update, context)

    elif data == "bal":
        b = await get_futures_balance()
        await q.edit_message_text(f"Баланс: <code>{b:,.2f}</code> USDT", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))

    elif data == "stats":
        text = f"<b>Статистика</b>\nВсего: <code>{stats.get('total_pnl',0):+.2f}</code> USDT\n\n"
        for c, p in stats["per_coin"].items():
            text += f"{c}: <code>{p:+.2f}</code>\n"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_instance
    bot_instance = Bot(token=TELEGRAM_TOKEN)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await tg_send("Бот запущен и готов к бою!")
    yield
    await application.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root(): return HTMLResponse("<h1>Bot ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        if data.get("secret") != WEBHOOK_SECRET: raise HTTPException(403)
        signal = data.get("signal", "").lower()
        coin = data.get("coin", "XRP").upper()
        if coin not in COINS: return {"error": "unknown coin"}
        if signal in ["buy", "long"]: asyncio.create_task(open_long(coin))
        elif signal == "close_all": asyncio.create_task(close_all(coin))
        return {"ok": True}
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}
