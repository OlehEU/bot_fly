# main.py — ТЕРМИНАТОР 2026 PATCHED
# Требует окружения:
# TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET, WEBHOOK_SECRET
# Optional: SCANNER_URL (https://scanner-fly-oz.fly.dev), BOT_URL (https://bot-fly-oz.fly.dev)

import os
import json
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
from typing import Dict, Optional, Any
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("terminator")

# ========== Environment checks ==========
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # used both by bot and scanner (shared secret)

SCANNER_BASE = os.getenv("SCANNER_BASE", "https://scanner-fly-oz.fly.dev")
BOT_BASE = os.getenv("BOT_BASE", "https://bot-fly-oz.fly.dev")

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]
SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"
SCANNER_CONFIG_FILE = "scanner_config.json"

# ========== Simple atomic write helper ==========
import tempfile
def atomic_write_json(path: str, data: Any):
    dirpath = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dirpath, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    os.replace(tmp, path)

# ========== Settings & stats ==========
def load_settings() -> Dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for coin in COINS:
            if coin not in data:
                data[coin] = {"amount_usd": 10, "leverage": 10, "enabled": True, "disable_tpsl": True}
        return data
    except Exception:
        default = {
            "XRP": {"amount_usd": 10, "leverage": 10, "enabled": True, "disable_tpsl": True},
            "SOL": {"amount_usd": 15, "leverage": 20, "enabled": False, "disable_tpsl": True},
            "ETH": {"amount_usd": 20, "leverage": 5, "enabled": False, "disable_tpsl": True},
            "BTC": {"amount_usd": 50, "leverage": 3, "enabled": False, "disable_tpsl": True},
            "DOGE": {"amount_usd": 5, "leverage": 50, "enabled": False, "disable_tpsl": True},
        }
        atomic_write_json(SETTINGS_FILE, default)
        return default

def save_settings(s: Dict):
    atomic_write_json(SETTINGS_FILE, s)

def load_stats() -> Dict:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        default = {"total_pnl": 0.0, "per_coin": {c: 0.0 for c in COINS}}
        atomic_write_json(STATS_FILE, default)
        return default

def save_stats(s: Dict):
    atomic_write_json(STATS_FILE, s)

settings = load_settings()
stats = load_stats()

# ========== Global client ==========
client = httpx.AsyncClient(timeout=30.0)

last_balance: Dict[str, float] = {}
bot: Optional[Bot] = None
scanner_status = {"online": False, "last_seen": 0, "enabled": True}

# ========== Binance low-level helpers ==========
BASE = "https://fapi.binance.com"

