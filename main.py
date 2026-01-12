# =========================================================================================
# OZ TRADING BOT 2025 v1.7.5 | ULTIMATE STABLE & FLY.IO READY
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
from typing import Dict, Set, List
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL", "").rstrip('/')

# Параметры (берутся из переменных Fly.io или дефолтные)
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
active_longs, active_shorts = set(), set()
active_trailing_enabled, take_profit_enabled = True, True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# --- DATABASE & STATS ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)''')
        conn.commit()

def log_trade_result(symbol, side, pnl):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)",
                         (symbol, side, round(pnl, 3), datetime.now()))
            conn.commit()
    except Exception as e: logger.error(f"DB Log Error: {e}")

def get_stats_report(days):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            since = datetime.now() - timedelta(days=days)
            # Общий итог
            res = conn.execute("SELECT SUM(pnl), COUNT(id) FROM trades WHERE timestamp >= ?", (since,)).fetchone()
            total_pnl, count = (res[0] or 0), (res[1] or 0)
            
            # Детализация по монетам
            coin_stats = conn.execute("""SELECT symbol, SUM(pnl), COUNT(id) FROM trades 
                                         WHERE timestamp >= ? GROUP BY symbol ORDER BY SUM(pnl) DESC""", (since,)).fetchall()
            
            # Последние 5 сделок
            recent = conn.execute("SELECT symbol, side, pnl FROM trades WHERE timestamp >= ? ORDER BY id DESC LIMIT 5", (since,)).fetchall()
            
            period = {1: "СУТКИ", 7: "НЕДЕЛЮ", 30: "МЕСЯЦ"}.get(days, f"{days} ДНЕЙ")
            msg = f"📊 <b>ОТЧЕТ ЗА {period}</b>\n💰 Итог: <b>{total_pnl:+.2f} USDT</b>\n📦 Сделок: <code>{count}</code>\n"
            
            if coin_stats:
                msg += "\n<b>📈 По монетам:</b>\n"
                for s, p, c in coin_stats: msg += f"• {s}: <code>{p:+.2f}</code> ({c})\n"
            
            if recent:
                msg += "\n<b>🕒 Последние сделки:</b>\n"
                for s, side, p in recent:
                    icon = "🟢" if p > 0 else "🔴"
                    msg += f"{icon} {s} | {side} | <code>{p:+.2f}</code>\n"
            return msg
    except Exception as e: return f"Ошибка статистики: {e}"

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
    try:
        r = await client.request(method, url, params=p, headers={"X-MBX-APIKEY": API_KEY})
        return r.json()
    except Exception as e: return {"error": str(e)}

async def load_exchange_info():
    global symbol_precision, price_precision
    data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    if 'symbols' in data:
        for s in data['symbols']:
            sym = s['symbol']
            lot = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
            prc = next(f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')
            symbol_precision[sym] = len(lot['stepSize'].rstrip('0').split('.')[-1]) if '.' in lot['stepSize'] else 0
            price_precision[sym] = len(prc['tickSize'].rstrip('0').split('.')[-1]) if '.' in prc['tickSize'] else 0

async def sync_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p['symbol'] for p in data if float(p['positionAmt']) > 0}
        active_shorts = {p['symbol'] for p in data if float(p['positionAmt']) < 0}

def fix_qty(s, q): return f"{q:.{symbol_precision.get(s, 3)}f}".rstrip("0").rstrip(".")
def fix_price(s, pr): return f"{pr:.{price_precision.get(s, 8)}f}".rstrip("0").rstrip(".")

# --- TRADE LOGIC ---
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts): return

        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        try: await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
        except: pass
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data: return
        price = float(p_data["price"])
        qty = fix_qty(symbol, (AMOUNT * LEV) / price)
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": qty})
        
        if res.get("orderId"):
            if side == "LONG": active_longs.add(symbol)
            else: active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side}: {symbol}</b>\nЦена: <code>{price}</code>", parse_mode="HTML")
            
            # Микро-задержка для синхронизации Binance
            await asyncio.sleep(1.5)
            close_side = "SELL" if side == "LONG" else "BUY"

            # TAKE PROFIT
            if take_profit_enabled:
                tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
                tp_res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", "stopPrice": fix_price(symbol, tp_p), "closePosition": "true"})
                if tp_res.get("orderId"):
                    await tg_bot.send_message(CHAT_ID, f"🎯 <b>TP установлен:</b> <code>{fix_price(symbol, tp_p)}</code>", parse_mode="HTML")

            # TRAILING STOP
            if active_trailing_enabled:
                act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
                ts_res = await binance("POST", "/fapi/v1/algoOrder", {"algoType":"CONDITIONAL","symbol":symbol,"side":close_side,"positionSide":side,"type":"TRAILING_STOP_MARKET","quantity":qty,"callbackRate":TRAILING_RATE,"activationPrice":fix_price(symbol,act)})
                if ts_res.get("orderId") or "algoOrderId" in str(ts_res):
                    await tg_bot.send_message(CHAT_ID, f"📉 <b>Trailing активен:</b> {TRAILING_RATE}%", parse_mode="HTML")

