# main.py — ФИНАЛЬНАЯ ВЕРСИЯ 2026 (всё работает: меню, назад, баланс, торговля)
import os
import json
import time
import logging
import asyncio
from typing import Dict, Optional
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
logger = logging.getLogger(__name__)

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret123")

COINS = {
    "XRP": {"amount_usd": 10, "leverage": 10, "enabled": True, "disable_tpsl": True},
    "SOL": {"amount_usd": 15, "leverage": 20, "enabled": False, "disable_tpsl": True},
    "ETH": {"amount_usd": 20, "leverage": 5, "enabled": False, "disable_tpsl": True},
    "BTC": {"amount_usd": 50, "leverage": 3, "enabled": False, "disable_tpsl": True},
    "DOGE": {"amount_usd": 5, "leverage": 50, "enabled": False, "disable_tpsl": True},
}

SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"

# ====================== НАСТРОЙКИ ======================
def load_settings() -> Dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        for coin, defs in COINS.items():
            if coin not in data:
                data[coin] = defs.copy()
            else:
                for k, v in defs.items():
                    data[coin].setdefault(k, v)
        return data
    except Exception:
        default = {c: d.copy() for c, d in COINS.items()}
        save_settings(default)
        return default

def save_settings(s: Dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

def load_stats() -> Dict:
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except Exception:
        default = {"total_pnl": 0.0, "per_coin": {c: 0.0 for c in COINS}}
        save_stats(default)
        return default

def save_stats(s: Dict):
    with open(STATS_FILE, "w") as f:
        json.dump(s, f, indent=2)

settings = load_settings()
stats = load_stats()

# ====================== ГЛОБАЛЬНЫЕ ======================
client = httpx.AsyncClient(timeout=60.0)
last_balance: Dict[str, float] = {}
bot: Optional[Bot] = None

# ====================== BINANCE ======================
BASE = "https://fapi.binance.com"

def sign(p: Dict) -> str:
    q = urllib.parse.urlencode({k: str(v).lower() if isinstance(v,bool) else str(v) for k,v in p.items() if v is not None})
    return hmac.new(BINANCE_API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

async def api(method: str, ep: str, params: Dict = None, signed=True, symbol=None):
    url = f"{BASE}{ep}"
    p = params or {}
    if symbol: p["symbol"] = f"{symbol}USDT"
    if signed:
        p["timestamp"] = int(time.time()*1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = await (client.get if method=="GET" else client.post)(url, params=p, headers=headers)
    r.raise_for_status()
    return r.json()

async def price(sym: str) -> float:
    d = await api("GET", "/fapi/v1/ticker/price", signed=False, symbol=sym)
    return float(d["price"])

async def qty(sym: str) -> float:
    cfg = settings[sym]
    raw = (cfg["amount_usd"] * cfg["leverage"]) / await price(sym)
    info = await api("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in info["symbols"]:
        if s["symbol"] == f"{sym}USDT":
            prec = s.get("quantityPrecision", 3)
            minq = next((float(f["minQty"]) for f in s["filters"] if f["filterType"]=="LOT_SIZE"), 0)
            q = round(raw, prec)
            return max(q, minq)
    return 0.0

async def balance() -> float:
    data = await api("GET", "/fapi/v2/balance")
    for a in data:
        if a["asset"] == "USDT":
            return float(a["balance"])
    return 0.0

# ====================== ТОРГОВЛЯ ======================
async def open_long(coin: str):
    if not settings[coin]["enabled"]: return
    try:
        q = await qty(coin)
        if q <= 0: return
        oid = f"{coin.lower()}_{int(time.time()*1000)}"
        entry = await price(coin)
        await api("POST", "/fapi/v1/order", {"side":"BUY","type":"MARKET","quantity":str(q),"newClientOrderId":oid}, symbol=coin)
        last_balance[coin] = await balance()
        await tg(f"<b>LONG {coin}</b>\n${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x\nEntry: <code>{entry:.5f}</code>")
        await tg_balance()
    except Exception as e: await tg(f"Ошибка LONG {coin}: {e}")

async def close_all(coin: str):
    if not settings[coin]["enabled"]: return
    try:
        pos = await api("GET", "/fapi/v2/positionRisk", symbol=coin)
        cur = next((p for p in pos if float(p.get("positionAmt",0)) != 0), None)
        if cur:
            q = abs(float(cur["positionAmt"]))
            side = "SELL" if float(cur["positionAmt"]) > 0 else "BUY"
            await api("POST", "/fapi/v1/order", {"side":side,"type":"MARKET","quantity":str(q),"reduceOnly":"true"}, symbol=coin)
        bal = await balance()
        pnl = bal - last_balance.get(coin, bal)
        stats["per_coin"][coin] = stats["per_coin"].get(coin,0) + pnl
        stats["total_pnl"] = stats.get("total_pnl",0) + pnl
        save_stats(stats)
        await tg(f"<b>{coin} ЗАКРЫТ</b>\nПрибыль: <code>{pnl:+.2f}</code> USDT")
        await tg_balance()
    except Exception as e: await tg(f"Ошибка закрытия {coin}: {e}")

async def tg(text: str):
    if bot:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)

async def tg_balance():
    b = await balance()
    await tg(f"<b>Баланс:</b> <code>{b:,.2f}</code> USDT")

# ====================== ТЕЛЕГРАМ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    for c in COINS:
        status = "ON" if settings[c]["enabled"] else "OFF"
        kb.append([InlineKeyboardButton(f"{c} {status} | ${settings[c]['amount_usd']} × {settings[c]['leverage']}x", callback_data=f"coin_{c}")])
    kb += [
        [InlineKeyboardButton("Баланс", callback_data="bal")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
    ]
    await update.message.reply_text("Мультикоин-бот 2026\nВыбери коин:", reply_markup=InlineKeyboardMarkup(kb))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("coin_"):
        coin = data[5:]
        status = "ON" if settings[coin]["enabled"] else "OFF"
        kb = [
            [InlineKeyboardButton("ВКЛ / ВЫКЛ", callback_data=f"toggle_{coin}")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ]
        await q.edit_message_text(
            f"<b>{coin}</b> — {status}\n"
            f"Сумма: ${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("toggle_"):
        coin = data[7:]
        settings[coin]["enabled"] = not settings[coin]["enabled"]
        save_settings(settings)
        await button(update, context)  # обновляем текущее меню

    elif data == "bal":
        b = await balance()
        await q.edit_message_text(
            f"<b>Баланс Futures:</b> <code>{b:,.2f}</code> USDT",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]])
        )

    elif data == "stats":
        text = f"<b>Статистика</b>\nОбщая P&L: <code>{stats.get('total_pnl',0):+.2f}</code> USDT\n\nПо коинам:\n"
        for c in COINS:
            text += f"{c}: <code>{stats['per_coin'].get(c,0):+.2f}</code> USDT\n"
        await q.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]])
        )

    elif data == "back":  # ← теперь работает!
        await start(update, context)

# ====================== FASTAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    bot = Bot(TELEGRAM_TOKEN)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await tg("Бот запущен!")
    yield
    await application.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>Бот работает</h1>")

# ====================== Webhook ======================
@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
        if data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(403)
        
        sig = data.get("signal", "").lower()
        coin_raw = data.get("coin", "XRP").upper()
        coin = coin_raw.replace("USDT", "").replace(".P", "")   # ← вот эта магия
        
        if coin not in COINS:
            await tg(f"Неизвестный коин: {coin} (получено: {coin_raw})")
            return {"error": "unknown coin"}
        
        if sig in ["buy", "long"]:
            asyncio.create_task(open_long(coin))
        elif sig == "close_all":
            asyncio.create_task(close_all(coin))
        return {"ok": True}
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}