def sign(p: Dict) -> str:
    q = urllib.parse.urlencode({k: str(v).lower() if isinstance(v,bool) else str(v) for k,v in p.items() if v is not None})
    return hmac.new(BINANCE_API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

async def api(method: str, ep: str, params: Dict = None, signed=True, symbol: Optional[str] = None):
    url = f"{BASE}{ep}"
    p = params.copy() if params else {}
    if symbol:
        p["symbol"] = f"{symbol}USDT"
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

# ========== Trading operations (safety checks, dedupe) ==========
_order_id_lock = asyncio.Lock()
_sent_order_ids = set()  # in-memory dedupe; for persistence consider DB

async def open_long(coin: str):
    if not settings[coin]["enabled"]:
        log.info("Торговля отключена для %s", coin)
        return
    try:
        q = await qty(coin)
        if q <= 0:
            log.warning("Q <= 0 для %s", coin)
            return
        # build client order id
        oid = f"{coin.lower()}_{int(time.time()*1000)}"
        # dedupe
        async with _order_id_lock:
            if oid in _sent_order_ids:
                log.warning("Duplicate oid %s", oid)
                return
            _sent_order_ids.add(oid)
        entry = await price(coin)
        # place order
        await api("POST", "/fapi/v1/order", {"side":"BUY","type":"MARKET","quantity":str(q),"newClientOrderId":oid}, symbol=coin)
        last_balance[coin] = await balance()
        await tg(f"LONG {coin}\n${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x\nEntry: <code>{entry:.5f}</code>")
        await tg_balance()
    except Exception:
        log.exception("Ошибка open_long %s", coin)
        await tg(f"Ошибка LONG {coin}: проверь логи")

async def close_all(coin: str):
    if not settings[coin]["enabled"]:
        log.info("Закрытие отключено для %s", coin)
        return
    try:
        pos = await api("GET", "/fapi/v2/positionRisk", symbol=coin)
        cur = next((p for p in pos if abs(float(p.get("positionAmt",0))) > 0), None)
        if cur:
            q = abs(float(cur["positionAmt"]))
            side = "SELL" if float(cur["positionAmt"]) > 0 else "BUY"
            # try market reduce-only
            try:
                await api("POST", "/fapi/v1/order", {"side":side,"type":"MARKET","quantity":str(q),"reduceOnly":"true"}, symbol=coin)
            except Exception:
                log.exception("reduceOnly order failed, trying plain market")
                await api("POST", "/fapi/v1/order", {"side":side,"type":"MARKET","quantity":str(q)}, symbol=coin)
        bal = await balance()
        pnl = bal - last_balance.get(coin, bal)
        stats["per_coin"][coin] = stats["per_coin"].get(coin,0) + pnl
        stats["total_pnl"] = stats.get("total_pnl",0) + pnl
        save_stats(stats)
        await tg(f"{coin} ЗАКРЫТ\nПрибыль: <code>{pnl:+.2f}</code> USDT")
        await tg_balance()
    except Exception:
        log.exception("Ошибка close_all %s", coin)
        await tg(f"Ошибка закрытия {coin}: проверь логи")

# ========== Telegram helpers ==========
async def tg(text: str):
    if bot:
        try:
            await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            log.exception("tg send failed")

async def tg_balance():
    try:
        b = await balance()
        await tg(f"Баланс: <code>{b:,.2f}</code> USDT")
    except Exception:
        log.exception("tg_balance failed")

# ========== Scanner config helpers ==========
def load_scanner_config() -> Dict:
    try:
        with open(SCANNER_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        default = {"XRP": "3m", "SOL": "5m", "ETH": "15m", "BTC": "15m", "DOGE": "1m"}
        atomic_write_json(SCANNER_CONFIG_FILE, default)
        return default

async def show_scanner_status(query_or_update):
    try:
        r = await client.get(f"{BOT_BASE}/scanner_status", timeout=5)
        status = r.json()
        config = load_scanner_config()
    except Exception:
        status = {"online": False, "enabled": False, "last_seen_seconds_ago": 999}
        config = {}
    tf_text = "\n".join([f"{c}: <b>{config.get(c, '—')}</b>" for c in COINS])
    text = (
        f"<b>СКАНЕР OZ 2026</b>\n\n"
        f"Статус: {'ОНЛАЙН' if status.get('online') else 'ОФФЛАЙН'}\n"
        f"Режим: {'ВКЛЮЧЁН' if status.get('enabled') else 'ВЫКЛЮЧЕН'}\n"
        f"Пинг: {status.get('last_seen_seconds_ago', 0)} сек назад\n\n"
        f"<b>Таймфреймы:</b>\n{tf_text}\n\n"
        f"Торговля: {'АКТИВНА' if status.get('enabled') and status.get('online') else 'ОСТАНОВЛЕНА'}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("XRP", callback_data="tf_XRP"), InlineKeyboardButton("SOL", callback_data="tf_SOL")],
        [InlineKeyboardButton("ETH", callback_data="tf_ETH"), InlineKeyboardButton("BTC", callback_data="tf_BTC")],
        [InlineKeyboardButton("DOGE", callback_data="tf_DOGE")],
        [InlineKeyboardButton("ВЫКЛ СКАНЕР" if status.get('enabled') else "ВКЛ СКАНЕР", callback_data="toggle_scanner")],
        [InlineKeyboardButton("Назад", callback_data="back")]
    ])
    if hasattr(query_or_update, "edit_message_text"):
        await query_or_update.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

# ========== Telegram UI handlers (unchanged logic, but set_tf calls protected) ==========
# ... (start, button_handler etc) - keep same as before, but when invoking scanner URL include header

# For brevity include only the modified part where we call scanner set_tf:
async def send_set_tf_to_scanner(coin: str, tf: str):
    url = f"{SCANNER_BASE}/set_tf"
    headers = {"X-Scanner-Secret": f"Bearer {WEBHOOK_SECRET}"}
    try:
        await client.post(url, json={"coin": coin, "tf": tf}, headers=headers, timeout=5)
    except Exception:
        log.exception("Не удалось послать set_tf на сканер")

# in your CallbackQuery handler replace direct client.post(...) with send_set_tf_to_scanner(...)

# ========== Lifespan / FastAPI app ==========
from contextlib import asynccontextmanager
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
    await tg("ТЕРМИНАТОР 2026 АКТИВИРОВАН")
    yield
    await application.stop()

app = FastAPI(lifespan=lifespan)

# ========== API for scanner (protected) ==========
@app.post("/scanner_ping")
async def scanner_ping(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    scanner_status["online"] = True
    scanner_status["last_seen"] = int(time.time())
    return {"ok": True}

@app.post("/toggle_scanner")
async def toggle_scanner(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    scanner_status["enabled"] = not scanner_status["enabled"]
    await tg(f"СКАНЕР OZ теперь {'ВКЛЮЧЁН' if scanner_status['enabled'] else 'ВЫКЛЮЧЕН'}")
    return {"enabled": scanner_status["enabled"]}

@app.get("/scanner_status")
async def get_scanner_status():
    ago = int(time.time()) - scanner_status.get("last_seen", 0)
    if ago > 120:
       scanner_status["online"] = False
    return {"online": scanner_status["online"], "enabled": scanner_status["enabled"], "last_seen_seconds_ago": ago}

@app.post("/set_tf")
async def set_tf(req: Request):
    # local /set_tf UI for external callers — protect with header
    auth = req.headers.get("X-Scanner-Secret") or req.headers.get("Authorization")
    if auth != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    data = await req.json()
    coin = data.get("coin")
    tf = data.get("tf")
    allowed = {"1m","3m","5m","15m","30m","45m","1h"}
    if coin in COINS and tf in allowed:
        config = load_scanner_config()
        config[coin] = tf
        atomic_write_json(SCANNER_CONFIG_FILE, config)
        await tg(f"{coin} → таймфрейм изменён на <b>{tf}</b>")
        return {"ok": True}
    return {"error": "invalid"}

# ========== webhook to receive signals ==========
@app.post("/webhook")
async def webhook(req: Request):
    # Expect Authorization: Bearer <WEBHOOK_SECRET>
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    data = await req.json()
    sig = data.get("signal", "").lower()
    coin = data.get("coin", "XRP").upper().replace("USDT", "").replace(".P", "")
    if coin not in COINS:
        return {"error": "unknown coin"}
    if sig in ["buy", "long"]:
        asyncio.create_task(open_long(coin))
    elif sig == "close_all":
        asyncio.create_task(close_all(coin))
    return {"ok": True}
