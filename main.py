# =========================================================================================
# OZ TRADING BOT 2025 v1.6.0 | FULL INTEGRATED VERSION (SQLite + Reply Menu)
# =========================================================================================
import os
import time
import hmac
import hashlib
import json
import sqlite3
from typing import Dict, Set, Any, List
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, constants
from telegram.error import TelegramError
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

# ==================== КОНФИГУРАЦИЯ & ПЕРЕМЕННЫЕ ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET", "PUBLIC_HOST_URL"]
for v in required:
    if not os.getenv(v):
        raise ValueError(f"Нет переменной окружения: {v}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
try:
    CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
except (ValueError, TypeError):
    raise ValueError("TELEGRAM_CHAT_ID должен быть целым числом.")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL").rstrip('/')
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "1.0")) 
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", "1.0")) 
TS_START_RATE = float(os.getenv("TS_START_RATE", "0.2")) 
PNL_MONITOR_INTERVAL = int(os.getenv("PNL_MONITOR_INTERVAL_SEC", "20")) 

client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"

# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
symbol_precision: Dict[str, int] = {}
price_precision: Dict[str, int] = {}
active_longs: Set[str] = set() 
active_shorts: Set[str] = set() 
active_trailing_enabled: bool = os.getenv("TRAILING_ENABLED", "true").lower() in ('true', '1', 't')
take_profit_enabled: bool = os.getenv("TAKE_PROFIT_ENABLED", "true").lower() in ('true', '1', 't')

tg_bot = Bot(token=TELEGRAM_TOKEN) 

# ==================== МОДУЛЬ СТАТИСТИКИ (SQLITE) ====================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)''')
        conn.commit()

def log_trade_result(symbol: str, position_side: str, pnl_usd: float):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)",
                     (symbol, position_side, round(pnl_usd, 3), datetime.now()))
        conn.commit()

def get_stats_report(days: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        since = datetime.now() - timedelta(days=days)
        cursor = conn.execute("""SELECT symbol, SUM(pnl), COUNT(id) FROM trades 
                                 WHERE timestamp >= ? GROUP BY symbol ORDER BY SUM(pnl) DESC""", (since,))
        rows = cursor.fetchall()
        if not rows: return "📭 Сделок за этот период не найдено."
        total_pnl = sum(r[1] for r in rows)
        report = f"📊 <b>ОТЧЕТ ЗА {days} ДН.</b>\n💰 Итого: <code>{total_pnl:+.2f} USDT</code>\n"
        report += "----------------------------\n"
        for sym, pnl, count in rows:
            icon = "🟢" if pnl >= 0 else "🔴"
            report += f"{icon} {sym}: <code>{pnl:+.2f}</code> ({count} шт)\n"
        return report

# ==================== BINANCE API ВСПОМОГАТЕЛЬНЫЕ ====================

async def tg(text: str):
    try:
        await tg_bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"[ERROR] Telegram failed: {e}")

async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    url = BASE + path
    p = params.copy() if params else {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000
        query_string = "&".join([f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in sorted(p.items())])
        signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        url = f"{url}?{query_string}&signature={signature}"
        p = None
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        r = await client.request(method, url, params=p, headers=headers)
        if r.status_code != 200:
            await tg(f"<b>BINANCE ERROR {r.status_code}</b>\n<code>{r.text[:500]}</code>")
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)}")
        return None

def calculate_precision_from_stepsize(step_size: str) -> int:
    s = step_size.rstrip('0')
    return len(s.split('.')[-1]) if '.' in s else 0

