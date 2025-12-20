# =========================================================================================
# OZ TRADING BOT 2025 v1.6.0 | FULL VERSION WITH SQL & REPLY MENU
# =========================================================================================
# Основные изменения: 
# 1. Добавлен обработчик "/" для устранения ошибок 404 (Health Check).
# 2. Интегрирована БД SQLite для надежного хранения статистики.
# 3. Реализовано постоянное меню с кнопками (Reply Keyboard).
# 4. Сохранена вся логика точного входа и мониторинга PnL из v1.5.6.
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
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.error import TelegramError
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# ==================== КОНФИГУРАЦИЯ & ПЕРЕМЕННЫЕ ====================
# Подгружаем настройки из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL", "").rstrip('/')

# Настройки стратегии
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))   # Сумма входа в USD
LEV = int(os.getenv("LEVERAGE", "10"))               # Плечо
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "1.0")) # Процент трейлинга
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", "1.0")) # Процент тейка
TS_START_RATE = float(os.getenv("TS_START_RATE", "0.2")) # Активация трейлинга
PNL_MONITOR_INTERVAL = int(os.getenv("PNL_MONITOR_INTERVAL_SEC", "20")) # Частота проверки позиций

# Инициализация клиента и бота
client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"
DB_PATH = "trades_history.db"

# Глобальные кэш-переменные
symbol_precision = {} # Округление количества
price_precision = {}  # Округление цены
active_longs = set()  # Список открытых лонгов
active_shorts = set() # Список открытых шортов
active_trailing_enabled = os.getenv("TRAILING_ENABLED", "true").lower() in ('true', '1', 't')
take_profit_enabled = os.getenv("TAKE_PROFIT_ENABLED", "true").lower() in ('true', '1', 't')

tg_bot = Bot(token=TELEGRAM_TOKEN) 

# ==================== МОДУЛЬ СТАТИСТИКИ (SQLITE) ====================

def init_db():
    """Создает базу данных и таблицу сделок при первом запуске."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      symbol TEXT, side TEXT, pnl REAL, timestamp DATETIME)''')
        conn.commit()

def log_trade_result(symbol, side, pnl):
    """Записывает результат закрытой сделки в БД."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO trades (symbol, side, pnl, timestamp) VALUES (?, ?, ?, ?)",
                     (symbol, side, round(pnl, 3), datetime.now()))
        conn.commit()

def get_stats_report(days):
    """Формирует текстовый отчет по прибыли за период."""
    with sqlite3.connect(DB_PATH) as conn:
        since = datetime.now() - timedelta(days=days)
        cursor = conn.execute("""SELECT symbol, SUM(pnl), COUNT(id) FROM trades 
                                 WHERE timestamp >= ? GROUP BY symbol ORDER BY SUM(pnl) DESC""", (since,))
        rows = cursor.fetchall()
        if not rows: return "📭 Сделок за этот период еще не зафиксировано."
        
        total = sum(r[1] for r in rows)
        res = f"📊 <b>ОТЧЕТ ЗА {days} ДН.</b>\n💰 Итого: <code>{total:+.2f} USDT</code>\n"
        res += "----------------------------\n"
        for sym, pnl, count in rows:
            icon = "🟢" if pnl >= 0 else "🔴"
            res += f"{icon} {sym}: <code>{pnl:+.2f}</code> ({count} шт)\n"
        return res

# ==================== BINANCE API ВЗАИМОДЕЙСТВИЕ ====================

async def tg(text):
    """Отправка уведомлений в Telegram."""
    try: await tg_bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except: pass

async def binance(method, path, params=None, signed=True):
    """Универсальная функция для запросов к Binance с подписью."""
    url = BASE + path
    p = params.copy() if params else {}
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000
        query = "&".join([f"{k}={v}" for k, v in sorted(p.items())])
        sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        url += f"?{query}&signature={sig}"
        p = None
    r = await client.request(method, url, params=p, headers={"X-MBX-APIKEY": API_KEY})
    return r.json()

