# =========================================================================================
# OZ TRADING BOT 2025 v1.6.2 | FIX: Double Entry, Cross-Margin, Multi-Stats
# =========================================================================================
import os
import time
import hmac
import hashlib
import sqlite3
import logging
from typing import Dict, Set
import httpx
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL", "").rstrip('/')
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "1.0"))
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", "1.0"))
TS_START_RATE = float(os.getenv("TS_START_RATE", "0.2"))
PNL_MONITOR_INTERVAL = 20

client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"
trade_lock = asyncio.Lock() # Блокировка для предотвращения дублей сделок

# Состояние
symbol_precision = {}
price_precision = {}
active_longs = set()
active_shorts = set()
active_trailing_enabled = True
take_profit_enabled = True

tg_bot = Bot(token=TELEGRAM_TOKEN)

# ==================== МОДУЛЬ СТАТИСТИКИ (SQLITE) ====================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)''')

def log_trade_result(symbol, side, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)",
                     (symbol, side, round(pnl, 3), datetime.now()))

def get_stats_report(days):
    with sqlite3.connect(DB_PATH) as conn:
        since = datetime.now() - timedelta(days=days)
        cursor = conn.execute("SELECT SUM(pnl), COUNT(id) FROM trades WHERE timestamp >= ?", (since,))
        res = cursor.fetchone()
        total_pnl = res[0] if res[0] else 0
        count = res[1] if res[1] else 0
        
        period_name = "СУТКИ" if days == 1 else f"{days} ДНЕЙ"
        return f"📊 <b>ИТОГ ЗА {period_name}</b>\n💰 Профит: <code>{total_pnl:+.2f} USDT</code>\n📦 Сделок: <code>{count}</code>"

# ==================== BINANCE API ====================

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

async def load_exchange_info():
    global symbol_precision, price_precision
    data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in data.get('symbols', []):
        sym = s['symbol']
        lot = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
        prc = next(f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')
        symbol_precision[sym] = len(lot['stepSize'].rstrip('0').split('.')[-1]) if '.' in lot['stepSize'] else 0
        price_precision[sym] = len(prc['tickSize'].rstrip('0').split('.')[-1]) if '.' in prc['tickSize'] else 0

def fix_qty(s, q): return f"{q:.{symbol_precision.get(s, 3)}f}".rstrip("0").rstrip(".")
def fix_price(s, pr): return f"{pr:.{price_precision.get(s, 8)}f}".rstrip("0").rstrip(".")

# ==================== ЛОГИКА ТОРГОВЛИ ====================

async def open_pos(sym, side):
    symbol = sym.upper().replace("/", "") + "USDT" if "USDT" not in sym.upper() else sym.upper()
    
    async with trade_lock:
        # ПРОВЕРКА НА ДУБЛЬ: Если позиция уже в списке активных, выходим
        if (side == "LONG" and symbol in active_longs) or (side == "SHORT" and symbol in active_shorts):
            logging.info(f"Игнорируем сигнал {side} {symbol}: позиция уже открыта.")
            return

        # ПРИНУДИТЕЛЬНЫЙ КРОСС И ПЛЕЧО
        try:
            await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
        except: pass # Ошибка если уже кросс
        await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
        
        p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        price = float(p_data["price"])
        qty = fix_qty(symbol, (AMOUNT * LEV) / price)
        
        res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL", "positionSide": side, "type": "MARKET", "quantity": qty})
        
        if res.get("orderId"):
            if side == "LONG": active_longs.add(symbol)
            else: active_shorts.add(symbol)
            await tg_bot.send_message(CHAT_ID, f"🚀 <b>ВХОД {side} {symbol}</b>", parse_mode="HTML")
            
            # Установка стопов (код аналогичен прошлым версиям)
            close_side = "SELL" if side == "LONG" else "BUY"
            if active_trailing_enabled:
                act = price * (1 + TS_START_RATE/100) if side == "LONG" else price * (1 - TS_START_RATE/100)
                await binance("POST", "/fapi/v1/algoOrder", {"algoType":"CONDITIONAL","symbol":symbol,"side":close_side,"positionSide":side,"type":"TRAILING_STOP_MARKET","quantity":qty,"callbackRate":TRAILING_RATE,"activationPrice":fix_price(symbol,act)})

async def close_pos(sym, side):
    symbol = sym.upper().replace("/", "") + "USDT" if "USDT" not in sym.upper() else sym.upper()
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    qty = next((abs(float(p["positionAmt"])) for p in data if p["positionSide"] == side), 0)
    if qty > 0:
        await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "SELL" if side == "LONG" else "BUY", "positionSide": side, "type": "MARKET", "quantity": fix_qty(symbol, qty)})

# ==================== МОНИТОРИНГ И ТЕЛЕГРАМ ====================

async def pnl_monitor():
    global active_longs, active_shorts
    while True:
        await asyncio.sleep(PNL_MONITOR_INTERVAL)
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

async def report_pnl(symbol, side):
    await asyncio.sleep(5)
    trades = await binance("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": 10})
    pnl = sum(float(t.get('realizedPnl', 0)) - float(t.get('commission', 0)) for t in trades)
    log_trade_result(symbol, side, pnl)
    await tg_bot.send_message(CHAT_ID, f"🏁 <b>ЗАКРЫТ {side} {symbol}</b>\nPnL: <code>{pnl:+.2f} USDT</code>", parse_mode="HTML")

# КЛАВИАТУРА С ПЕРИОДАМИ
def get_stats_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("День", callback_data="st_1"), InlineKeyboardButton("Неделя", callback_data="st_7")],
        [InlineKeyboardButton("Месяц", callback_data="st_30"), InlineKeyboardButton("3 Месяца", callback_data="st_90")]
    ])

async def handle_tg(update_json):
    upd = Update.de_json(update_json, tg_bot)
    if upd.message and upd.message.text == "📊 Статистика":
        await upd.message.reply_html("Выберите период отчета:", reply_markup=get_stats_kb())
    elif upd.callback_query:
        data = upd.callback_query.data
        if data.startswith("st_"):
            days = int(data.split("_")[1])
            await upd.callback_query.edit_message_text(get_stats_report(days), parse_mode="HTML", reply_markup=get_stats_kb())
        await upd.callback_query.answer()

# ==================== ЗАПУСК ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await load_exchange_info()
    asyncio.create_task(pnl_monitor())
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg")
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root(): return "OZ Bot v1.6.2 Running"

@app.post("/tg")
async def tg_webhook(request: Request):
    asyncio.create_task(handle_tg(await request.json()))
    return {"ok": True}

@app.post("/webhook")
async def signal_webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error":403}
    data = await request.json()
    sig, sym = data.get("signal", "").upper(), data.get("symbol", "").upper()
    if sig == "LONG": asyncio.create_task(open_pos(sym, "LONG"))
    elif sig == "SHORT": asyncio.create_task(open_pos(sym, "SHORT"))
    elif "CLOSE" in sig: asyncio.create_task(close_pos(sym, "LONG" if "LONG" in sig else "SHORT"))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
