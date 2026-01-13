# =========================================================================================
# OZ TRADING BOT 2026 v1.6.7 | FIXED TAKE PROFIT & STABILITY
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
import httpx
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# --- КОНФИГУРАЦИЯ ---
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
active_longs, active_shorts = set(), set()
active_trailing_enabled, take_profit_enabled = True, True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)')
        conn.commit()

def log_trade(symbol, side, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)", (symbol, side, round(pnl, 3), datetime.now()))
        conn.commit()

# --- BINANCE API ---
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
        if r.status_code == 451: return {"error": "GEO_BLOCK_451"}
        return r.json()
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

# --- ТОРГОВАЯ ЛОГИКА ---
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
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": qty})
        
        if res.get("orderId"):
            if side == "LONG": active_longs.add(symbol)
            else: active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side} {symbol}</b>\nЦена: {price}", parse_mode="HTML")
            
            # --- ПАУЗА ДЛЯ СИНХРОНИЗАЦИИ ---
            await asyncio.sleep(1.5)
            close_side = "SELL" if side == "LONG" else "BUY"

            # TAKE PROFIT
            if take_profit_enabled:
                tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
                tp_res = await binance("POST", "/fapi/v1/order", {
                    "symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", 
                    "stopPrice": fix_price(symbol, tp_p), "closePosition": "true", "workingType": "MARK_PRICE"
                })
                if tp_res.get("orderId"):
                    await tg_bot.send_message(CHAT_ID, f"🎯 TP: <code>{fix_price(symbol, tp_p)}</code>", parse_mode="HTML")
                else:
                    await tg_bot.send_message(CHAT_ID, f"⚠️ TP Error: {tp_res.get('msg')}")

            # TRAILING STOP
            if active_trailing_enabled:
                await asyncio.sleep(0.5)
                act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
                ts_res = await binance("POST", "/fapi/v1/algoOrder", {
                    "algoType":"CONDITIONAL", "symbol":symbol, "side":close_side, "positionSide":side,
                    "type":"TRAILING_STOP_MARKET", "quantity":qty, "callbackRate":TRAILING_RATE, "activationPrice":fix_price(symbol, act)
                })
                if "orderId" in str(ts_res) or "algoOrderId" in str(ts_res):
                    await tg_bot.send_message(CHAT_ID, f"📉 Trailing: {TRAILING_RATE}%", parse_mode="HTML")
        else:
            await tg_bot.send_message(CHAT_ID, f"❌ Ошибка входа: {res.get('msg')}")

# --- МОНИТОРИНГ ---
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
        log_trade(symbol, side, pnl)
        await tg_bot.send_message(CHAT_ID, f"{'✅' if pnl > 0 else '🛑'} <b>ЗАКРЫТО: {symbol}</b>\nPnL: {pnl:+.2f} USDT", parse_mode="HTML")

# --- WEB & TG ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); await load_exchange_info(); await sync_positions()
    asyncio.create_task(pnl_monitor())
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    await tg_bot.send_message(CHAT_ID, "🟢 Бот v1.6.7 запущен")
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/tg")
async def tg_webhook(request: Request):
    global active_trailing_enabled, take_profit_enabled
    d = await request.json()
    upd = Update.de_json(d, tg_bot)
    if upd.message and upd.message.text:
        t = upd.message.text
        if t == "📦 Позиции":
            res = await binance("GET", "/fapi/v2/positionRisk")
            if isinstance(res, list):
                msg = "\n".join([f"<b>{p['symbol']}</b> {p['positionSide']}: {round(float(p['unRealizedProfit']), 2)} USDT" for p in res if float(p['positionAmt']) != 0])
                await tg_bot.send_message(CHAT_ID, msg or "Нет позиций", parse_mode="HTML")
            else: await tg_bot.send_message(CHAT_ID, "Ошибка API или блок 451")
        elif t == "/start":
            await tg_bot.send_message(CHAT_ID, "Меню:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📦 Позиции"), KeyboardButton("⚙️ Настройки")]], resize_keyboard=True))
        elif t == "⚙️ Настройки":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"TP: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
            await tg_bot.send_message(CHAT_ID, "Настройки:", reply_markup=kb)
    elif upd.callback_query:
        q = upd.callback_query
        if q.data == "t_ts": active_trailing_enabled = not active_trailing_enabled
        if q.data == "t_tp": take_profit_enabled = not take_profit_enabled
        await q.answer("Настройки изменены")
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