async def load_exchange_info():
    """Загружает правила биржи (округление) для всех пар."""
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

async def get_entry_price(symbol, side):
    """Цикл ожидания фактической цены входа после MARKET ордера."""
    for _ in range(5):
        await asyncio.sleep(0.8)
        data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        for p in data:
            if p["positionSide"] == side and abs(float(p["positionAmt"])) > 0:
                return float(p["entryPrice"])
    return None

# ==================== ТОРГОВАЯ ЛОГИКА ====================

async def open_pos(sym, side):
    """Открытие позиции MARKET и выставление стопов (Трейлинг/Тейк)."""
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    
    # Настройка режима маржи и плеча
    await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
    
    # Расчет объема (Quantity)
    p_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    price = float(p_data["price"])
    qty = fix_qty(symbol, (AMOUNT * LEV) / price)
    
    order_side = "BUY" if side == "LONG" else "SELL"
    res = await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": order_side, "positionSide": side, "type": "MARKET", "quantity": qty})
    
    if res.get("orderId"):
        if side == "LONG": active_longs.add(symbol)
        else: active_shorts.add(symbol)
        
        entry = await get_entry_price(symbol, side) or price
        await tg(f"{'🚀' if side=='LONG' else '⬇️'} <b>{side} {symbol}</b>\nВход: <code>{fix_price(symbol, entry)}</code>")
        
        close_side = "SELL" if side == "LONG" else "BUY"
        # Выставление Trailing Stop
        if active_trailing_enabled:
            act = entry * (1 + TS_START_RATE/100) if side == "LONG" else entry * (1 - TS_START_RATE/100)
            await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "positionSide": side, "type": "TRAILING_STOP_MARKET", "quantity": qty, "callbackRate": TRAILING_RATE, "activationPrice": fix_price(symbol, act)})
        # Выставление Take Profit
        if take_profit_enabled:
            tp = entry * (1 + TAKE_PROFIT_RATE/100) if side == "LONG" else entry * (1 - TAKE_PROFIT_RATE/100)
            await binance("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "positionSide": side, "type": "TAKE_PROFIT_MARKET", "quantity": qty, "triggerPrice": fix_price(symbol, tp)})

async def close_pos(sym, side):
    """Полное закрытие позиции и отмена всех стоп-ордеров по символу."""
    symbol = sym.upper().replace("/", "")
    if "USDT" not in symbol: symbol += "USDT"
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    data = await binance("GET", "/fapi/v2/positionRisk")
    qty = next((abs(float(p["positionAmt"])) for p in data if p["symbol"] == symbol and p["positionSide"] == side), 0)
    if qty > 0:
        order_side = "SELL" if side == "LONG" else "BUY"
        await binance("POST", "/fapi/v1/order", {"symbol": symbol, "side": order_side, "positionSide": side, "type": "MARKET", "quantity": fix_qty(symbol, qty)})

# ==================== МОНИТОРИНГ PNL ====================

async def pnl_monitor():
    """Фоновая задача для отслеживания закрытых позиций и записи PnL."""
    global active_longs, active_shorts
    while True:
        await asyncio.sleep(PNL_MONITOR_INTERVAL)
        data = await binance("GET", "/fapi/v2/positionRisk")
        if not isinstance(data, list): continue
        current = {p['symbol'] + p['positionSide'] for p in data if abs(float(p['positionAmt'])) > 0}
        
        # Проверяем, какие позиции исчезли (были закрыты стопом или вручную)
        for s in list(active_longs):
            if (s + "LONG") not in current:
                active_longs.discard(s)
                asyncio.create_task(report_pnl(s, "LONG"))
        for s in list(active_shorts):
            if (s + "SHORT") not in current:
                active_shorts.discard(s)
                asyncio.create_task(report_pnl(s, "SHORT"))

async def report_pnl(symbol, side):
    """Получает финальный профит из истории сделок и шлет отчет."""
    await asyncio.sleep(5)
    trades = await binance("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": 10})
    if isinstance(trades, list):
        pnl = sum(float(t.get('realizedPnl', 0)) - float(t.get('commission', 0)) for t in trades)
        log_trade_result(symbol, side, pnl)
        await tg(f"{'✅' if pnl>0 else '🛑'} <b>ЗАКРЫТ {side} {symbol}</b>\nPnL: <code>{pnl:+.2f} USDT</code>")

