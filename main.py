# main.py — УНИВЕРСАЛЬНЫЙ МУЛЬТИКОИН БОТ 2026 + TELEGRAM МЕНЮ + АВТОСОЗДАНИЕ НАСТРОЕК
import os
import json
import time
import logging
import asyncio
import sys
from typing import Dict, Any, Optional
import httpx
import hmac
import hashlib
import urllib.parse
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("multi-coin-bot")

# ====================== КОНФИГ ======================
required_env = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
for var in required_env:
    if not os.getenv(var):
        raise EnvironmentError(f"ОБЯЗАТЕЛЬНАЯ переменная не задана: {var}")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "supersecret123")

# Дефолтные настройки коинов
COINS = {
    "XRP":  {"amount_usd": 10,  "leverage": 10,  "tp_percent": 0.5, "sl_percent": 1.0, "enabled": True,  "disable_tpsl": True},
    "SOL":  {"amount_usd": 15,  "leverage": 20,  "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True},
    "ETH":  {"amount_usd": 20,  "leverage": 5,   "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True},
    "BTC":  {"amount_usd": 50,  "leverage": 3,   "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True},
    "DOGE": {"amount_usd": 5,   "leverage": 50,  "tp_percent": 0.5, "sl_percent": 1.0, "enabled": False, "disable_tpsl": True}
}

SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"

# ====================== НАСТРОЙКИ И СТАТИСТИКА ======================
def load_settings() -> Dict:
    try:
        with open(SETTINGS_FILE, 'r') as f:
            saved = json.load(f)
        # Дополняем недостающими коинами и полями
        for coin, default in COINS.items():
            if coin not in saved:
                saved[coin] = default.copy()
            else:
                for k, v in default.items():
                    if k not in saved[coin]:
                        saved[coin][k] = v
        return saved
    except (FileNotFoundError, json.JSONDecodeError):
        # Создаём с дефолтами
        default_settings = {coin: data.copy() for coin, data in COINS.items()}
        save_settings(default_settings)
        return default_settings

def save_settings(settings: Dict):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

def load_stats() -> Dict:
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        default_stats = {
            "total_pnl": 0.0,
            "daily_pnl": {},
            "per_coin": {coin: 0.0 for coin in COINS}
        }
        save_stats(default_stats)
        return default_stats

def save_stats(stats: Dict):
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=2)

settings = load_settings()
stats = load_stats()

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
binance_client = httpx.AsyncClient(timeout=60.0)
last_balance_before_trade: Dict[str, float] = {}  # по коинам

# ====================== УТИЛИТЫ ======================
async def tg_send(text: str):
    try:
        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

async def tg_balance():
    balance = await get_futures_balance()
    await tg_send(f"<b>Баланс Futures:</b> <code>{balance:,.2f}</code> USDT")

# ====================== BINANCE ======================
BINANCE_BASE_URL = "https://fapi.binance.com"

def _create_signature(params: Dict[str, Any], secret: str) -> str:
    normalized = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items() if v is not None}
    query_string = urllib.parse.urlencode(normalized)
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance_request(method: str, endpoint: str, params: Optional[Dict] = None, signed: bool = True, symbol: Optional[str] = None):
    url = f"{BINANCE_BASE_URL}{endpoint}"
    params = params or {}
    if symbol:
        params["symbol"] = f"{symbol}USDT"
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _create_signature(params, BINANCE_API_SECRET)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        resp = await (binance_client.get if method == "GET" else binance_client.post)(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Binance error: {e}")
        return None

async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", signed=False, symbol=symbol)
    return float(data["price"]) if data else 0.0

async def get_qty(symbol: str) -> float:
    cfg = settings[symbol]
    price = await get_price(symbol)
    raw = (cfg["amount_usd"] * cfg["leverage"]) / price
    info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in info.get("symbols", []):
        if s["symbol"] == f"{symbol}USDT":
            prec = s.get("quantityPrecision", 3)
            min_qty = next((float(f["minQty"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"), 0)
            qty = round(raw, prec)
            return max(qty, min_qty)
    return 0.0

async def get_futures_balance() -> float:
    data = await binance_request("GET", "/fapi/v2/balance", signed=True)
    for item in data or []:
        if item["asset"] == "USDT":
            return float(item["balance"])
    return 0.0

# ====================== ТОРГОВЛЯ ======================
async def open_long(coin: str):
    if not settings[coin]["enabled"]:
        return
    try:
        qty = await get_qty(coin)
        if qty <= 0:
            return
        oid = f"{coin.lower()}_{int(time.time()*1000)}"
        entry = await get_price(coin)

        # MARKET вход
        await binance_request("POST", "/fapi/v1/order", {
            "side": "BUY", "type": "MARKET", "quantity": str(qty), "newClientOrderId": oid
        }, symbol=coin)

        last_balance_before_trade[coin] = await get_futures_balance()

        msg = f"<b>LONG открыт — {coin}</b>\n" \
              f"${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x\n" \
              f"Entry: <code>{entry:.4f}</code>"
        if settings[coin]["disable_tpsl"]:
            msg += "\n<i>TP/SL отключены</i>"
        await tg_send(msg)
        await tg_balance()
    except Exception as e:
        await tg_send(f"Ошибка открытия {coin}: {e}")

async def close_all(coin: str):
    if not settings[coin]["enabled"]:
        return
    try:
        pos = await binance_request("GET", "/fapi/v2/positionRisk", symbol=coin)
        current = next((p for p in pos if float(p.get("positionAmt", 0)) != 0), None)
        if current:
            qty = abs(float(current["positionAmt"]))
            side = "SELL" if float(current["positionAmt"]) > 0 else "BUY"
            await binance_request("POST", "/fapi/v1/order", {
                "side": side, "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"
            }, symbol=coin)

        # Отмена открытых ордеров
        orders = await binance_request("GET", "/fapi/v1/openOrders", symbol=coin)
        cancelled = 0
        if orders:
            for o in orders:
                await binance_request("DELETE", "/fapi/v1/order", {"orderId": o["orderId"]}, symbol=coin)
                cancelled += 1

        current_balance = await get_futures_balance()
        pnl = current_balance - last_balance_before_trade.get(coin, current_balance)
        stats["per_coin"][coin] = stats["per_coin"].get(coin, 0) + pnl
        stats["total_pnl"] += pnl
        save_stats(stats)

        pnl_text = f"<b>Прибыль по {coin}:</b> <code>{pnl:+.2f}</code> USDT"
        await tg_send(f"<b>{coin} закрыт</b>\nОтменено ордеров: {cancelled}\n\n{pnl_text}")
        await tg_balance()
    except Exception as e:
        await tg_send(f"Ошибка закрытия {coin}: {e}")

# ====================== TELEGRAM МЕНЮ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for coin in COINS:
        status = "ON" if settings[coin]["enabled"] else "OFF"
        keyboard.append([InlineKeyboardButton(
            f"{coin} {status} | ${settings[coin]['amount_usd']} × {settings[coin]['leverage']}x",
            callback_data=f"menu_{coin}"
        )])
    keyboard += [
        [InlineKeyboardButton("Баланс", callback_data="balance")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
        [InlineKeyboardButton("Перезапуск", callback_data="restart")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Универсальный бот 2026\nВыбери коин или действие:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("menu_"):
        coin = data.split("_")[1]
        kb = [
            [InlineKeyboardButton("ON/OFF" if not settings[coin]["enabled"] else "OFF", callback_data=f"toggle_{coin}")],
            [InlineKeyboardButton("Сумма $", callback_data=f"amount_{coin}")],
            [InlineKeyboardButton("Плечо", callback_data=f"leverage_{coin}")],
            [InlineKeyboardButton("TP/SL", callback_data=f"tpsl_{coin}")],
            [InlineKeyboardButton("Назад", callback_data="back")]
        ]
        await query.edit_message_text(f"Настройки {coin}:", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "back":
        await start(update, context)

    elif data == "balance":
        bal = await get_futures_balance()
        await query.edit_message_text(f"Баланс Futures: <code>{bal:,.2f}</code> USDT", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))

    elif data == "stats":
        text = f"<b>Статистика</b>\nОбщая P&L: <code>{stats['total_pnl']:+.2f}</code> USDT\n\nПо коинам:\n"
        for c, p in stats["per_coin"].items():
            text += f"{c}: <code>{p:+.2f}</code> USDT\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))

    elif data.startswith("toggle_"):
        coin = data.split("_")[1]
        settings[coin]["enabled"] = not settings[coin]["enabled"]
        save_settings(settings)
        await button_handler(update, context)

# ====================== FASTAPI ======================
bot = None  # будет создан в lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    bot = Bot(token=TELEGRAM_TOKEN)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await tg_send("Универсальный бот запущен и готов!")
    yield
    await application.stop()
    await application.updater.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>Multi-Coin Bot 2026 — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        if data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(403)
        signal = data.get("signal", "").lower()
        coin = data.get("coin", "XRP").upper()
        if coin not in COINS:
            return {"error": "unknown coin"}

        if signal in ["buy", "long"]:
            asyncio.create_task(open_long(coin))
        elif signal == "close_all":
            asyncio.create_task(close_all(coin))
        return {"ok": True}
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}