async def close_pos(sym, side):
    symbol = sym.upper().replace("/", "") + "USDT" if "USDT" not in sym.upper() else sym.upper()
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    qty = next((abs(float(p["positionAmt"])) for p in data if p["positionSide"] == side), 0)
    if qty > 0:
        await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side, "type": "MARKET", "quantity": fix_qty(symbol, qty)})

# --- TELEGRAM HANDLERS ---
def get_main_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("📊 Статистика"), KeyboardButton("📦 Позиции")], [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔄 Обновить")]], resize_keyboard=True)

async def handle_tg(update_json):
    global active_trailing_enabled, take_profit_enabled
    try:
        upd = Update.de_json(update_json, tg_bot)
        if not upd or not upd.effective_chat: return
        chat_id = upd.effective_chat.id

        if upd.message and upd.message.text:
            t = upd.message.text
            if t == "/start":
                await tg_bot.send_message(chat_id, "<b>OZ Bot v1.7.5 Online</b>", parse_mode="HTML", reply_markup=get_main_kb())
            elif t == "📊 Статистика":
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("День", callback_data="st_1"), InlineKeyboardButton("Неделя", callback_data="st_7")], [InlineKeyboardButton("Месяц", callback_data="st_30")]])
                await tg_bot.send_message(chat_id, "Выберите период:", reply_markup=kb)
            elif t == "📦 Позиции":
                data = await binance("GET", "/fapi/v2/positionRisk")
                msg = "\n\n".join([f"<b>{p['symbol']}</b> ({p['positionSide']})\nPnL: {float(p['unRealizedProfit']):+.2f}" for p in data if float(p['positionAmt']) != 0])
                await tg_bot.send_message(chat_id, msg or "📭 Нет позиций", parse_mode="HTML")
            elif t == "⚙️ Настройки":
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
                await tg_bot.send_message(chat_id, "Настройки (клик для переключения):", reply_markup=kb)
            elif t == "🔄 Обновить":
                await load_exchange_info(); await sync_positions()
                await tg_bot.send_message(chat_id, "✅ Синхронизировано")

        elif upd.callback_query:
            q = upd.callback_query
            if q.data.startswith("st_"):
                await q.edit_message_text(get_stats_report(int(q.data.split("_")[1])), parse_mode="HTML")
            elif q.data == "t_ts":
                active_trailing_enabled = not active_trailing_enabled
                await q.answer("Trailing изменен"); await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]]))
            elif q.data == "t_tp":
                take_profit_enabled = not take_profit_enabled
                await q.answer("Take Profit изменен"); await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]]))
    except Exception as e: logger.error(f"TG Handler Error: {e}")

# --- MONITORING ---
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
        icon = "✅" if pnl > 0 else "🛑"
        await tg_bot.send_message(CHAT_ID, f"{icon} <b>ЗАКРЫТО: {symbol}</b>\nЧистый PnL: <b>{pnl:+.2f} USDT</b>", parse_mode="HTML")

# --- FASTAPI APP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Фоновая задача, чтобы деплой не ждал ответа от биржи
    asyncio.create_task(load_exchange_info())
    asyncio.create_task(sync_positions())
    asyncio.create_task(pnl_monitor())
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health(): return {"status": "ok", "version": "1.7.5"}

@app.post("/tg")
async def tg_webhook(request: Request):
    data = await request.json()
    asyncio.create_task(handle_tg(data))
    return {"ok": True}

@app.post("/webhook")
async def signal_webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": 403}
    data = await request.json()
    sig, sym = data.get("signal", "").upper(), data.get("symbol", "").upper()
    if sig == "LONG": asyncio.create_task(open_pos(sym, "LONG"))
    elif sig == "SHORT": asyncio.create_task(open_pos(sym, "SHORT"))
    elif "CLOSE" in sig: asyncio.create_task(close_pos(sym, "LONG" if "LONG" in sig else "SHORT"))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
