# =========================================================================================
# OZ TRADING BOT 2026 v1.6.9 | ULTIMATE STABLE VERSION
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- КОНФИГУРАЦИЯ (Берется из ENV Fly.io) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL", "").rstrip('/')

# Параметры стратегии
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

# Глобальное состояние
symbol_precision, price_precision = {}, {}
active_longs, active_shorts = set(), set()
active_trailing_enabled, take_profit_enabled = True, True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# ==================== МОДУЛЬ БАЗЫ ДАННЫХ ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)''')
        conn.commit()

def log_trade_result(symbol, side, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)",
                     (symbol, side, round(pnl, 3), datetime.now()))
        conn.commit()

# ==================== BINANCE API МЕТОДЫ ====================
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
    except Exception as e:
        logging.error(f"API Error: {e}")
        return {"error": str(e)}

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

# ==================== ТОРГОВАЯ ЛОГИКА ====================
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts):
            return

        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data: return
        
        price = float(p_data["price"])
        qty = fix_qty(symbol, (AMOUNT * LEV) / price)
        
        res = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", 
            "positionSide": side, "type": "MARKET", "quantity": qty
        })
        
        if res.get("orderId"):
            if side == "LONG": active_longs.add(symbol)
            else: active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side} {symbol}</b>\nЦена: <code>{price}</code>", parse_mode="HTML")
            
            # Пауза для регистрации позиции на бирже
            await asyncio.sleep(1.5)
            close_side = "SELL" if side == "LONG" else "BUY"

            # Установка TAKE PROFIT
            if take_profit_enabled:
                tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
                tp_res = await binance("POST", "/fapi/v1/order", {
                    "symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", 
                    "stopPrice": fix_price(symbol, tp_p), "closePosition": "true", "workingType": "MARK_PRICE"
                })
                if tp_res.get("orderId"):
                    await tg_bot.send_message(CHAT_ID, f"🎯 <b>TP установлен:</b> <code>{fix_price(symbol, tp_p)}</code>", parse_mode="HTML")

            # Установка TRAILING STOP
            if active_trailing_enabled:
                await asyncio.sleep(0.5)
                act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
                ts_res = await binance("POST", "/fapi/v1/algoOrder", {
                    "algoType":"CONDITIONAL", "symbol":symbol, "side":close_side, "positionSide":side,
                    "type":"TRAILING_STOP_MARKET", "quantity":qty, "callbackRate":TRAILING_RATE, "activationPrice":fix_price(symbol, act)
                })
                if "orderId" in str(ts_res) or "algoOrderId" in str(ts_res):
                    await tg_bot.send_message(CHAT_ID, f"📉 <b>Трейлинг активен:</b> <code>{TRAILING_RATE}%</code>", parse_mode="HTML")
        else:
            await tg_bot.send_message(CHAT_ID, f"❌ <b>Ошибка входа {symbol}:</b>\n<code>{res.get('msg')}</code>", parse_mode="HTML")

# ==================== МОНИТОРИНГ И ОТЧЕТЫ ====================
async def pnl_monitor():
    while True:
        await asyncio.sleep(PNL_MONITOR_INTERVAL)
        try:
            data = await binance("GET", "/fapi/v2/positionRisk")
            if not isinstance(data, list): continue
            current = {p['symbol'] + p['positionSide'] for p in data if abs(float(p['positionAmt'])) > 0}
            
            for s in list(active_longs):
                if (s + "LONG") not in current:
                    active_longs.discard(s)
                    asyncio.create_task(report_pnl(s, "LONG"))
            for s in list(active_shorts):
                if (s + "SHORT") not in current:
                    active_shorts.discard(s)
                    asyncio.create_task(report_pnl(s, "SHORT"))
        except: pass

async def report_pnl(symbol, side):
    await asyncio.sleep(5)
    trades = await binance("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": 5})
    if isinstance(trades, list):
        pnl = sum(float(t.get('realizedPnl', 0)) - float(t.get('commission', 0)) for t in trades)
        log_trade_result(symbol, side, pnl)
        icon = "✅" if pnl > 0 else "🛑"
        await tg_bot.send_message(CHAT_ID, f"{icon} <b>ЗАКРЫТО: {symbol}</b>\nЧистый PnL: <b>{pnl:+.2f} USDT</b>", parse_mode="HTML")

# ==================== ЛОГИКА ТЕЛЕГРАМ ====================
async def handle_tg_logic(update_json):
    global active_trailing_enabled, take_profit_enabled
    try:
        upd = Update.de_json(update_json, tg_bot)
        if upd.message and upd.message.text:
            chat_id = upd.message.chat_id
            t = upd.message.text
            
            if t == "/start":
                kb = ReplyKeyboardMarkup([[KeyboardButton("📦 Позиции"), KeyboardButton("⚙️ Настройки")]], resize_keyboard=True)
                await tg_bot.send_message(chat_id, "🤖 <b>OZ Bot v1.6.9</b> запущен!", reply_markup=kb, parse_mode="HTML")
            
            elif t == "📦 Позиции":
                data = await binance("GET", "/fapi/v2/positionRisk")
                if isinstance(data, list):
                    pos_list = [f"<b>{p['symbol']}</b> ({p['positionSide']})\nPnL: <code>{float(p['unRealizedProfit']):+.2f}</code>" 
                               for p in data if float(p['positionAmt']) != 0]
                    await tg_bot.send_message(chat_id, "\n\n".join(pos_list) if pos_list else "📭 Нет позиций", parse_mode="HTML")
            
            elif t == "⚙️ Настройки":
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"Trailing: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")],
                    [InlineKeyboardButton(f"Take Profit: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]
                ])
                await tg_bot.send_message(chat_id, "Управление ордерами:", reply_markup=kb)

        elif upd.callback_query:
            q = upd.callback_query
            if q.data == "t_ts": active_trailing_enabled = not active_trailing_enabled
            if q.data == "t_tp": take_profit_enabled = not take_profit_enabled
            
            new_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Trailing: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")],
                [InlineKeyboardButton(f"Take Profit: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]
            ])
            await q.edit_message_reply_markup(reply_markup=new_kb)
            await q.answer("Настройки сохранены")
    except Exception as e:
        logging.error(f"TG Logic Error: {e}")

# ==================== FASTAPI APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await load_exchange_info()
    await sync_positions()
    asyncio.create_task(pnl_monitor())
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    await tg_bot.send_message(CHAT_ID, "🟢 <b>Бот успешно перезапущен!</b>", parse_mode="HTML")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health_check():
    return {"status": "online", "version": "1.6.9"}

@app.post("/tg")
async def tg_webhook(request: Request):
    data = await request.json()
    asyncio.create_task(handle_tg_logic(data))
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
