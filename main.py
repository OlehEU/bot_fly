# main.py — РАБОЧАЯ ВЕРСИЯ от 17.11.2025 (XRP LONG 10$ × 10x)
import os
import logging
import asyncio
import math
from typing import Dict
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ====================== КОНФИГУРАЦИЯ ======================
for var in ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]:
    if not os.getenv(var):
        raise EnvironmentError(f"Переменная {var} не установлена!")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Настройки торговли
FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))   # 10$
LEVERAGE = int(os.getenv("LEVERAGE", "10"))                     # 10x
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))              # +0.5%
SL_PERCENT = float(os.getenv("SL_PERCENT", "1.0"))              # -1%
AUTO_CLOSE_MINUTES = 10
BASE_COIN = "XRP"  # можно менять на BTC, ETH, SOL и т.д.

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# ====================== TELEGRAM ======================
bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        logger.info("Telegram: сообщение отправлено")
    except Exception as e:
        logger.error(f"Telegram ошибка: {e}")

# ====================== MEXC ======================
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',           # Фьючерсы
        'adjustForTimeDifference': True,
    },
    'timeout': 15000,
})

# Глобальный кэш символов
_cached_markets: Dict[str, str] = {}

async def resolve_symbol(base: str) -> str:
    global _cached_markets
    if not _cached_markets:
        await exchange.load_markets()
        _cached_markets = {
            s.split("/")[0]: s for s in exchange.markets.keys() if s.endswith(":USDT")
        }
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise ValueError(f"Не найден символ для {base}")
    return symbol

# ====================== УТИЛИТЫ ======================
async def fetch_price(symbol: str) -> float:
    ticker = await exchange.fetch_ticker(symbol)
    return float(ticker['last'])

async def get_balance_usdt() -> float:
    bal = await exchange.fetch_balance()
    return float(bal['total'].get('USDT', 0))

async def calculate_quantity(symbol: str) -> float:
    price = await fetch_price(symbol)
    if price <= 0:
        raise Exception("Не удалось получить цену")
    
    await exchange.load_markets()
    market = exchange.markets[symbol]
    info = market.get('info', {})
    contract_size = float(info.get('contractSize', 1))
    min_qty = float(info.get('minQuantity', 1))
    
    # 10$ × 10x = 100$ номинал
    raw_qty = (FIXED_AMOUNT_USD * LEVERAGE) / price / contract_size
    qty = math.ceil(raw_qty * 10) / 10  # округление до 0.1
    qty = max(qty, min_qty)
    
    logger.info(f"Расчёт qty: {qty} контрактов при цене {price}")
    return qty

# ====================== ОТКРЫТИЕ ПОЗИЦИИ ======================
async def open_long_position():
    global position_active
    if position_active:
        await tg_send("⚠️ Позиция уже открыта!")
        return

    try:
        symbol = await resolve_symbol(BASE_COIN)
        qty = await calculate_quantity(symbol)

        # Установка плеча
        await exchange.set_leverage(LEVERAGE, symbol)

        # Проверка баланса
        usdt = await get_balance_usdt()
        if usdt < 2:
            await tg_send(f"❌ Недостаточно USDT: {usdt:.2f}")
            return

        # Рыночный ордер
        params = {
            'openType': 1,           # isolated
            'positionType': 1,       # one-way
            'leverage': LEVERAGE,
        }

        logger.info(f"Открываем LONG {qty} {symbol}")
        order = await exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=qty,
            params=params
        )

        entry_price = await fetch_price(symbol)
        tp_price = round(entry_price * (1 + TP_PERCENT / 100), 4)
        sl_price = round(entry_price * (1 - SL_PERCENT / 100), 4)

        # TP и SL (лимитные)
        await exchange.create_order(symbol, 'limit', 'sell', qty, tp_price, {'reduceOnly': True, 'stopPrice': tp_price})
        await exchange.create_order(symbol, 'limit', 'sell', qty, sl_price, {'reduceOnly': True, 'stopPrice': sl_price})

        position_active = True

        msg = f"""
🚀 <b>LONG ОТКРЫТ</b>
<b>{symbol}</b> | ${FIXED_AMOUNT_USD} × {LEVERAGE}x
📍 Entry: <code>{entry_price:.4f}</code>
🎯 TP (+{TP_PERCENT}%): <code>{tp_price:.4f}</code>
🛑 SL (-{SL_PERCENT}%): <code>{sl_price:.4f}</code>
⏱ Автозакрытие: через {AUTO_CLOSE_MINUTES} мин
        """
        await tg_send(msg.strip())

        # Автозакрытие
        asyncio.create_task(auto_close_after_timeout(symbol, qty))

    except Exception as e:
        err = str(e)
        logger.error(f"Ошибка открытия позиции: {err}")
        await tg_send(f"❌ Ошибка открытия LONG:\n<code>{err}</code>")
        position_active = False

# ====================== АВТОЗАКРЫТИЕ ======================
async def auto_close_after_timeout(symbol: str, qty: float):
    await asyncio.sleep(AUTO_CLOSE_MINUTES * 60)
    if not position_active:
        return
    try:
        await exchange.create_order(symbol, 'market', 'sell', qty, params={'reduceOnly': True})
        await tg_send("⏰ Автозакрытие: позиция закрыта по рынку")
    except Exception as e:
        await tg_send(f"❌ Ошибка автозакрытия: {e}")
    finally:
        global position_active
        position_active = False

# ====================== FASTAPI ======================
position_active = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_send(f"Bot started | {BASE_COIN} Long | ${FIXED_AMOUNT_USD} | {LEVERAGE}x | TP +{TP_PERCENT}% | SL -{SL_PERCENT}%")
    yield
    await exchange.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>🤖 MEXC XRP Bot — ONLINE</h1><p>Готов к приёму сигналов</p>")

@app.get("/status")
async def status():
    pos = "Открыта" if position_active else "Нет"
    return {"coin": BASE_COIN, "position": pos, "leverage": LEVERAGE}

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403, "Неверный секрет")
    
    data = await request.json()
    if data.get("signal") == "buy":
        await tg_send("📨 BUY signal received")
        asyncio.create_task(open_long_position())
    
    return {"status": "ok"}

# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
