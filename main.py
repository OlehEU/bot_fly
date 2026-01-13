# =========================================================================================
# OZ TRADING BOT 2026 v1.7.2 | FULL INFO & ERROR HANDLING
# =========================================================================================
import os, time, hmac, hashlib, sqlite3, logging, asyncio
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

# Параметры стратегии
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

# ==================== ТОРГОВАЯ ЛОГИКА ====================
async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    async with trade_lock:
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts):
            return

        # Подготовка
        await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if "price" not in p_data:
            await tg_bot.send_message(CHAT_ID, f"⚠️ <b>Ошибка цены {symbol}</b>\nБиржа не ответила на запрос цены.", parse_mode="HTML")
            return
        
        price = float(p_data["price"])
        qty = fix_qty(symbol, (AMOUNT * LEV) / price)
        
        # 1. ВХОД
        res = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", 
            "positionSide": side, "type": "MARKET", "quantity": qty
        })
        
        if not res.get("orderId"):
            await tg_bot.send_message(CHAT_ID, f"❌ <b>ОШИБКА ВХОДА {symbol}</b>\nПричина: <code>{res.get('msg', 'Insufficient Margin')}</code>", parse_mode="HTML")
            return

        # Успех
        if side == "LONG": active_longs.add(symbol)
        else: active_shorts.add(symbol)
        await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side}</b>\nСимвол: <code>{symbol}</code>\nЦена: <code>{price}</code>\nПлечо: <code>{LEV}x</code>", parse_mode="HTML")
        
        await asyncio.sleep(1.5)
        close_side = "SELL" if side == "LONG" else "BUY"

        # 2. TAKE PROFIT
        if take_profit_enabled:
            tp_p = price * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else price * (1 - TAKE_PROFIT_RATE/100)
            tp_res = await binance("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", 
                "stopPrice": fix_price(symbol, tp_p), "closePosition": "true", "workingType": "MARK_PRICE"
            })
            if tp_res.get("orderId"):
                await tg_bot.send_message(CHAT_ID, f"🎯 <b>Take Profit установлен</b>\nЦена: <code>{fix_price(symbol, tp_p)}</code> (+{TAKE_PROFIT_RATE}%)", parse_mode="HTML")
            else:
                await tg_bot.send_message(CHAT_ID, f"⚠️ <b>Ошибка TP:</b> <code>{tp_res.get('msg')}</code>", parse_mode="HTML")

        # 3. TRAILING STOP
        if active_trailing_enabled:
            await asyncio.sleep(0.5)
            act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
            ts_res = await binance("POST", "/fapi/v1/algoOrder", {
                "algoType":"CONDITIONAL", "symbol":symbol, "side":close_side, "positionSide":side,
                "type":"TRAILING_STOP_MARKET", "quantity":qty, "callbackRate":TRAILING_RATE, "activationPrice":fix_price(symbol, act)
            })
            if "orderId" in str(ts_res) or "algoOrderId" in str(ts_res):
                await tg_bot.send_message(CHAT_ID, f"📉 <b>Trailing Stop активен</b>\nОтступ: <code>{TRAILING_RATE}%</code>\nАктивация: <code>{fix_price(symbol, act)}</code>", parse_mode="HTML")
            else:
                await tg_bot.send_message(CHAT_ID, f"⚠️ <b>Ошибка Trailing:</b> <code>{ts_res.get('msg', 'Параметры вне диапазона')}</code>", parse_mode="HTML")

# ==================== TG HANDLER ====================
async def handle_tg_logic(update_json):
    global active_trailing_enabled, take_profit_enabled
    try:
        upd = Update.de_json(update_json, tg_bot)
        if upd.callback_query:
            q = upd.callback_query
            if q.data == "t_ts": active_trailing_enabled = not active_trailing_enabled
            if q.data == "t_tp": take_profit_enabled = not take_profit_enabled
            new_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Trailing: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"Take Profit: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
            await q.edit_message_reply_markup(reply_markup=new_kb)
            await q.answer("Настройки обновлены")
            return

        if not upd.message or not upd.message.text: return
        t, cid = upd.message.text, upd.message.chat_id
        
        main_kb = ReplyKeyboardMarkup([
            [KeyboardButton("📦 Позиции"), KeyboardButton("📈 Статистика")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("🔄 Обновить")]
        ], resize_keyboard=True)

        if t == "/start":
            await tg_bot.send_message(cid, "<b>OZ Bot v1.7.2 Ready</b>\nИспользуйте меню для управления.", reply_markup=main_kb, parse_mode="HTML")

        elif t == "📦 Позиции":
            data = await binance("GET", "/fapi/v2/positionRisk")
            pos = [f"<b>{p['symbol']}</b> {p['positionSide']}\nPnL: <code>{float(p['unRealizedProfit']):+.2f} USDT</code>" for p in data if float(p['positionAmt']) != 0]
            await tg_bot.send_message(cid, "📂 <b>Активные позиции:</b>\n\n" + ("\n\n".join(pos) if pos else "Список пуст"), parse_mode="HTML")

        elif t == "📈 Статистика":
            s = get_stats()
            msg = (f"📊 <b>ФИНАНСОВЫЙ ОТЧЕТ</b>\n\n"
                   f"💰 <b>За 24 часа:</b>\n"
                   f"└ Прибыль: <code>{s['daily_pnl']:+.2f} USDT</code>\n"
                   f"└ Закрыто: <code>{s['daily_count']} сделок</code>\n\n"
                   f"📈 <b>Всего:</b>\n"
                   f"└ Прибыль: <code>{s['total_pnl']:+.2f} USDT</code>\n"
                   f"└ Сделок: <code>{s['total_count']}</code>")
            await tg_bot.send_message(cid, msg, parse_mode="HTML")

        elif t == "🔄 Обновить":
            await load_exchange_info()
            await sync_positions()
            await tg_bot.send_message(cid, "🔄 <b>Данные синхронизированы</b>\nОбновлены параметры монет и позиции.", parse_mode="HTML")

        elif t == "⚙️ Настройки":
            ikb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Trailing: {'✅' if active_trailing_enabled else '❌'}", callback_data="t_ts")], [InlineKeyboardButton(f"Take Profit: {'✅' if take_profit_enabled else '❌'}", callback_data="t_tp")]])
            await tg_bot.send_message(cid, "⚙️ <b>Настройки защиты:</b>", reply_markup=ikb, parse_mode="HTML")

    except Exception as e: logging.error(f"TG Error: {e}")

# ==================== WEB APP ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); await load_exchange_info(); await sync_positions()
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    await tg_bot.send_message(CHAT_ID, "🟢 <b>Система онлайн</b>\nБот готов к приему сигналов.", parse_mode="HTML")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health(): return {"status": "ok", "bot": "OZ v1.7.2"}

@app.post("/tg")
async def tg_webhook(request: Request):
    data = await request.json()
    asyncio.create_task(handle_tg_logic(data))
    return {"ok": True}

@app.post("/webhook")
async def signal_webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": 403}
    d = await request.json()
    sig, sym = d.get("signal", "").upper(), d.get("symbol", "").upper()
    if sig in ["LONG", "SHORT"]: asyncio.create_task(open_pos(sym, sig))
    return {"ok": True}
