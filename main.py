# main.py — ТЕРМИНАТОР 2026 | ПОЛНОЕ МЕНЮ + УПРАВЛЕНИЕ СКАНЕРОМ
import os
import json
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from contextlib import asynccontextmanager
import tempfile

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("OZ2026")

# ====== ENV ======
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        raise EnvironmentError(f"Нет {v}")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
SECRET = os.getenv("WEBHOOK_SECRET")
SCANNER_URL = os.getenv("SCANNER_BASE", "https://scanner-fly-oz.fly.dev")
BOT_URL = os.getenv("BOT_BASE", "https://bot-fly-oz.fly.dev")

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]
TF_LIST = ["1m", "3m", "5m", "15m", "30m", "1h"]

SIGNAL_LOG_FILE = "signal_log.json"
signal_log = []

client = httpx.AsyncClient(timeout=20.0)
bot: Bot = None

# ====== JSON ======
def save_log():
    with open(SIGNAL_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(signal_log[-500:], f, ensure_ascii=False, indent=2)

try:
    with open(SIGNAL_LOG_FILE, "r", encoding="utf-8") as f:
        signal_log = json.load(f)
except:
    signal_log = []

# ====== Binance ======
def sign(p: Dict) -> str:
    q = urllib.parse.urlencode({k: str(v).lower() if isinstance(v,bool) else str(v) for k,v in p.items() if v is not None})
    return hmac.new(BINANCE_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

async def binance_get(ep: str, params: Dict = None):
    p = params or {}
    p["timestamp"] = int(time.time()*1000)
    p["signature"] = sign(p)
    async with client:
        r = await client.get(f"https://fapi.binance.com{ep}", params=p, headers={"X-MBX-APIKEY": BINANCE_KEY})
        r.raise_for_status()
        return r.json()

async def get_balance() -> float:
    data = await binance_get("/fapi/v2/balance")
    for a in data:
        if a["asset"] == "USDT":
            return float(a["balance"])
    return 0.0

async def tg(text: str):
    global bot
    if bot:
        try:
            await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.error(f"TG: {e}")

# ====== FastAPI + Lifespan ======
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(btn))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    bot = app.bot
    await tg("ТЕРМИНАТОР 2026 ONLINE")
    log.info("BOT UP")
    yield
    await app.stop()

app = FastAPI(lifespan=lifespan)

# ====== Роуты ======
@app.get("/")
async def root(): return {"status": "OZ 2026 ALIVE"}

@app.get("/logs")
async def logs_page():
    rows = "".join(f"<tr><td>{e.get('date','—')}</td><td>{e.get('time','—')}</td><td><b>{e.get('coin')}</b></td><td>{e.get('action')}</td></tr>" for e in signal_log[-200:])
    return Response(f"<html><body bgcolor=#0d1117 text=#c9d1d9><h1 align=center>СИГНАЛЫ OZ 2026</h1><table width=90% align=center border=1 cellpadding=10 cellspacing=0><tr bgcolor=#21262d><th>Дата</th><th>Время</th><th>Монета</th><th>Сигнал</th></tr>{rows}</table><center><a href='/scanner'><button style='padding:15px;background:#238636;color:white;border:none;border-radius:8px;font-size:18px'>СКАНЕР</button></a></center></body></html>", media_type="text/html")

@app.get("/scanner")
async def scanner_page():
    try:
        s = (await client.get(f"{SCANNER_URL}/scanner_status", timeout=6)).json()
    except:
        s = {"online":False, "enabled":False, "coins":{}, "tf":{c:"—" for c in COINS}}
    rows = "".join(f"<tr><td><b>{c}</b></td><td>{'ВКЛ' if s['coins'].get(c,True) else 'ВЫКЛ'}</td><td>{s['tf'].get(c,'—')}</td></tr>" for c in COINS)
    return Response(f"<html><body bgcolor=#0d1117 text=#c9d1d9><h1 align=center>СКАНЕР OZ 2026</h1><p align=center><b>Статус:</b> {'<font color=lime>ОНЛАЙН</font>' if s.get('online') else '<font color=red>ОФФЛАЙН</font>'} | <b>Режим:</b> {'<font color=lime>ВКЛ</font>' if s.get('enabled') else '<font color=red>ВЫКЛ</font>'}</p><table width=90% align=center border=1 cellpadding=10 cellspacing=0><tr bgcolor=#21262d><th>Монета</th><th>Статус</th><th>ТФ</th></tr>{rows}</table><center><a href='/logs'><button style='padding:15px;background:#238636;color:white;border:none;border-radius:8px;font-size:18px'>ЛОГИ</button></a></center></body></html>", media_type="text/html")

@app.post("/webhook")
async def webhook(req: Request):
    if req.headers.get("Authorization") != f"Bearer {SECRET}":
        raise HTTPException(403)
    data = await req.json()
    signal = data.get("signal","").lower()
    coin = data.get("coin","").upper()
    if coin not in COINS: return {"error":"bad coin"}

    entry = {"date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M:%S"), "coin": coin, "action": "LONG" if "buy" in signal or "long" in signal else "CLOSE"}
    signal_log.append(entry)
    save_log()

    if "buy" in signal or "long" in signal:
        await tg(f"LONG {coin}USDT\nСканер OZ 2026 дал сигнал!")
    elif "close" in signal:
        await tg(f"ЗАКРЫТИЕ {coin}USDT")
    return {"ok": True}

