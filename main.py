# main.py — УЛЬТИМАТИВНАЯ ВЕРСИЯ 2026: управление таймфреймами + всё остальное
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

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]

SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"
SCANNER_CONFIG_FILE = "scanner_config.json"

# ====================== НАСТРОЙКИ ======================
def load_settings() -> Dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        default_coin = {"amount_usd": 10, "leverage": 10, "enabled": True, "disable_tpsl": True}
        for coin in COINS:
            if coin not in data:
                data[coin] = default_coin.copy()
        return data
    except Exception:
        default = {c: {"amount_usd": 10, "leverage": 10, "enabled": True, "disable_tpsl": True} for c in COINS}
        default["XRP"]["amount_usd"], default["XRP"]["leverage"] = 10, 10
        default["SOL"]["amount_usd"], default["SOL"]["leverage"] = 15, 20
        default["ETH"]["amount_usd"], default["ETH"]["leverage"] = 20, 5
        default["BTC"]["amount_usd"], default["BTC"]["leverage"] = 50, 3
        default["DOGE"]["amount_usd"], default["DOGE"]["leverage"] = 5, 50
        save_settings(default)
        return default

def save_settings(s: Dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

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
        json.dump(s, f, indent=2, ensure_ascii=False)

settings = load_settings()
stats = load_stats()

# ====================== ГЛОБАЛЬНЫЕ ======================
client = httpx.AsyncClient(timeout=60.0)
last_balance: Dict[str, float] = {}
bot: Optional[Bot] = None
scanner_status = {"online": False, "last_seen": 0, "enabled": True}

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
    except Exception as e:
        await tg(f"Ошибка LONG {coin}: {e}")

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
    except Exception as e:
        await tg(f"Ошибка закрытия {coin}: {e}")

async def tg(text: str):
    if bot:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)

async def tg_balance():
    b = await balance()
    await tg(f"<b>Баланс:</b> <code>{b:,.2f}</code> USDT")

# ====================== СКАНЕР СТАТУС + ТАЙМФРЕЙМЫ ======================
async def get_scanner_config():
    try:
        with open(SCANNER_CONFIG_FILE) as f:
            return json.load(f)
    except:
        default = {"XRP": "3m", "SOL": "5m", "ETH": "15m", "BTC": "15m", "DOGE": "1m"}
        with open(SCANNER_CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default

async def show_scanner_status(query_or_update):
    try:
        status = (await client.get("https://bot-fly-oz.fly.dev/scanner_status")).json()
        config = await get_scanner_config()
    except:
        status = {"online": False, "enabled": False, "last_seen_seconds_ago": 999}
        config = {}

    tf_text = "\n".join([f"{coin}: <b>{config.get(coin, '—')}</b>" for coin in COINS])

    text = (
        f"<b>СКАНЕР OZ 2026</b>\n\n"
        f"Статус: {'🟢 ОНЛАЙН' if status['online'] else '🔴 ОФФЛАЙН'}\n"
        f"Режим: {'ВКЛЮЧЁН' if status['enabled'] else 'ВЫКЛЮЧЕН'}\n"
        f"Пинг: {status['last_seen_seconds_ago']} сек назад\n\n"
        f"<b>Таймфреймы:</b>\n{tf_text}\n\n"
        f"Торговля: {'АКТИВНА' if status['enabled'] and status['online'] else 'ОСТАНОВЛЕНА'}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("XRP", callback_data="tf_XRP"), InlineKeyboardButton("SOL", callback_data="tf_SOL")],
        [InlineKeyboardButton("ETH", callback_data="tf_ETH"), InlineKeyboardButton("BTC", callback_data="tf_BTC")],
        [InlineKeyboardButton("DOGE", callback_data="tf_DOGE")],
        [InlineKeyboardButton("ВЫКЛ СКАНЕР" if status['enabled'] else "ВКЛ СКАНЕР", callback_data="toggle_scanner")],
        [InlineKeyboardButton("Назад", callback_data="back")]
    ])

    if hasattr(query_or_update, "edit_message_text"):
        await query_or_update.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