async def load_exchange_info():
    global symbol_precision, price_precision
    data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
    if data and 'symbols' in data:
        for s in data['symbols']:
            sym = s['symbol']
            lot = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
            prc = next(f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')
            symbol_precision[sym] = calculate_precision_from_stepsize(lot['stepSize'])
            price_precision[sym] = calculate_precision_from_stepsize(prc['tickSize'])
        await tg(f"✅ ExchangeInfo загружен. Пар: {len(symbol_precision)}")

async def load_active_positions():
    global active_longs, active_shorts
    data = await binance("GET", "/fapi/v2/positionRisk")
    if isinstance(data, list):
        active_longs = {p["symbol"] for p in data if float(p["positionAmt"]) > 0 and p["positionSide"] == "LONG"}
        active_shorts = {p["symbol"] for p in data if float(p["positionAmt"]) < 0 and p["positionSide"] == "SHORT"}
        await tg(f"📋 Позиции: LONG: {len(active_longs)}, SHORT: {len(active_shorts)}")

def fix_qty(symbol: str, qty: float) -> str:
    prec = symbol_precision.get(symbol.upper(), 3)
    return f"{qty:.{prec}f}".rstrip("0").rstrip(".") if prec > 0 else str(int(qty))

def fix_price(symbol: str, price: float) -> str:
    prec = price_precision.get(symbol.upper(), 8)
    return f"{price:.{prec}f}".rstrip("0").rstrip(".")

async def get_symbol_and_qty(sym: str):
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
    p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not p_data or 'price' not in p_data: return None
    price = float(p_data["price"])
    qty = fix_qty(symbol, (AMOUNT * LEV) / price)
    return symbol, qty, price

# ==================== МОНИТОРИНГ PNL И ЗАКРЫТИЕ ====================

async def get_pnl_from_closed_trades(symbol: str):
    end = int(time.time() * 1000)
    start = end - (90 * 60 * 1000)
    trades = await binance("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start})
    if trades and isinstance(trades, list):
        net = sum(float(t.get('realizedPnl', 0)) - float(t.get('commission', 0)) for t in trades)
        return net if any(float(t.get('realizedPnl', 0)) != 0 for t in trades) else None
    return None

async def calculate_and_report_pnl(symbol: str, side: str):
    await asyncio.sleep(5) # Ждем синхронизации Binance
    pnl = await get_pnl_from_closed_trades(symbol)
    if pnl is not None:
        log_trade_result(symbol, side, pnl)
        icon = "✅" if pnl > 0 else "🛑"
        await tg(f"{icon} <b>ЗАКРЫТИЕ {side} {symbol}</b>\nЧИСТЫЙ PnL: <code>{pnl:+.2f} USDT</code>")
    else:
        await tg(f"❌ <b>ЗАКРЫТИЕ {side} {symbol}</b>\nPnL не определен.")

async def pnl_monitor_task():
    global active_longs, active_shorts
    while True:
        await asyncio.sleep(PNL_MONITOR_INTERVAL)
        try:
            data = await binance("GET", "/fapi/v2/positionRisk")
            if not isinstance(data, list): continue
            current = {p['symbol'] + p['positionSide'] for p in data if abs(float(p['positionAmt'])) > 0}
            
            for s in list(active_longs):
                if (s + "LONG") not in current:
                    active_longs.discard(s)
                    asyncio.create_task(calculate_and_report_pnl(s, "LONG"))
            for s in list(active_shorts):
                if (s + "SHORT") not in current:
                    active_shorts.discard(s)
                    asyncio.create_task(calculate_and_report_pnl(s, "SHORT"))
        except Exception as e: print(f"Monitor error: {e}")

# ==================== ОТКРЫТИЕ ПОЗИЦИЙ ====================

async def get_entry_price_from_position(symbol: str, side: str):
    for _ in range(3):
        data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(data, list):
            for p in data:
                if p["positionSide"] == side and abs(float(p["positionAmt"])) > 0:
                    return float(p["entryPrice"])
        await asyncio.sleep(0.7)
    return None

async def open_long(sym: str):
    res = await get_symbol_and_qty(sym)
    if not res: return
    symbol, qty, _ = res
    order = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": qty})
    
    if order and order.get("orderId"):
        active_longs.add(symbol)
        entry = await get_entry_price_from_position(symbol, "LONG")
        if not entry: return await tg(f"⚠️ {symbol} LONG: Нет Entry Price!")
        
        await tg(f"🚀 <b>LONG {symbol}</b> (x{LEV})\nEntry: <code>{fix_price(symbol, entry)}</code>")
        
        if active_trailing_enabled:
            act_p = fix_price(symbol, entry * (1 + TS_START_RATE/100))
            await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG", "type": "TRAILING_STOP_MARKET", "quantity": qty, "callbackRate": f"{TRAILING_RATE:.2f}", "activationPrice": act_p})
        if take_profit_enabled:
            tp_p = fix_price(symbol, entry * (1 + TAKE_PROFIT_RATE/100))
            await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG", "type": "TAKE_PROFIT_MARKET", "quantity": qty, "triggerPrice": tp_p})

async def open_short(sym: str):
    res = await get_symbol_and_qty(sym)
    if not res: return
    symbol, qty, _ = res
    order = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": qty})
    
    if order and order.get("orderId"):
        active_shorts.add(symbol)
        entry = await get_entry_price_from_position(symbol, "SHORT")
        if not entry: return await tg(f"⚠️ {symbol} SHORT: Нет Entry Price!")
        
        await tg(f"⬇️ <b>SHORT {symbol}</b> (x{LEV})\nEntry: <code>{fix_price(symbol, entry)}</code>")
        
        if active_trailing_enabled:
            act_p = fix_price(symbol, entry * (1 - TS_START_RATE/100))
            await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT", "type": "TRAILING_STOP_MARKET", "quantity": qty, "callbackRate": f"{TRAILING_RATE:.2f}", "activationPrice": act_p})
        if take_profit_enabled:
            tp_p = fix_price(symbol, entry * (1 - TAKE_PROFIT_RATE/100))
            await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT", "type": "TAKE_PROFIT_MARKET", "quantity": qty, "triggerPrice": tp_p})

async def close_long(sym: str):
    symbol = sym.upper() + "USDT"
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    qty = next((p["positionAmt"] for p in data if p["positionSide"] == "LONG" and float(p["positionAmt"]) > 0), 0)
    if float(qty) > 0:
        await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "SELL", "positionSide": "LONG", "type": "MARKET", "quantity": fix_qty(symbol, abs(float(qty)))})
    active_longs.discard(symbol)