# ====== ТЕЛЕГРАМ МЕНЮ ======
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Баланс", callback_data="bal"), InlineKeyboardButton("Статистика", callback_data="stats")],
        [InlineKeyboardButton("Управление сканером", callback_data="scanner_menu")],
        [InlineKeyboardButton("Логи", url=f"{BOT_URL}/logs"), InlineKeyboardButton("Сканер", url=f"{BOT_URL}/scanner")],
    ])

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("ТЕРМИНАТОР 2026\nГотов к бою, командир!", reply_markup=main_menu())

async def btn(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()

    if q.data == "back":
        await q.edit_message_text("Главное меню", reply_markup=main_menu())
        return

    if q.data == "bal":
        b = await get_balance()
        await q.edit_message_text(f"Баланс: <code>{b:,.2f}</code> USDT", parse_mode="HTML", reply_markup=main_menu())

    elif q.data == "stats":
        await q.edit_message_text("Статистика в разработке\nСкоро будет PnL, профит-фактор и т.д.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))

    elif q.data == "scanner_menu":
        try:
            s = (await client.get(f"{SCANNER_URL}/scanner_status")).json()
        except:
            s = {"enabled": False, "coins": {c: True for c in COINS}, "tf": {c:"5m" for c in COINS}}

        keyboard = [
            [InlineKeyboardButton("ВКЛ СКАНЕР" if not s.get("enabled") else "ВЫКЛ СКАНЕР", callback_data="toggle_global")],
            [InlineKeyboardButton("XRP", callback_data="coin_XRP"), InlineKeyboardButton("SOL", callback_data="coin_SOL")],
            [InlineKeyboardButton("ETH", callback_data="coin_ETH"), InlineKeyboardButton("BTC", callback_data="coin_BTC")],
            [InlineKeyboardButton("DOGE", callback_data="coin_DOGE")],
            [InlineKeyboardButton("Назад", callback_data="back")]
        ]
        text = f"УПРАВЛЕНИЕ СКАНЕРОМ OZ 2026\n\nГлобально: {'ВКЛ' if s.get('enabled') else 'ВЫКЛ'}\n\nНажми на монету → вкл/выкл + смена ТФ"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif q.data == "toggle_global":
        await client.post(f"{BOT_URL}/toggle_scanner", headers={"Authorization": f"Bearer {SECRET}"})
        await btn(u, c)

    elif q.data.startswith("coin_"):
        coin = q.data.split("_")[1]
        try:
            s = (await client.get(f"{SCANNER_URL}/scanner_status")).json()
        except:
            s = {"coins": {c: True for c in COINS}, "tf": {c:"5m" for c in COINS}}

        enabled = s["coins"].get(coin, True)
        current_tf = s["tf"].get(coin, "5m")

        tf_buttons = []
        row = []
        for tf in TF_LIST:
            row.append(InlineKeyboardButton(f"{tf} ✓" if tf == current_tf else tf, callback_data=f"settf_{coin}_{tf}"))
            if len(row) == 3:
                tf_buttons.append(row)
                row = []
        if row: tf_buttons.append(row)

        kb = [
            [InlineKeyboardButton("ВЫКЛ" if enabled else "ВКЛ", callback_data=f"togglecoin_{coin}")],
            *tf_buttons,
            [InlineKeyboardButton("Назад", callback_data="scanner_menu")]
        ]
        await q.edit_message_text(f"Настройка {coin}\nСейчас: {'ВКЛ' if enabled else 'ВЫКЛ'} | ТФ: {current_tf}", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data.startswith("togglecoin_"):
        coin = q.data.split("_")[1]
        await client.post(f"{SCANNER_URL}/toggle_coin", json={"coin": coin}, headers={"Authorization": f"Bearer {SECRET}"})
        await btn(u, c)

    elif q.data.startswith("settf_"):
        parts = q.data.split("_")
        coin, tf = parts[1], parts[2]
        await client.post(f"{SCANNER_URL}/set_tf", json={"coin": coin, "tf": tf}, headers={"Authorization": f"Bearer {SECRET}"})
        await tg(f"{coin} → таймфрейм {tf}")
        await btn(u, c)
