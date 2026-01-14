# =========================================================================================
# OZ TRADING BOT 2026 v1.7.8 | FINAL STABLE VERSION
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio, math
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# --- CONFIG ---
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

client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"
trade_lock = asyncio.Lock()

prec_qty, prec_price = {}, {}
active_longs, active_shorts = set(), set()
config = {"tp": True, "ts": True} # Загрузится из БД

tg_bot = Bot(token=TELEGRAM_TOKEN)

# ==================== DATABASE & SETTINGS ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)')
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
        conn.execute('INSERT OR IGNORE INTO settings VALUES ("tp", 1)')
        conn.execute('INSERT OR IGNORE INTO settings VALUES ("ts", 1)')
        conn.commit()

def load_settings():
    global config
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for key in ["tp", "ts"]:
                val = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                if val is not None: config[key] = bool(val[0])
    except: pass

def save_setting(key, val):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, int(val)))
        conn.commit()

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        today = datetime.now().strftime('%Y-%m-%d')
        total = conn.execute("SELECT SUM(pnl), COUNT(*) FROM trades").fetchone()
        daily = conn.execute("SELECT SUM(pnl), COUNT(*) FROM trades WHERE date(timestamp) = ?", (today,)).fetchone()
        return {"t_pnl": total[0] or 0, "t_cnt": total[1] or 0, "d_pnl": daily[0] or 0, "d_cnt": daily[1] or 0}

