# main.py — УНИВЕРСАЛЬНЫЙ МУЛЬТИКОИН БОТ ДЛЯ BINANCE FUTURES + TELEGRAM МЕНЮ 2026
import os
import json
import time
import logging
import asyncio
from typing import Dict, Any, Optional
import httpx
import hmac
import hashlib
import urllib.parse
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("multi-coin-bot")

# ====================== КОНФИГ ======================
# Все секреты — только из .env или переменных окружения (Fly.io, Render, Railway, VPS и т.д.)
required_env = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required_env:
    if not os.getenv(var):
        raise EnvironmentError(f"ОБЯЗАТЕЛЬНАЯ переменная не задана: {var}")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))           # ← твой личный чат ID
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "supersecret123")  # ← можно переопределить в .env (Для TradingView)
# ====================== /КОНФИГ ======================

# Список коинов (расширяй здесь)
COINS = {
    "XRP": {"amount_usd": 10, "leverage": 10, "tp_percent": 0.5, "sl_percent": 1.0, "enabled": True},
    "SOL": {"amount_usd": 15, "leverage": 20, "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False},
    "ETH": {"amount_usd": 20, "leverage": 5, "tp_percent": 0.5, "sl_percent": 1.0, "enabled": True},
    "BTC": {"amount_usd": 50, "leverage": 3, "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False},
    "DOGE": {"amount_usd": 5, "leverage": 50, "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False}
}

SETTINGS_FILE = "settings.json"  # Автосохранение настроек
STATS_FILE = "stats.json"        # Статистика

bot = Bot(token=TELEGRAM_TOKEN)
binance_client = httpx.AsyncClient(timeout=60.0)

# ====================== НАСТРОЙКИ И СТАТИСТИКА ======================
def load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {coin: settings for coin, settings in COINS.items()}

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

def load_stats():
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"total_pnl": 0.0, "daily_pnl": {}, "per_coin": {coin: 0.0 for coin in COINS}}

def save_stats(stats):
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=2)

settings = load_settings()
stats = load_stats()

# ====================== BINANCE ФУНКЦИИ (УНИВЕРСАЛЬНЫЕ) ======================
BINANCE_BASE_URL = "https://fapi.binance.com"

def _create_signature(params: Dict[str, Any], secret: str) -> str:
    normalized = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items() if v is not None}
    query_string = urllib.parse.urlencode(normalized)
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True, symbol: str = None):
    url = f"{BINANCE_BASE_URL}{endpoint}"
    params = params or {}
    if symbol:
        params["symbol"] = f"{symbol}USDT"
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _create_signature(params, BINANCE_API_SECRET)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        response = await (binance_client.get if method == "GET" else binance_client.post)(
            url, params=params, headers=headers
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Binance error: {e}")
        return None

async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", signed=False, symbol=symbol)
    return float(data["price"]) if data else 0.0

async def get_qty(symbol: str, amount_usd: float, leverage: int) -> float:
    price = await get_price(symbol)
    raw = (amount_usd * leverage) / price
    info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in info.get("symbols", []):
        if s["symbol"] == f"{symbol}USDT":
            qty_precision = s.get("quantityPrecision", 3)
            min_qty = float(next((f["minQty"] for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0))
            qty = round(raw, qty_precision)
            return max(qty, min_qty)
    return 0.0

async def get_futures_balance() -> float:
    account = await binance_request("GET", "/fapi/v2/balance", signed=True)
    for asset in account:
        if asset["asset"] == "USDT":
            return float(asset["balance"])
    return 0.0

# ====================== ТОРГОВЛЯ ПО КОИНАМ ======================
async def open_long(coin: str):
    if not settings[coin]["enabled"]:
        return
    global stats
    try:
        symbol = coin.upper()
        pos_info = await binance_request("GET", "/fapi/v2/positionRisk", signed=True)
        for p in pos_info:
            if p["symbol"] == f"{symbol}USDT" and float(p.get("positionAmt", 0)) != 0:
                return  # Уже в позиции

        qty = await get_qty(symbol, settings[coin]["amount_usd"], settings[coin]["leverage"])
        if qty == 0:
            return

        oid = f"{coin}_{int(time.time()*1000)}"
        entry = await get_price(symbol)
        await binance_request("POST", "/fapi/v1/order", {"side": "BUY", "type": "MARKET", "quantity": str(qty), "newClientOrderId": oid}, signed=True, symbol=symbol)

        # TP/SL если включено
        if not settings[coin].get("disable_tpsl", True):  # По умолчанию отключено
            tp = entry * (1 + settings[coin]["tp_percent"] / 100)
            sl = entry * (1 - settings[coin]["sl_percent"] / 100)
            for price, name, order_type in [(tp, "tp", "TAKE_PROFIT_MARKET"), (sl, "sl", "STOP_MARKET")]:
                await binance_request("POST", "/fapi/v1/order", {"side": "SELL", "type": order_type, "quantity": str(qty), "stopPrice": str(price), "reduceOnly": "true", "newClientOrderId": f"{name}_{oid}"}, signed=True, symbol=symbol)

        last_balance_before_trade[coin] = await get_futures_balance()
        await tg_send(f"<b>LONG открыт по {coin}</b>\nСумма: ${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x\nEntry: <code>{entry:.4f}</code>")
        await tg_balance()

    except Exception as e:
        await tg_send(f"Ошибка открытия {coin}: {str(e)}")

async def close_all(coin: str):
    if not settings[coin]["enabled"]:
        return
    global stats
    try:
        symbol = coin.upper()
        pos_info = await binance_request("GET", "/fapi/v2/positionRisk", signed=True)
        current = None
        for p in pos_info:
            if p["symbol"] == f"{symbol}USDT" and float(p.get("positionAmt", 0)) != 0:
                current = p
                break
        if current:
            qty = abs(float(current["positionAmt"]))
            side = "SELL" if float(current["positionAmt"]) > 0 else "BUY"
            await binance_request("POST", "/fapi/v1/order", {"side": side, "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"}, signed=True, symbol=symbol)

        open_orders = await binance_request("GET", "/fapi/v1/openOrders", signed=True, symbol=symbol)
        cancelled = 0
        if open_orders:
            for order in open_orders:
                await binance_request("DELETE", "/fapi/v1/order", {"orderId": order["orderId"]}, signed=True, symbol=symbol)
                cancelled += 1

        current_balance = await get_futures_balance()
        pnl = current_balance - last_balance_before_trade.get(coin, current_balance)
        stats["per_coin"][coin] += pnl
        stats["total_pnl"] += pnl
        save_stats(stats)

        pnl_text = f"<b>Прибыль по {coin}:</b> <code>{pnl:+.2f}</code> USDT"
        await tg_send(f"<b>{coin} закрыт</b>\nОтменено ордеров: {cancelled}\n{pnl_text}")
        await tg_balance()

    except Exception as e:
        await tg_send(f"Ошибка закрытия {coin}: {str(e)}")

# ====================== TELEGRAM BOT МЕНЮ ======================
last_balance_before_trade = {}  # По коинам

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for coin in COINS:
        status = "✅" if settings[coin]["enabled"] else "❌"
        keyboard.append([InlineKeyboardButton(f"{coin} {status} | ${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x", callback_data=f"menu_{coin}")])
    keyboard.append([InlineKeyboardButton("📊 Статистика", callback_data="stats")])
    keyboard.append([InlineKeyboardButton("💰 Баланс", callback_data="balance")])
    keyboard.append([InlineKeyboardButton("🔄 Перезапуск", callback_data="restart")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите коин или действие:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("menu_"):
        coin = data.split("_")[1]
        keyboard = [
            [InlineKeyboardButton("✅ Включить" if not settings[coin]["enabled"] else "❌ Выключить", callback_data=f"toggle_{coin}")],
            [InlineKeyboardButton("💰 Сумма", callback_data=f"amount_{coin}")],
            [InlineKeyboardButton("⚖️ Плечо", callback_data=f"leverage_{coin}")],
            [InlineKeyboardButton("🎯 TP/SL", callback_data=f"tpsl_{coin}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Настройки {coin}:", reply_markup=reply_markup)

    elif data == "back":
        await start(update, context)  # Обновить главное меню

    elif data == "stats":
        total = stats["total_pnl"]
        daily = stats.get("daily_pnl", {}).get(time.strftime("%Y-%m-%d"), 0)
        per_coin_text = "\n".join([f"{c}: {stats['per_coin'][c]:+.2f} USDT" for c in COINS])
        await query.edit_message_text(f"Статистика:\nОбщая P&L: {total:+.2f} USDT\nСегодня: {daily:+.2f} USDT\nПо коинам:\n{per_coin_text}")

    elif data == "balance":
        balance = await get_futures_balance()
        await query.edit_message_text(f"Баланс Futures: {balance:,.2f} USDT")

    elif data == "restart":
        await query.edit_message_text("Перезапуск бота...")
        asyncio.create_task(restart_bot())

    # Обработчики настроек (пример для toggle, amount и т.д. — расширь по аналогии)
    elif data.startswith("toggle_"):
        coin = data.split("_")[1]
        settings[coin]["enabled"] = not settings[coin]["enabled"]
        save_settings(settings)
        await button_handler(update, context)  # Обновить меню

    # Для amount, leverage, tpsl — аналогично, с inline клавиатурой для выбора значений

async def restart_bot():
    os.execv(sys.executable, ['python'] + sys.argv)  # Перезапуск

# ====================== FASTAPI + WEBHOOK ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск Telegram бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    yield
    await application.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>Multi-Coin Bot — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        if data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(status_code=403)
        signal = data.get("signal", "").lower()
        coin = data.get("coin", "XRP").upper()  # По умолчанию XRP, или из сигнала
        if coin not in COINS:
            return {"error": "Unknown coin"}

        if signal in ["buy", "long"]:
            await open_long(coin)
        elif signal == "close_all":
            await close_all(coin)
        return {"ok": True, "coin": coin}
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}
