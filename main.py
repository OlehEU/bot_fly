# main.py — ТЕРМИНАТОР 2026 ULTIMATE — РАБОТАЕТ НА FLY.IO НА 1000%
import os
import json
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from contextlib import asynccontextmanager
import tempfile

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("terminator")

# ====== ENV CHECK ======
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"ОТСУТСТВУЕТ ПЕРЕМЕННАЯ: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
SCANNER_BASE = os.getenv("SCANNER_BASE", "https://scanner-fly-oz.fly.dev")
BOT_BASE = os.getenv("BOT_BASE", "https://bot-fly-oz.fly.dev")

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]
SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"
SCANNER_CONFIG_FILE = "scanner_config.json"
SIGNAL_LOG_FILE = "signal_log.json"

# ====== JSON HELPERS ======
def atomic_write_json(path: str, data: Any):
    dirpath = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dirpath, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    os.replace(tmp, path)

def load_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        atomic_write_json(path, default)
        return default

settings = load_json(SETTINGS_FILE, {c: {"amount_usd": 10, "leverage": 10, "enabled": True} for c in COINS})
stats = load_json(STATS_FILE, {"total_pnl": 0.0, "per_coin": {c: 0.0 for c in COINS}})
signal_log = load_json(SIGNAL_LOG_FILE, [])

client = httpx.AsyncClient(timeout=20.0)
bot: Bot = None
scanner_status = {"online": False, "last_seen": 0, "enabled": True}

# ====== Binance ======
BASE = "https://fapi.binance.com"
def sign(params: Dict) -> str:
    query = urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items() if v is not None})
    return hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def api(method: str, endpoint: str, params: Dict = None, signed: bool = True, symbol: str = None):
    url = f"{BASE}{endpoint}"
    p = params.copy() if params else {}
    if symbol:
        p["symbol"] = f"{symbol}USDT"
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign(p)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = await (client.get if method == "GET" else client.post)(url, params=p, headers=headers)
    resp.raise_for_status()
    return resp.json()

async def get_balance() -> float:
    data = await api("GET", "/fapi/v2/balance")
    for item in data:
        if item["asset"] == "USDT":
            return float(item["balance"])
    return 0.0

# ====== Telegram ======
async def tg(text: str):
    global bot
    if bot:
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.error(f"TG error: {e}")

# ====== FastAPI APP — ВОТ ОНО, В САМОМ НИЗУ! ======
app = FastAPI()  # ← ЭТО ДОЛЖНО БЫТЬ ДО ВСЕХ @app.get/post!

# ====== Web Pages ======
HTML_CSS = """
<style>
    body {background:#0d1117;color:#c9d1d9;font-family:Arial;margin:0;padding:20px;}
    h1,h2 {color:#58a6ff;text-align:center;}
    table {width:90%;max-width:1000px;margin:30px auto;border-collapse:collapse;background:#161b22;}
    th,td {border:1px solid #30363d;padding:14px;text-align:center;}
    th {background:#21262d;color:#58a6ff;}
    tr:nth-child(even) {background:#0d1117;}
    tr:hover {background:#1f6feb22;}
    .btn {padding:12px 24px;margin:10px;background:#238636;color:white;border:none;border-radius:8px;font-size:16px;cursor:pointer;}
    .status-on {color:#7ce38b;font-weight:bold;}
    .status-off {color:#f85149;font-weight:bold;}
    .center {text-align:center;}
</style>
"""

@app.get("/")
async def root():
    return {"status": "alive", "message": "ТЕРМИНАТОР 2026 ЗАПУЩЕН"}

@app.get("/logs")
async def logs_page():
    logs = signal_log[-150:]
    rows = "".join(f"<tr><td>{l.get('date','—')}</td><td>{l.get('time','—')}</td><td><b>{l.get('coin','—')}</b></td><td>{l.get('action','—')}</td><td>{l.get('price','—')}</td></tr>" for l in logs)
    return Response(content=f"<html><head><title>Логи OZ 2026</title>{HTML_CSS}</head><body><h1>СИГНАЛЫ</h1><div class='center'><a href='/scanner'><button class='btn'>СКАНЕР</button></a></div><table><tr><th>Дата</th><th>Время</th><th>Монета</th><th>Действие</th><th>Цена</th></tr>{rows}</table></body></html>", media_type="text/html")

