# =========================================================================================
# OZ BOT 2026 v1.9.7 | ПОЛНАЯ ВЕРСИЯ | ВСЕ ФУНКЦИИ ВКЛЮЧЕНЫ
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# --- НАСТРОЙКИ (ENV) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL", "").rstrip('/')

AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "1.0"))
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", "1.5"))
TS_START_RATE = float(os.getenv("TS_START_RATE", "0.2"))
PNL_MONITOR_INTERVAL = 20

client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"
trade_lock = asyncio.Lock()

# Состояние
symbol_precision, price_precision = {}, {}
active_longs, active_shorts = set() , set()
active_trailing_enabled = True
take_profit_enabled = True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# --- БАЗА ДАННЫХ И СОХРАНЕНИЕ НАСТРОЕК ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)')
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ts_enabled', 1), ('tp_enabled', 1)")
        conn.commit()

def load_settings():
    global active_trailing_enabled, take_profit_enabled
    try:
        with sqlite3.connect(DB_PATH) as conn:
            ts = conn.execute("SELECT value FROM settings WHERE key='ts_enabled'").fetchone()
            tp = conn.execute("SELECT value FROM settings WHERE key='tp_enabled'").fetchone()
            active_trailing_enabled = bool(ts[0]) if ts else True
            take_profit_enabled = bool(tp[0]) if tp else True
    except: pass

def save_setting(key, value):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE settings SET value=? WHERE key=?", (int(value), key))
        conn.commit()

def log_trade_result(symbol, side, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)",
                     (symbol, side, round(pnl, 3), datetime.now()))
        conn.commit()

# --- СТАТИСТИКА ---
def get_stats_report(days):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            since = datetime.now() - timedelta(days=days)
            res = conn.execute("SELECT SUM(pnl), COUNT(id) FROM trades WHERE timestamp >= ?", (since,)).fetchone()
            total_pnl, count = (res[0] or 0), (res[1] or 0)
            coin_stats = conn.execute("SELECT symbol, SUM(pnl) FROM trades WHERE timestamp >= ? GROUP BY symbol ORDER BY SUM(pnl) DESC", (since,)).fetchall()
            
            period = {1: "СУТКИ", 7: "НЕДЕЛЮ", 30: "МЕСЯЦ"}.get(days, f"{days} дн.")
            msg = f"📊 <b>ОТЧЕТ ЗА {period}</b>\n💰 Итог: {round(total_pnl, 2)} USDT\n📦 Сделок: {count}\n"
            if coin_stats:
                msg += "\n<b>📈 По монетам:</b>\n"
                for s, p in coin_stats:
                    msg += f"• {s}: {round(p, 2)}\n"
            return msg
    except Exception as e: return f"Ошибка БД: {e}"

# --- BINANCE API ---
async def binance(method, path, params=None, signed=True):
    url = BASE + path
    p = params.copy() if params else {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        query = "&".join([f"{k}={v}" for k, v in sorted(p.items())])
        sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        url += f"?{query}&signature={sig}"
        p = None
    r = await client.request(method, url, params=p, headers={"X-MBX-APIKEY": API_KEY})
    return r.json()

async def sync_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p['symbol'] for p in data if float(p['positionAmt']) > 0}
        active_shorts = {p['symbol'] for p in data if float(p['positionAmt']) < 0}

# --- ТОРГОВАЯ ЛОГИКА ---
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts):
            await tg_bot.send_message(CHAT_ID, f"⚠️ {symbol} уже открыт, игнорирую.")
            return

        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        try: await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
        except: pass
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data: return
        
        price = float(p_data["price"])
        qty = round((AMOUNT * LEV) / price, 3)
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": str(qty)})
        
        if res.get("orderId"):
            active_longs.add(symbol) if side == "LONG" else active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side}</b>\nМонета: {symbol}\nЦена: {price}", parse_mode="HTML")
            
            await asyncio.sleep(2) # Задержка для регистрации позиции
            close_side = "SELL" if side == "LONG" else "BUY"

            if take_profit_enabled:
                tp_p = round(price * (1 + TAKE_PROFIT_RATE/100 if side == "LONG" else 1 - TAKE_PROFIT_RATE/100), 4)
                await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", "stopPrice": str(tp_p), "closePosition": "true"})
                await tg_bot.send_message(CHAT_ID, f"🎯 Тейк: {tp_p}")

            if active_trailing_enabled:
                act = round(price * (1 + TS_START_RATE/100 if side == "LONG" else 1 - TS_START_RATE/100), 4)
                await binance("POST", "/fapi/v1/algoOrder", {"algoType":"CONDITIONAL","symbol":symbol,"side":close_side,"positionSide":side,"type":"TRAILING_STOP_MARKET","quantity":str(qty),"callbackRate":TRAILING_RATE,"activationPrice":str(act)})
                await tg_bot.send_message(CHAT_ID, f"📉 Трейлинг: {TRAILING_RATE}%")
        else:
            await tg_bot.send_message(CHAT_ID, f"❌ Ошибка входа: {res.get('msg')}")

