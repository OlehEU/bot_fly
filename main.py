# =========================================================================================
# OZ BOT 2026 v1.10.0 | TOTAL PROTECTION | FINAL
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# --- НАСТРОЙКИ ---
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
PNL_MONITOR_INTERVAL = 30

client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"
trade_lock = asyncio.Lock()

# Состояние
symbol_precision, price_precision = {}, {}
active_longs, active_shorts = set(), set()
active_trailing_enabled = True
take_profit_enabled = True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# --- БД ---
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

# --- API ---
async def binance(method, path, params=None, signed=True):
    url = BASE + path
    p = params.copy() if params else {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        query = "&".join([f"{k}={v}" for k, v in sorted(p.items())])
        sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        url += f"?{query}&signature={sig}"
        p = None
    try:
        r = await client.request(method, url, params=p, headers={"X-MBX-APIKEY": API_KEY})
        data = r.json()
        if isinstance(data, dict) and "code" in data and data["code"] != 200:
            logging.error(f"Binance Error: {data}")
        return data
    except Exception as e:
        return {"error": str(e)}

async def load_exchange_info():
    global symbol_precision, price_precision
    data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    if isinstance(data, dict) and 'symbols' in data:
        for s in data['symbols']:
            sym = s['symbol']
            try:
                lot = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
                step = lot['stepSize'].find('1') - lot['stepSize'].find('.')
                symbol_precision[sym] = max(0, step) if '.' in lot['stepSize'] else 0
                tick = next(f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')
                p_step = tick['tickSize'].find('1') - tick['tickSize'].find('.')
                price_precision[sym] = max(0, p_step) if '.' in tick['tickSize'] else 0
            except: pass

async def sync_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p['symbol'] for p in data if float(p.get('positionAmt', 0)) > 0}
        active_shorts = {p['symbol'] for p in data if float(p.get('positionAmt', 0)) < 0}

def fix_qty(sym, q):
    prec = symbol_precision.get(sym, 3)
    return format(q, f".{prec}f").rstrip('0').rstrip('.')

def fix_price(sym, p):
    prec = price_precision.get(sym, 4)
    return format(p, f".{prec}f").rstrip('0').rstrip('.')

# --- ТОРГОВАЯ ЛОГИКА ---
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        await sync_positions()
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts):
            return

        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data: return
        
        price = float(p_data["price"])
        qty = fix_qty(symbol, (AMOUNT * LEV) / price)
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": qty})
        
        if res.get("orderId"):
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side}</b>\n{symbol} по {price}", parse_mode="HTML")
            close_side = "SELL" if side == "LONG" else "BUY"

            if take_profit_enabled:
                tp_val = price * (1 + TAKE_PROFIT_RATE/100 if side == "LONG" else 1 - TAKE_PROFIT_RATE/100)
                await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", "stopPrice": fix_price(symbol, tp_val), "closePosition": "true"})

            if active_trailing_enabled:
                act_val = price * (1 + TS_START_RATE/100 if side == "LONG" else 1 - TS_START_RATE/100)
                await binance("POST", "/fapi/v1/algoOrder", {"algoType":"CONDITIONAL","symbol":symbol,"side":close_side,"positionSide":side,"type":"TRAILING_STOP_MARKET","quantity":qty,"callbackRate":TRAILING_RATE,"activationPrice":fix_price(symbol, act_val)})
        else:
            await tg_bot.send_message(CHAT_ID, f"❌ Ошибка API: {res.get('msg', 'Unknown')}")

# --- WEB & TG ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); load_settings()
    await load_exchange_info()
    await sync_positions()
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    await tg_bot.send_message(CHAT_ID, "🟢 Бот v1.10 запущен")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health(): return {"status": "ok"}

@app.post("/tg")
async def tg_webhook(request: Request):
    global active_trailing_enabled, take_profit_enabled
    data = await request.json()
    upd = Update.de_json(data, tg_bot)
    if not upd or not upd.message: return {"ok": True}
    
    t = upd.message.text
    if t == "📦 Позиции":
        res = await binance("GET", "/fapi/v2/positionRisk")
        if not isinstance(res, list):
            await tg_bot.send_message(CHAT_ID, "⚠️ Ошибка связи с биржей.")
            return {"ok": True}
        
        msg = ""
        for p in res:
            try:
                amt = float(p.get('positionAmt', 0))
                if amt != 0:
                    pnl = round(float(p.get('unRealizedProfit', 0)), 2)
                    msg += f"<b>{p['symbol']}</b> {'LONG' if amt > 0 else 'SHORT'}\nPnL: {pnl} USDT\n\n"
            except: continue
        await tg_bot.send_message(CHAT_ID, msg or "Нет открытых позиций", parse_mode="HTML")
    
    elif t == "/start":
        await tg_bot.send_message(CHAT_ID, "Меню:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📦 Позиции"), KeyboardButton("⚙️ Настройки")]], resize_keyboard=True))
    
    elif t == "⚙️ Настройки":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Трейлинг: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"Тейк: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
        await tg_bot.send_message(CHAT_ID, "Настройки:", reply_markup=kb)

    return {"ok": True}

@app.post("/webhook")
async def signal_webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": 403}
    data = await request.json()
    sig, sym = data.get("signal", "").upper(), data.get("symbol", "").upper()
    if sig in ["LONG", "SHORT"]: asyncio.create_task(open_pos(sym, sig))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