# ====================== ТЕЛЕГРАМ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    for c in COINS:
        status = "ON" if settings[c]["enabled"] else "OFF"
        kb.append([InlineKeyboardButton(f"{c} {status} | ${settings[c]['amount_usd']} × {settings[c]['leverage']}x", callback_data=f"coin_{c}")])
    kb += [
        [InlineKeyboardButton("Баланс", callback_data="bal")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
        [InlineKeyboardButton("СКАНЕР OZ", callback_data="scanner_menu")],
    ]
    await update.message.reply_text("Мультикоин-бот 2026\nВыбери действие:", reply_markup=InlineKeyboardMarkup(kb))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Управление таймфреймами
    if data.startswith("tf_"):
        coin = data[3:]
        kb = [
            [InlineKeyboardButton("1m", callback_data=f"settf_{coin}_1m")],
            [InlineKeyboardButton("3m", callback_data=f"settf_{coin}_3m")],
            [InlineKeyboardButton("5m", callback_data=f"settf_{coin}_5m")],
            [InlineKeyboardButton("15m", callback_data=f"settf_{coin}_15m")],
            [InlineKeyboardButton("30m", callback_data=f"settf_{coin}_30m")],
            [InlineKeyboardButton("1h", callback_data=f"settf_{coin}_1h")],
            [InlineKeyboardButton("Назад", callback_data="scanner_menu")],
        ]
        await query.edit_message_text(f"Выбери таймфрейм для <b>{coin}</b>:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("settf_"):
        parts = data.split("_")
        coin = parts[1]
        tf = parts[2]
        await client.post("https://scanner-fly-oz.fly.dev/set_tf", json={"coin": coin, "tf": tf})
        await tg(f"{coin} → таймфрейм <b>{tf}</b>")
        await show_scanner_status(query)
        return

    # Остальные кнопки
    if data.startswith("coin_"):
        coin = data[5:]
        kb = [[InlineKeyboardButton("ВКЛ / ВЫКЛ", callback_data=f"toggle_{coin}")], [InlineKeyboardButton("Назад", callback_data="back")]]
        await query.edit_message_text(f"<b>{coin}</b> — {'ON' if settings[coin]['enabled'] else 'OFF'}\nСумма: ${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("toggle_"):
        coin = data[7:]
        settings[coin]["enabled"] = not settings[coin]["enabled"]
        save_settings(settings)
        await button_handler(update, context)
    elif data == "bal":
        b = await balance()
        await query.edit_message_text(f"<b>Баланс Futures:</b> <code>{b:,.2f}</code> USDT", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))
    elif data == "stats":
        text = f"<b>Статистика</b>\nОбщая P&L: <code>{stats.get('total_pnl',0):+.2f}</code> USDT\n\nПо коинам:\n"
        for c in COINS:
            text += f"{c}: <code>{stats['per_coin'].get(c,0):+.2f}</code> USDT\n"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))
    elif data == "scanner_menu":
        await show_scanner_status(query)
    elif data == "toggle_scanner":
        await client.post("https://bot-fly-oz.fly.dev/toggle_scanner")
        await show_scanner_status(query)
    elif data == "back":
        await start(update, context)

# ====================== LIFESPAN ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    bot = Bot(TELEGRAM_TOKEN)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await tg("ТЕРМИНАТОР 2026 ЗАПУЩЕН")
    yield
    await application.stop()

app = FastAPI(lifespan=lifespan)

# ====================== API ДЛЯ СКАНЕРА ======================
@app.post("/scanner_ping")
async def scanner_ping():
    scanner_status["online"] = True
    scanner_status["last_seen"] = int(time.time())
    return {"ok": True}

@app.post("/toggle_scanner")
async def toggle_scanner():
    scanner_status["enabled"] = not scanner_status["enabled"]
    await tg(f"СКАНЕР OZ теперь {'ВКЛЮЧЁН' if scanner_status['enabled'] else 'ВЫКЛЮЧЕН'}")
    return {"enabled": scanner_status["enabled"]}

@app.get("/scanner_status")
async def get_scanner_status():
    ago = int(time.time()) - scanner_status["last_seen"]
    if ago > 120:
        scanner_status["online"] = False
    return {"online": scanner_status["online"], "enabled": scanner_status["enabled"], "last_seen_seconds_ago": ago}

@app.post("/set_tf")
async def set_tf(req: Request):
    data = await req.json()
    coin = data.get("coin")
    tf = data.get("tf")
    if coin in COINS and tf in ["1m","3m","5m","15m","30m","1h"]:
        config = await get_scanner_config()
        config[coin] = tf
        with open(SCANNER_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        await tg(f"{coin} → таймфрейм изменён на <b>{tf}</b>")
        return {"ok": True}
    return {"error": "invalid"}

# ====================== ВЕБ-СТРАНИЦЫ ======================
@app.get("/")
async def root():
    return HTMLResponse("<h1>ТЕРМИНАТОР 2026 — РАБОТАЕТ 24/7</h1><p><a href='/scanner'>График</a> | <a href='/logs'>Логи</a></p>")

@app.get("/scanner")
async def scanner_dashboard():
    return HTMLResponse("""твой красивый TradingView дашборд — оставь как есть""")

@app.get("/logs")
async def signal_logs():
    # твой код логов — оставь как есть
    pass

# ====================== WEBHOOK ======================
@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
        if data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(403)
        sig = data.get("signal", "").lower()
        coin = data.get("coin", "XRP").upper().replace("USDT", "").replace(".P", "")
        if coin not in COINS:
            return {"error": "unknown coin"}
        if sig in ["buy", "long"]:
            asyncio.create_task(open_long(coin))
        elif sig == "close_all":
            asyncio.create_task(close_all(coin))
        return {"ok": True}
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}