# --- МОНИТОРИНГ PNL ---
async def pnl_monitor():
    while True:
        await asyncio.sleep(PNL_MONITOR_INTERVAL)
        try:
            data = await binance("GET", "/fapi/v2/positionRisk")
            if not isinstance(data, list): continue
            current = {p['symbol'] + p['positionSide'] for p in data if abs(float(p['positionAmt'])) > 0}
            for s in list(active_longs):
                if (s + "LONG") not in current:
                    active_longs.discard(s); asyncio.create_task(report_pnl(s, "LONG"))
            for s in list(active_shorts):
                if (s + "SHORT") not in current:
                    active_shorts.discard(s); asyncio.create_task(report_pnl(s, "SHORT"))
        except: pass

async def report_pnl(symbol, side):
    await asyncio.sleep(5)
    trades = await binance("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": 5})
    if isinstance(trades, list):
        pnl = sum(float(t.get('realizedPnl', 0)) - float(t.get('commission', 0)) for t in trades)
        log_trade_result(symbol, side, pnl)
        await tg_bot.send_message(CHAT_ID, f"{'✅' if pnl > 0 else '🛑'} <b>ЗАКРЫТО: {symbol}</b>\nДоход: {round(pnl, 2)} USDT", parse_mode="HTML")

# --- WEB & TG ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); load_settings()
    await sync_positions()
    asyncio.create_task(pnl_monitor())
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    await tg_bot.send_message(CHAT_ID, "🟢 Бот запущен. Настройки и позиции синхронизированы.")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health(): return {"status": "ok"}

@app.post("/tg")
async def tg_webhook(request: Request):
    global active_trailing_enabled, take_profit_enabled
    data = await request.json()
    upd = Update.de_json(data, tg_bot)
    if not upd.message and not upd.callback_query: return {"ok": True}
    
    if upd.message:
        t = upd.message.text
        if t == "/start":
            await tg_bot.send_message(CHAT_ID, "Меню:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📊 Статистика"), KeyboardButton("📦 Позиции")], [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔄 Обновить")]], resize_keyboard=True))
        elif t == "📊 Статистика":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("День", callback_data="st_1"), InlineKeyboardButton("Неделя", callback_data="st_7")], [InlineKeyboardButton("Месяц", callback_data="st_30")]])
            await tg_bot.send_message(CHAT_ID, "Выберите период:", reply_markup=kb)
        elif t == "📦 Позиции":
            data = await binance("GET", "/fapi/v2/positionRisk")
            msg = ""
            for p in data:
                if float(p['positionAmt']) != 0:
                    msg += f"<b>{p['symbol']}</b> {p['positionSide']}\nPnL: {round(float(p['unRealizedProfit']), 2)} USDT\n\n"
            await tg_bot.send_message(CHAT_ID, msg or "Нет активных позиций", parse_mode="HTML")
        elif t == "⚙️ Настройки":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Трейлинг: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"Тейк: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
            await tg_bot.send_message(CHAT_ID, "Настройки сохранены:", reply_markup=kb)
        elif t == "🔄 Обновить":
            await sync_positions()
            await tg_bot.send_message(CHAT_ID, "✅ Синхронизация завершена")

    elif upd.callback_query:
        q = upd.callback_query
        if q.data.startswith("st_"):
            await q.edit_message_text(get_stats_report(int(q.data.split("_")[1])), parse_mode="HTML")
        elif q.data == "t_ts":
            active_trailing_enabled = not active_trailing_enabled
            save_setting('ts_enabled', active_trailing_enabled)
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Трейлинг: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"Тейк: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]]))
        elif q.data == "t_tp":
            take_profit_enabled = not take_profit_enabled
            save_setting('tp_enabled', take_profit_enabled)
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Трейлинг: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"Тейк: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]]))
    return {"ok": True}

@app.post("/webhook")
async def signal_webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": 403}
    data = await request.json()
    sig, sym = data.get("signal", "").upper(), data.get("symbol", "").upper()
    if sig and sym:
        await tg_bot.send_message(CHAT_ID, f"📩 Сигнал: {sym} {sig}")
        if sig in ["LONG", "SHORT"]: asyncio.create_task(open_pos(sym, sig))
    return {"ok": True}
