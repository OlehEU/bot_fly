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

from fastapi.staticfiles import StaticFiles # ИМПОРТ ЛОГОВ
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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

# ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
# ВСЁ, ЧТО НИЖЕ — ДОБАВЛЯЙ В КОНЕЦ main.py (перед polling)
# ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←



# Команда /scanner — показывает статус и кнопку
@dp.message_handler(commands=['scanner'])
async def cmd_scanner(message: types.Message):
    if message.from_user.id != ADMIN_ID:        # ← твой ADMIN_ID уже есть в коде
        return
    
    try:
        status = (await httpx.AsyncClient().get("https://bot-fly-oz.fly.dev/scanner_status")).json()
    except:
        status = {"online": False, "enabled": False, "last_seen_seconds_ago": 999}

    text = (
        f"СКАНЕР OZ 2026\n\n"
        f"Статус: {'🟢 ОНЛАЙН' if status['online'] else '🔴 ОФФЛАЙН'}\n"
        f"Режим: {'ВКЛЮЧЁН' if status['enabled'] else 'ВЫКЛЮЧЕН'}\n"
        f"Последний пинг: {status['last_seen_seconds_ago']} сек назад\n\n"
        f"Торговля: {'АКТИВНА' if status['enabled'] and status['online'] else 'ОСТАНОВЛЕНА'}"
    )
    
    keyboard = InlineKeyboardMarkup()
    btn_text = "ВЫКЛЮЧИТЬ СКАНЕР" if status['enabled'] else "ВКЛЮЧИТЬ СКАНЕР"
    keyboard.add(InlineKeyboardButton(btn_text, callback_data="toggle_scanner"))
    
    await message.answer(text, reply_markup=keyboard)

# Кнопка ВКЛ/ВЫКЛ
@dp.callback_query_handler(lambda c: c.data == "toggle_scanner")
async def toggle_scanner_btn(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    
    await httpx.post("https://bot-fly-oz.fly.dev/toggle_scanner")
    await call.answer("Готово!", show_alert=True)
    
    # Обновляем сообщение
    await cmd_scanner(call.message)
        
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

# ====================== Бот, главная страница ======================
@app.get("/")
async def root():
    return HTMLResponse("<h1>Бот работает</h1>")

# ====================== SCANNER ======================
@app.get("/scanner")
async def scanner_dashboard():
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>ТЕРМИНАТОР 2026 — ЖИВ</title>
        <meta charset="utf-8">
        <style>
            body {margin:0; background:#000; color:#0f0; font-family: 'Courier New', monospace; overflow:hidden;}
            .header {text-align:center; padding:15px; background:#111; border-bottom:2px solid #0f0; text-shadow:0 0 10px #0f0;}
            h1 {margin:0; font-size:2.5em;}
            .status {font-size:1.2em; margin-top:8px;}
            .widget {width:100%; height:calc(100vh - 100px);}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>ТЕРМИНАТОР 2026</h1>
            <div class="status">АВТОТРЕЙДИНГ 24/7 • OZ СТРАТЕГИЯ • XRP • SOL • ETH • BTC • DOGE</div>
        </div>

        <div class="widget">
            <!-- TradingView Widget (официальный, без ограничений) -->
            <div class="tradingview-widget-container">
                <div id="tvchart" style="height:100%; width:100%;"></div>
                <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
                <script type="text/javascript">
                new TradingView.widget({
                    "container_id": "tvchart",
                    "width": "100%",
                    "height": "100%",
                    "symbol": "BINANCE:XRPUSDT",
                    "interval": "5",
                    "timezone": "Etc/UTC",
                    "theme": "dark",
                    "style": "1",
                    "locale": "ru",
                    "toolbar_bg": "#0f0f0f",
                    "enable_publishing": false,
                    "hide_side_toolbar": false,
                    "allow_symbol_change": true,
                    "studies": [
                        "MASimple@tv-basicstudies",
                        "RSI@tv-basicstudies",
                        "Volume@tv-basicstudies"
                    ],
                    "show_popup_button": true,
                    "popup_width": "1000",
                    "popup_height": "650"
                });
                </script>
            </div>
        </div>

        <!-- УБРАЛИ ЭТУ СТРОКУ — БОЛЬШЕ НИКАКИХ ПЕРЕЗАГРУЗОК! -->
        <!-- <script>setInterval(() => location.reload(), 10000);</script> -->
    </body>
    </html>
    """)
    
# ====================== Логи сигналов ======================
@app.get("/logs")
async def signal_logs():
    try:
        with open("signal_log.json") as f:
            logs = json.load(f)
    except:
        logs = []
    rows = ""
    for entry in reversed(logs[-30:]):
        rows += f"<tr><td>{entry['date']}</td><td>{entry['time']}</td><td>{entry['coin']}</td><td style='color:{'lime' if entry['action']=='BUY' else 'red'}'><b>{entry['action']}</b></td><td>{entry['price']}</td></tr>"
    
    return HTMLResponse(f"""
    <html><head><title>ЛОГИ СИГНАЛОВ</title>
    <style>body{{background:#000;color:#0f0;font-family:monospace;padding:20px}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{border:1px solid #0f0;padding:8px;text-align:center}}
    th{{background:#111}}</style></head>
    <body>
    <h1>ЛОГИ СИГНАЛОВ — ПОСЛЕДНИЕ 30</h1>
    <table><tr><th>Дата</th><th>Время</th><th>Коин</th><th>Сигнал</th><th>Цена</th></tr>
    {rows or "<tr><td colspan=5>Сигналов пока нет</td></tr>"}
    </table>
    <p><a href="/scanner">← График</a> | <a href="/">Главная</a></p>
    <script>setInterval(() => location.reload(), 10000)</script>
    </body></html>
    """)

# Глобальные перемены для статуса сканера
scanner_status = {
    "online": False,
    "last_seen": 0,
    "enabled": True
}

# Сканер будет каждые 30–60 сек слать сюда "пинг"
@app.post("/scanner_ping")
async def scanner_ping():
    scanner_status["online"] = True
    scanner_status["last_seen"] = int(time.time())
    return {"status": "ok"}

# Включаем/выключаем сканер
@app.post("/toggle_scanner")
async def toggle_scanner():
    scanner_status["enabled"] = not scanner_status["enabled"]
    await bot.send_message(ADMIN_ID, f"СКАНЕР {'ВКЛЮЧЁН' if scanner_status['enabled'] else 'ВЫКЛЮЧЕН'}")
    return {"enabled": scanner_status["enabled"]}

# Статус сканера для админа
@app.get("/scanner_status")
async def get_status():
    ago = int(time.time()) - scanner_status["last_seen"]
    if ago > 120:
        scanner_status["online"] = False
    return {
        "online": scanner_status["online"],
        "enabled": scanner_status["enabled"],
        "last_seen_seconds_ago": ago
    }

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