# ==================== ТЕЛЕГРАМ МЕНЮ ====================

def get_kb():
    """Создает главную клавиатуру с кнопками."""
    return ReplyKeyboardMarkup([[KeyboardButton("📊 Статистика"), KeyboardButton("⚙️ Настройки")], [KeyboardButton("🔄 Обновить")]], resize_keyboard=True)

async def handle_tg(update_json):
    """Обработка нажатий на кнопки и команд в Telegram."""
    global active_trailing_enabled, take_profit_enabled
    upd = Update.de_json(update_json, tg_bot)
    if upd.message and upd.message.text:
        msg = upd.message.text
        if msg == "/start": 
            await upd.message.reply_html("🤖 <b>OZ Trading Bot</b>\nИспользуйте меню для управления.", reply_markup=get_kb())
        elif msg == "📊 Статистика": 
            await upd.message.reply_html(get_stats_report(1))
        elif msg == "⚙️ Настройки":
            txt = f"🛡 Трейлинг: {'✅' if active_trailing_enabled else '❌'}\n🎯 Take Profit: {'✅' if take_profit_enabled else '❌'}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Переключить TS", callback_data="t_ts"), InlineKeyboardButton("Переключить TP", callback_data="t_tp")]])
            await upd.message.reply_html(txt, reply_markup=kb)
        elif msg == "🔄 Обновить":
            await load_exchange_info()
            await upd.message.reply_text("✅ Информация о монетах обновлена.")
    elif upd.callback_query:
        if upd.callback_query.data == "t_ts": active_trailing_enabled = not active_trailing_enabled
        if upd.callback_query.data == "t_tp": take_profit_enabled = not take_profit_enabled
        await upd.callback_query.answer("Сохранено")
        txt = f"🛡 Трейлинг: {'✅' if active_trailing_enabled else '❌'}\n🎯 Take Profit: {'✅' if take_profit_enabled else '❌'}"
        await upd.callback_query.edit_message_text(txt, reply_markup=upd.callback_query.message.reply_markup, parse_mode="HTML")

# ==================== FASTAPI И ЭНДПОИНТЫ ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db() # Инициализация БД
    await load_exchange_info() # Загрузка данных пар
    asyncio.create_task(pnl_monitor()) # Запуск монитора
    await tg_bot.set_webhook(f"{PUBLIC_HOST_URL}/tg") # Настройка вебхука
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def root():
    """Главная страница для Health Check (убирает 404 в логах)."""
    return "<html><body><h1 style='color:green'>OZ Bot is Running</h1></body></html>"

@app.post("/tg")
async def tg_webhook(request: Request):
    """Прием обновлений от Telegram."""
    asyncio.create_task(handle_tg(await request.json()))
    return {"ok": True}

@app.post("/webhook")
async def signal_webhook(request: Request):
    """Прием сигналов от TradingView."""
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET: return {"error": 403}
    data = await request.json()
    sig, sym = data.get("signal", "").upper(), data.get("symbol", "").upper()
    if sig == "LONG": asyncio.create_task(open_pos(sym, "LONG"))
    elif sig == "SHORT": asyncio.create_task(open_pos(sym, "SHORT"))
    elif "CLOSE" in sig: 
        side = "LONG" if "LONG" in sig else "SHORT"
        asyncio.create_task(close_pos(sym, side))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
