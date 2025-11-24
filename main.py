# main.py — ТЕРМИНАТОР 2026 | ФИНАЛЬНАЯ ВЕРСИЯ ДЛЯ ПОРТА 8000
import os
import json
import time
import logging
import asyncio
from typing import Dict
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("OZ2026")

# ==================== ENV ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        raise EnvironmentError(f"Нет переменной: {v}")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")
SECRET = os.getenv("WEBHOOK_SECRET")
BOT_BASE = os.getenv("BOT_BASE", "https://bot-fly-oz.fly.dev")

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]
TF_LIST = ["1m", "3m", "5m", "15m", "30m", "1h"]

SIGNAL_LOG_FILE = "signal_log.json"
CONFIG_FILE = "scanner_config.json"

# ==================== JSON ====================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default

signal_log = load_json(SIGNAL_LOG_FILE, [])
scanner_config = load_json(CONFIG_FILE, {c: "5m" for c in COINS})

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==================== BINANCE BALANCE ====================
import hmac, hashlib, urllib.parse
def sign(p: Dict):
    q = urllib.parse.urlencode({k: str(v) for k,v in p.items() if v is not None})
    return hmac.new(BINANCE_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

async def get_balance() -> float:
    try:
        ts = int(time.time()*1000)
        sig = sign({"timestamp": ts})
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://fapi.binance.com/fapi/v2/balance?timestamp={ts}&signature={sig}",
                          headers={"X-MBX-APIKEY": BINANCE_KEY})
            for a in r.json():
                if a["asset"] == "USDT":
                    return float(a["balance"])
    except:
        pass
    return 0.0

# ==================== TG ====================
bot: Bot = None
async def tg(text: str):
    global bot
    if bot:
        try:
            await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.error(f"TG send error: {e}")

# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    app_tg = Application.builder().token(TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CallbackQueryHandler(button))
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling()
    bot = app_tg.bot
    await tg("ТЕРМИНАТОР 2026 ОНЛАЙН")
    log.info("Bot started")
    yield
    await app_tg.stop()

app = FastAPI(lifespan=lifespan)

# ==================== WEB ====================
STYLE = "<style>body{background:#0d1117;color:#c9d1d9;font-family:Arial;text-align:center;padding:20px;}table{margin:30px auto;width:90%;border-collapse:collapse;}th,td{border:1px solid #30363d;padding:12px;}th{background:#21262d;color:#58a6ff;}tr:nth-child(even){background:#161b22;}</style>"

@app.get("/")
async def root():
    return {"status": "OZ 2026 ALIVE", "port": 8000}

@app.get("/logs")
async def logs():
    rows = "".join(f"<tr><td>{e.get('date','—')}</td><td>{e.get('time','—')}</td><td><b>{e.get('coin')}</b></td><td>{e.get('action')}</td></tr>" for e in signal_log[-200:])
    return Response(f"<html><head><title>ЛОГИ</title>{STYLE}</head><body><h1>СИГНАЛЫ 2026</h1><table><tr><th>Дата</th><th>Время</th><th>Монета</th><th>Сигнал</th></tr>{rows}</table></body></html>", media_type="text/html")

# ==================== САМЫЙ ВАЖНЫЙ WEBHOOK — ЛОВИТ ВСЁ ====================
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {SECRET}":
        raise HTTPException(403)

    try:
        data = await request.json()
    except:
        raise HTTPException(400, "Invalid JSON")

    coin = data.get("coin", "").upper().strip()
    signal = data.get("signal", "").lower().strip()

    if coin not in COINS:
        return {"error": "bad coin"}

    # ЛОВИМ ВСЕ ВАРИАНТЫ ЗАКРЫТИЯ
    is_close = signal in ["close", "close_all", "sell", "exit", "closeall", "stop"]
    is_long  = signal in ["buy", "long", "open", "entry"]

    action = "LONG" if is_long else "CLOSE" if is_close else "UNKNOWN"

    if action == "UNKNOWN":
        return {"warning": "unknown signal", "received": signal}

    # Запись в лог
    entry = {
        "date": time.strftime("%Y-%m-%d"),
        "time": time.strftime("%H:%M:%S"),
        "coin": coin,
        "action": action
    }
    signal_log.append(entry)
    save_json(SIGNAL_LOG_FILE, signal_log)

    # Отправка в Telegram
    if action == "LONG":
        await tg(f"LONG {coin}USDT\nСканер OZ 2026 дал сигнал!")
    elif action == "CLOSE":
        await tg(f"ЗАКРЫТИЕ {coin}USDT")

    return {"ok": True, "coin": coin, "action": action}

# ==================== ТЕЛЕГРАМ МЕНЮ ====================
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Баланс", callback_data="bal")],
        [InlineKeyboardButton("Сканер — выбор ТФ", callback_data="scanner")],
        [InlineKeyboardButton("Логи", url=f"{BOT_BASE}/logs")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ТЕРМИНАТОР 2026\nГотов к бою!", reply_markup=menu())

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "bal":
        b = await get_balance()
        await q.edit_message_text(f"Баланс: <code>{b:,.2f}</code> USDT", parse_mode="HTML", reply_markup=menu())

    elif q.data == "scanner":
        kb = [[InlineKeyboardButton(f"{c} → {scanner_config.get(c,'—')}", callback_data=f"tf_{c}")] for c in COINS]
        kb.append([InlineKeyboardButton("Назад", callback_data="back")])
        await q.edit_message_text("Выбери монету для смены таймфрейма:", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data.startswith("tf_"):
        coin = q.data[3:]
        kb = [[InlineKeyboardButton(tf + (" CURRENT" if scanner_config.get(coin) == tf else ""), callback_data=f"set_{coin}_{tf}")] for tf in TF_LIST]
        kb.append([InlineKeyboardButton("Назад", callback_data="scanner")])
        await q.edit_message_text(f"{coin}\nТекущий: {scanner_config.get(coin)}", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data.startswith("set_"):
        _, coin, tf = q.data.split("_", 2)
        scanner_config[coin] = tf
        save_json(CONFIG_FILE, scanner_config)
        await tg(f"{coin} → таймфрейм <b>{tf}</b>")
        await button(update, context)  # обновить

    elif q.data == "back":
        await q.edit_message_text("Главное меню", reply_markup=menu())

# ==================== ЗАПУСК НА 8000 ПОРТУ ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