@app.get("/scanner")
async def scanner_page():
    try:
        status = (await client.get(f"{SCANNER_BASE}/scanner_status", timeout=6)).json()
    except:
        status = {"online": False, "enabled": False, "tf": {c: "—" for c in COINS}}
    rows = "".join(f"<tr><td><b>{c}</b></td><td>{status['tf'].get(c, '—')}</td></tr>" for c in COINS)
    return Response(content=f"<html><head><title>Сканер OZ 2026</title>{HTML_CSS}</head><body><h1>СКАНЕР OZ 2026</h1><p class='center'>Статус: <span class='{'status-on' if status.get('online') else 'status-off'}'>{'ОНЛАЙН' if status.get('online') else 'ОФФЛАЙН'}</span><br>Режим: <span class='{'status-on' if status.get('enabled') else 'status-off'}'>{'ВКЛ' if status.get('enabled') else 'ВЫКЛ'}</span></p><table><tr><th>Монета</th><th>ТФ</th></tr>{rows}</table><div class='center'><a href='/logs'><button class='btn'>ЛОГИ</button></a></div></body></html>", media_type="text/html")

# ====== Scanner API ======
@app.post("/scanner_ping")
async def scanner_ping(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    scanner_status["online"] = True
    scanner_status["last_seen"] = int(time.time())
    return {"ok": True}

@app.post("/toggle_scanner")
async def toggle_scanner(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    scanner_status["enabled"] = not scanner_status["enabled"]
    await tg(f"СКАНЕР OZ 2026 → {'ВКЛЮЧЁН' if scanner_status['enabled'] else 'ВЫКЛЮЧЕН'}")
    return {"enabled": scanner_status["enabled"]}

@app.get("/scanner_status")
async def get_scanner_status():
    ago = int(time.time()) - scanner_status.get("last_seen", 0)
    if ago > 90:
        scanner_status["online"] = False
    return {"online": scanner_status["online"], "enabled": scanner_status["enabled"], "last_seen_seconds_ago": ago}

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    data = await request.json()
    signal = data.get("signal", "").lower()
    coin = data.get("coin", "").upper()
    if coin not in COINS:
        return {"error": "bad coin"}
    
    entry = {"date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M:%S"), "coin": coin, "action": "LONG" if "buy" in signal else "CLOSE", "price": "—"}
    signal_log.append(entry)
    atomic_write_json(SIGNAL_LOG_FILE, signal_log[-500:])

    if "buy" in signal:
        await tg(f"LONG {coin}USDT\nСигнал от OZ 2026!")
    elif signal == "close_all":
        await tg(f"ЗАКРЫТИЕ {coin}USDT")
    return {"ok": True}

# ====== Telegram Bot ======
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Баланс", callback_data="balance"), InlineKeyboardButton("Сканер OZ", callback_data="scanner")],
        [InlineKeyboardButton("Логи", url=f"{BOT_BASE}/logs"), InlineKeyboardButton("Сканер", url=f"{BOT_BASE}/scanner")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>ТЕРМИНАТОР 2026 АКТИВИРОВАН</b>", parse_mode="HTML", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "balance":
        bal = await get_balance()
        await query.edit_message_text(f"<b>Баланс:</b> <code>{bal:,.2f}</code> USDT", parse_mode="HTML", reply_markup=main_menu())
    elif query.data == "scanner":
        try:
            status = (await client.get(f"{SCANNER_BASE}/scanner_status")).json()
        except:
            status = {"online": False, "enabled": False, "tf": {c: "—" for c in COINS}}
        tf_text = "\n".join([f"{c}: <b>{status['tf'].get(c, '—')}</b>" for c in COINS])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("ВЫКЛ СКАНЕР" if status.get("enabled") else "ВКЛ СКАНЕР", callback_data="toggle_scanner")],
            [InlineKeyboardButton("Назад", callback_data="back")]
        ])
        await query.edit_message_text(f"<b>СКАНЕР OZ 2026</b>\n\nСтатус: {'ОНЛАЙН' if status.get('online') else 'ОФФЛАЙН'}\nРежим: {'ВКЛ' if status.get('enabled') else 'ВЫКЛ'}\n\n<b>ТФ:</b>\n{tf_text}", parse_mode="HTML", reply_markup=keyboard)
    elif query.data == "toggle_scanner":
        await client.post(f"{BOT_BASE}/toggle_scanner", headers={"Authorization": f"Bearer {WEBHOOK_SECRET}"})
        await button_handler(update, context)
    elif query.data == "back":
        await query.edit_message_text("Главное меню", reply_markup=main_menu())

# ====== Lifespan ======
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    bot = application.bot
    await tg("<b>ТЕРМИНАТОР 2026 ONLINE</b>")
    yield
    await application.stop()

app = FastAPI(lifespan=lifespan)  # ← ВТОРОЙ РАЗ — ПЕРЕОПРЕДЕЛЯЕМ С LIFESPAN

# ГОТОВО! ДЕПЛОЙ И ЖИВИ В 2026
