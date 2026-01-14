# =========================================================================================
# OZ TRADING BOT 2026 v1.9.0 | COMPLETE LOGIC & AUTO-CLEAN
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

client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"
trade_lock = asyncio.Lock()

prec_qty, prec_price = {}, {}
active_longs, active_shorts = set(), set()
config = {"tp": True, "ts": True}

tg_bot = Bot(token=TELEGRAM_TOKEN)

# ==================== DATABASE ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)')
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
        conn.execute('INSERT OR IGNORE INTO settings VALUES ("tp", 1), ("ts", 1)')
        conn.commit()

def log_trade(symbol, side, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)", (symbol, side, pnl, datetime.now()))
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
            sym = s['symbol']; lot = next((f for f in s['filters'] if f['filterType'] == 'LOT_SIZE'), None); tick = next((f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if lot: prec_qty[sym] = int(round(-math.log10(float(lot['stepSize']))))
            if tick: prec_price[sym] = int(round(-math.log10(float(tick['tickSize']))))

async def sync_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p['symbol'] for p in data if float(p['positionAmt']) > 0}
        active_shorts = {p['symbol'] for p in data if float(p['positionAmt']) < 0}

# ==================== TRADE LOGIC ====================
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        # ПРОВЕРКА НАЛИЧИЯ ОТКРЫТОЙ СДЕЛКИ
        if symbol in active_longs or symbol in active_shorts:
            await tg_bot.send_message(CHAT_ID, f"⚠️ <b>Сигнал пропущен</b>\nМонета {symbol} уже находится в работе.", parse_mode="HTML")
            return

        # ПРЕДВАРИТЕЛЬНАЯ ОЧИСТКА
        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data: 
            await tg_bot.send_message(CHAT_ID, f"❌ Ошибка: Не удалось получить цену {symbol}")
            return
        
        price = float(p_data["price"])
        qty = f"{math.floor(((AMOUNT * LEV) / price) * 10**prec_qty.get(symbol, 3)) / 10**prec_qty.get(symbol, 3):.{prec_qty.get(symbol, 3)}f}"
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": qty})
        
        if res.get("orderId"):
            if side == "LONG": active_longs.add(symbol)
            else: active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side}</b>\nМонета: {symbol}\nЦена: {price}\nОбъем: {qty}", parse_mode="HTML")
            
            close_side = "SELL" if side == "LONG" else "BUY"
            
            # УСТАНОВКА TP
            if config["tp"]:
                tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
                tp_res = await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", "quantity": qty, "stopPrice": f"{round(tp_p, prec_price.get(symbol, 2)):.{prec_price.get(symbol, 2)}f}"})
                if "orderId" in str(tp_res): await tg_bot.send_message(CHAT_ID, f"🎯 TP установлен: <code>{tp_p:.4f}</code>", parse_mode="HTML")
                else: await tg_bot.send_message(CHAT_ID, f"⚠️ Ошибка TP: {tp_res.get('msg', 'Algo Error')}")

            # УСТАНОВКА TRAILING
            if config["ts"]:
                ts_res = await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "positionSide": side, "type": "TRAILING_STOP_MARKET", "quantity": qty, "callbackRate": TRAILING_RATE})
                if "orderId" in str(ts_res): await tg_bot.send_message(CHAT_ID, f"📉 Trailing активен: {TRAILING_RATE}%", parse_mode="HTML")
        else:
            await tg_bot.send_message(CHAT_ID, f"❌ Ошибка открытия {symbol}: {res.get('msg')}")

# ==================== MONITORING (CLOSE CHECK) ====================
async def check_closings():
    """Раз в минуту проверяет, не закрылись ли позиции, и чистит мусор"""
    while True:
        try:
            data = await binance("GET", "/fapi/v2/positionRisk")
            if isinstance(data, list):
                current_active = {p['symbol'] for p in data if float(p['positionAmt']) != 0}
                
                # Ищем монеты, которые были активны, но исчезли
                for sym in list(active_longs | active_shorts):
                    if sym not in current_active:
                        # Позиция закрылась!
                        active_longs.discard(sym); active_shorts.discard(sym)
                        # Очистка всех оставшихся ордеров (если TP закрыл, то TS висит, и наоборот)
                        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym})
                        
                        # Получаем PnL последней сделки
                        history = await binance("GET", "/fapi/v1/userTrades", {"symbol": sym, "limit": 5})
                        last_pnl = sum(float(t['realizedPnl']) for t in history if abs(int(t['time']) - int(time.time()*1000)) < 60000)
                        
                        log_trade(sym, "CLOSED", last_pnl)
                        await tg_bot.send_message(CHAT_ID, f"🏁 <b>ЗАКРЫТО: {sym}</b>\nРезультат: <code>{last_pnl:+.2f} USDT</code>\nОрдера очищены ✅", parse_mode="HTML")
        except: pass
        await asyncio.sleep(60)

# ==================== TG HANDLER ====================
async def handle_tg_logic(update_json):
    global config
    try:
        upd = Update.de_json(update_json, tg_bot)
        if upd.callback_query:
            key = "ts" if upd.callback_query.data == "t_ts" else "tp"
            config[key] = not config[key]
            with sqlite3.connect(DB_PATH) as conn: conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, int(config[key])))
            ikb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if config['ts'] else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if config['tp'] else '❌'}", callback_data="t_tp")]])
            await upd.callback_query.edit_message_reply_markup(reply_markup=ikb)
            return

        if not upd.message or not upd.message.text: return
        t, cid = upd.message.text, upd.message.chat_id
        main_kb = ReplyKeyboardMarkup([[KeyboardButton("📦 Позиции"), KeyboardButton("📈 Статистика")], [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔄 Обновить")]], resize_keyboard=True)

        if t == "/start": await tg_bot.send_message(cid, "OZ Bot v1.9.0", reply_markup=main_kb)
        elif "Позиции" in t:
            data = await binance("GET", "/fapi/v2/positionRisk")
            msg = "\n".join([f"• <b>{p['symbol']}</b>: {float(p['unRealizedProfit']):+.2f} USDT" for p in data if float(p['positionAmt']) != 0])
            await tg_bot.send_message(cid, f"📂 <b>Текущие позиции:</b>\n{msg}" if msg else "Нет активных сделок", parse_mode="HTML")
        elif "Статистика" in t:
            s = get_stats()
            await tg_bot.send_message(cid, f"📊 <b>Статистика</b>\nЗа сегодня: {s['d_pnl']:.2f} USDT\nВсего: {s['t_pnl']:.2f} USDT", parse_mode="HTML")
        elif "Обновить" in t:
            await load_exchange_info(); await sync_positions()
            await tg_bot.send_message(cid, "✅ Данные обновлены")
        elif "Настройки" in t:
            ikb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if config['ts'] else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if config['tp'] else '❌'}", callback_data="t_tp")]])
            await tg_bot.send_message(cid, "Настройки:", reply_markup=ikb)
    except: pass

# ==================== WEB APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); await load_exchange_info(); await sync_positions()
    asyncio.create_task(check_closings()) # Фоновый мониторинг закрытий
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