# ==================== BINANCE API ====================
async def binance(method, path, params=None, signed=True):
    url = BASE + path
    p = params.copy() if params else {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000
        query = "&".join([f"{k}={v}" for k, v in sorted(p.items())])
        sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        url += f"?{query}&signature={sig}"
        p = None
    try:
        r = await client.request(method, url, params=p, headers={"X-MBX-APIKEY": API_KEY})
        return r.json()
    except Exception as e: return {"error": str(e)}

async def load_exchange_info():
    global prec_qty, prec_price
    data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    if isinstance(data, dict) and 'symbols' in data:
        for s in data['symbols']:
            sym = s['symbol']
            lot = next((f for f in s['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            tick = next((f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if lot: prec_qty[sym] = int(round(-math.log10(float(lot['stepSize']))))
            if tick: prec_price[sym] = int(round(-math.log10(float(tick['tickSize']))))

async def sync_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p['symbol'] for p in data if float(p['positionAmt']) > 0}
        active_shorts = {p['symbol'] for p in data if float(p['positionAmt']) < 0}

def fix_qty(s, q):
    p = prec_qty.get(s, 3)
    return f"{math.floor(q * 10**p) / 10**p:.{p}f}"

def fix_price(s, pr):
    p = prec_price.get(s, 2)
    return f"{round(pr, p):.{p}f}"

# ==================== TRADE LOGIC ====================
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts): return
        
        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data: return
        
        price = float(p_data["price"])
        qty = fix_qty(symbol, (AMOUNT * LEV) / price)
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": qty})
        
        if res.get("orderId"):
            if side == "LONG": active_longs.add(symbol)
            else: active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side} {symbol}</b>\nЦена: {price}", parse_mode="HTML")
            
            await asyncio.sleep(1.2)
            close_side = "SELL" if side == "LONG" else "BUY"
            
            # Algo-TP Fix
            if config["tp"]:
                tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
                tp_res = await binance("POST", "/fapi/v1/algoOrder", {
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "positionSide": side,
                    "type": "TAKE_PROFIT_MARKET", "quantity": qty, "stopPrice": fix_price(symbol, tp_p)
                })
                if "orderId" in str(tp_res) or "algoOrderId" in str(tp_res):
                    await tg_bot.send_message(CHAT_ID, f"🎯 TP установлен: <code>{fix_price(symbol, tp_p)}</code>", parse_mode="HTML")
                else:
                    await tg_bot.send_message(CHAT_ID, f"⚠️ Ошибка TP: {tp_res.get('msg', 'Error')}")

            # Trailing Stop
            if config["ts"]:
                await asyncio.sleep(0.5)
                act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
                ts_res = await binance("POST", "/fapi/v1/algoOrder", {
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "positionSide": side,
                    "type": "TRAILING_STOP_MARKET", "quantity": qty, "callbackRate": TRAILING_RATE, "activationPrice": fix_price(symbol, act)
                })
                if "orderId" in str(ts_res) or "algoOrderId" in str(ts_res):
                    await tg_bot.send_message(CHAT_ID, f"📉 Trailing активен: {TRAILING_RATE}%", parse_mode="HTML")
        else:
            await tg_bot.send_message(CHAT_ID, f"❌ Ошибка входа: {res.get('msg')}")

# ==================== TG HANDLER ====================
async def handle_tg_logic(update_json):
    global config
    try:
        upd = Update.de_json(update_json, tg_bot)
        if upd.callback_query:
            q = upd.callback_query
            key = "ts" if q.data == "t_ts" else "tp"
            config[key] = not config[key]
            save_setting(key, config[key])
            ikb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if config['ts'] else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if config['tp'] else '❌'}", callback_data="t_tp")]])
            await q.edit_message_reply_markup(reply_markup=ikb)
            await q.answer("Настройка сохранена")
            return

        if not upd.message or not upd.message.text: return
        t, cid = upd.message.text, upd.message.chat_id
        main_kb = ReplyKeyboardMarkup([[KeyboardButton("📦 Позиции"), KeyboardButton("📈 Статистика")], [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔄 Обновить")]], resize_keyboard=True)

        if t == "/start":
            await tg_bot.send_message(cid, "OZ Bot v1.7.8 Ready", reply_markup=main_kb)

        elif "Позиции" in t:
            data = await binance("GET", "/fapi/v2/positionRisk")
            if isinstance(data, list):
                active = [p for p in data if float(p['positionAmt']) != 0]
                if not active:
                    await tg_bot.send_message(cid, "📂 <b>Активных позиций нет</b>", parse_mode="HTML")
                else:
                    msg = "📂 <b>Текущие позиции:</b>\n\n"
                    for p in active:
                        side = "LONG 🟢" if float(p['positionAmt']) > 0 else "SHORT 🔴"
                        msg += f"• <b>{p['symbol']}</b> ({side})\n  PnL: <code>{float(p['unRealizedProfit']):+.2f} USDT</code>\n\n"
                    await tg_bot.send_message(cid, msg, parse_mode="HTML")

        elif "Статистика" in t:
            s = get_stats()
            msg = f"📊 <b>Сегодня:</b> {s['d_pnl']:.2f} USDT ({s['d_cnt']})\n<b>Всего:</b> {s['t_pnl']:.2f} USDT ({s['t_cnt']})"
            await tg_bot.send_message(cid, msg, parse_mode="HTML")

        elif "Обновить" in t:
            await load_exchange_info(); await sync_positions()
            await tg_bot.send_message(cid, "✅ Данные синхронизированы")

        elif "Настройки" in t:
            ikb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if config['ts'] else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if config['tp'] else '❌'}", callback_data="t_tp")]])
            await tg_bot.send_message(cid, "⚙️ <b>Защита (сохранено в БД):</b>", reply_markup=ikb, parse_mode="HTML")

    except Exception as e: logging.error(f"TG Error: {e}")

# ==================== WEB APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); load_settings(); await load_exchange_info(); await sync_positions()
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    await tg_bot.send_message(CHAT_ID, "🟢 <b>OZ Bot v1.7.8 Запущен</b>", parse_mode="HTML")
    yield

app = FastAPI(lifespan=lifespan)
@app.get("/")
async def health(): return {"status": "ok"}
@app.post("/tg")
async def tg_webhook(request: Request):
    data = await request.json()
    asyncio.create_task(handle_tg_logic(data))
    return {"ok": True}
@app.post("/webhook")
async def signal_webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": 403}
    d = await request.json()
    if d.get("signal") in ["LONG", "SHORT"]: asyncio.create_task(open_pos(d.get("symbol"), d.get("signal")))
    return {"ok": True}