async def close_short(sym: str):
    symbol = sym.upper() + "USDT"
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    qty = next((p["positionAmt"] for p in data if p["positionSide"] == "SHORT" and float(p["positionAmt"]) < 0), 0)
    if abs(float(qty)) > 0:
        await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY", "positionSide": "SHORT", "type": "MARKET", "quantity": fix_qty(symbol, abs(float(qty)))})
    active_shorts.discard(symbol)

# ==================== ТЕЛЕГРАМ ОБРАБОТКА ====================

def get_main_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("📊 Статистика"), KeyboardButton("⚙️ Настройки")], [KeyboardButton("🔄 Обновить данные")]], resize_keyboard=True)

def create_trailing_menu(ts, tp):
    t = f"<b>⚙️ Настройки</b>\nТрейлинг: {'✅' if ts else '❌'}\nTake Profit: {'✅' if tp else '❌'}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"TS: {'✅' if ts else '❌'}", callback_data=f"set_ts_{not ts}"),
                                 InlineKeyboardButton(f"TP: {'✅' if tp else '❌'}", callback_data=f"set_tp_{not tp}")]])
    return t, kb

async def handle_telegram_update(update_json: Dict):
    global active_trailing_enabled, take_profit_enabled
    upd = Update.de_json(update_json, tg_bot)
    if upd.message and upd.message.text:
        txt = upd.message.text
        if txt in ["/start", "🔙 Назад"]: await upd.message.reply_html("🤖 Бот готов", reply_markup=get_main_keyboard())
        elif txt == "📊 Статистика": await upd.message.reply_html(get_stats_report(1))
        elif txt == "⚙️ Настройки":
            t, kb = create_trailing_menu(active_trailing_enabled, take_profit_enabled)
            await upd.message.reply_html(t, reply_markup=kb)
        elif txt == "🔄 Обновить данные":
            await load_exchange_info()
            await load_active_positions()
            await upd.message.reply_text("✅ Обновлено")
    elif upd.callback_query:
        d = upd.callback_query.data
        if "set_ts" in d: active_trailing_enabled = "True" in d
        if "set_tp" in d: take_profit_enabled = "True" in d
        await upd.callback_query.answer("Сохранено")
        t, kb = create_trailing_menu(active_trailing_enabled, take_profit_enabled)
        await upd.callback_query.edit_message_text(t, reply_markup=kb, parse_mode="HTML")

# ==================== FASTAPI И ЗАПУСК ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await load_exchange_info()
    await load_active_positions()
    asyncio.create_task(pnl_monitor_task())
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg_hook")
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/tg_hook")
async def tg_hook(request: Request):
    asyncio.create_task(handle_telegram_update(await request.json()))
    return {"ok": True}

@app.post("/webhook")
async def signal_hook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": "auth"}
    data = await request.json()
    sym, sig = data.get("symbol", "").upper(), data.get("signal", "").upper()
    if sig == "LONG": asyncio.create_task(open_long(sym))
    elif sig == "SHORT": asyncio.create_task(open_short(sym))
    elif sig == "CLOSE_LONG": asyncio.create_task(close_long(sym))
    elif sig == "CLOSE_SHORT": asyncio.create_task(close_short(sym))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
