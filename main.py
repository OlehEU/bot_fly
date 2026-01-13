# =========================================================================================
# OZ TRADING BOT 2026 v1.7.0 | STATS & REFRESH FIXED
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

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

symbol_precision, price_precision = {}, {}
active_longs, active_shorts = set(), set()
active_trailing_enabled, take_profit_enabled = True, True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# ==================== DATABASE ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)')
        conn.commit()

def log_trade(symbol, side, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)", (symbol, side, round(pnl, 3), datetime.now()))
        conn.commit()

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        # Статистика за сегодня и за всё время
        today = datetime.now().strftime('%Y-%m-%d')
        total = conn.execute("SELECT SUM(pnl), COUNT(*) FROM trades").fetchone()
        daily = conn.execute("SELECT SUM(pnl), COUNT(*) FROM trades WHERE date(timestamp) = ?", (today,)).fetchone()
        return {
            "total_pnl": total[0] or 0, "total_count": total[1] or 0,
            "daily_pnl": daily[0] or 0, "daily_count": daily[1] or 0
        }

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
    global symbol_precision, price_precision
    data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    if isinstance(data, dict) and 'symbols' in data:
        for s in data['symbols']:
            sym = s['symbol']
            try:
                lot = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
                tick = next(f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')
                symbol_precision[sym] = lot['stepSize'].find('1') - lot['stepSize'].find('.') if '.' in lot['stepSize'] else 0
                price_precision[sym] = tick['tickSize'].find('1') - tick['tickSize'].find('.') if '.' in tick['tickSize'] else 0
            except: pass

async def sync_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p['symbol'] for p in data if float(p['positionAmt']) > 0}
        active_shorts = {p['symbol'] for p in data if float(p['positionAmt']) < 0}

def fix_qty(s, q): return f"{q:.{abs(symbol_precision.get(s, 3))}f}".rstrip("0").rstrip(".")
def fix_price(s, pr): return f"{pr:.{abs(price_precision.get(s, 4))}f}".rstrip("0").rstrip(".")

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
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side} {symbol}</b>", parse_mode="HTML")
            await asyncio.sleep(1.5)
            close_side = "SELL" if side == "LONG" else "BUY"
            if take_profit_enabled:
                tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
                await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", "stopPrice": fix_price(symbol, tp_p), "closePosition": "true", "workingType": "MARK_PRICE"})
            if active_trailing_enabled:
                act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
                await binance("POST", "/fapi/v1/algoOrder", {"algoType":"CONDITIONAL", "symbol":symbol, "side":close_side, "positionSide":side, "type":"TRAILING_STOP_MARKET", "quantity":qty, "callbackRate":TRAILING_RATE, "activationPrice":fix_price(symbol, act)})

# ==================== TG HANDLER ====================
async def handle_tg_logic(update_json):
    global active_trailing_enabled, take_profit_enabled
    try:
        upd = Update.de_json(update_json, tg_bot)
        if not upd.message or not upd.message.text: 
            if upd.callback_query: # Обработка инлайн кнопок настроек
                q = upd.callback_query
                if q.data == "t_ts": active_trailing_enabled = not active_trailing_enabled
                if q.data == "t_tp": take_profit_enabled = not take_profit_enabled
                new_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
                await q.edit_message_reply_markup(reply_markup=new_kb)
                await q.answer("Обновлено")
            return

        t = upd.message.text
        cid = upd.message.chat_id
        
        # ГЛАВНОЕ МЕНЮ (кнопки внизу)
        main_kb = ReplyKeyboardMarkup([
            [KeyboardButton("📦 Позиции"), KeyboardButton("📈 Статистика")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔄 Обновить")]
        ], resize_keyboard=True)

        if t == "/start":
            await tg_bot.send_message(cid, "OZ Bot v1.7 Готов", reply_markup=main_kb)

        elif t == "📦 Позиции":
            data = await binance("GET", "/fapi/v2/positionRisk")
            msg = "\n".join([f"<b>{p['symbol']}</b>: {float(p['unRealizedProfit']):+.2f}" for p in data if float(p['positionAmt']) != 0])
            await tg_bot.send_message(cid, msg or "Нет позиций", parse_mode="HTML")

        elif t == "📈 Статистика":
            s = get_stats()
            msg = (f"📊 <b>ОТЧЕТ ПО СДЕЛКАМ</b>\n\n"
                   f"📅 <b>За сегодня:</b>\n"
                   f"└ Профит: <code>{s['daily_pnl']:.2f} USDT</code>\n"
                   f"└ Сделок: <code>{s['daily_count']}</code>\n\n"
                   f"🌍 <b>Всего:</b>\n"
                   f"└ Профит: <code>{s['total_pnl']:.2f} USDT</code>\n"
                   f"└ Сделок: <code>{s['total_count']}</code>")
            await tg_bot.send_message(cid, msg, parse_mode="HTML")

        elif t == "🔄 Обновить":
            await load_exchange_info()
            await sync_positions()
            await tg_bot.send_message(cid, "✅ Данные биржи синхронизированы")

        elif t == "⚙️ Настройки":
            ikb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
            await tg_bot.send_message(cid, "Управление защитой:", reply_markup=ikb)

    except Exception as e: logging.error(f"TG Error: {e}")

# ==================== WEB APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); await load_exchange_info(); await sync_positions()
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
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
