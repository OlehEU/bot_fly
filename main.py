# main.py
import os
import json
import asyncio
import logging
import time
import traceback
from contextlib import asynccontextmanager
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from slowapi import Limiter
from slowapi.util import get_remote_address

# === НАСТРОЙКА ЛОГИРОВАНИЯ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# === КОНФИГУРАЦИЯ ===
REQUEST_TIMEOUT = 30          # Уменьшено с 60 → быстрее отработка
MAX_RETRIES = 3
RETRY_DELAY = 5
RATE_LIMIT = "10/minute"      # Защита от спама

# === СЕКРЕТЫ (с дефолтами для теста) ===
REQUIRED_SECRETS = [
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY",
    "MEXC_API_SECRET", "WEBHOOK_SECRET", "SYMBOL",
    "FIXED_AMOUNT_USDT", "LEVERAGE"
]

for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        logger.warning(f"Предупреждение: {secret} не задан! Используется дефолт.")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", 123456789))
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "default_key")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "default_secret")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_webhook_secret")
SYMBOL = os.getenv("SYMBOL", "BTC/USDT:USDT")
FIXED_AMOUNT_USDT = float(os.getenv("FIXED_AMOUNT_USDT", 10.0))
LEVERAGE = max(int(os.getenv("LEVERAGE", 5)), 1)  # минимум 1x
MAX_RISK_USDT = float(os.getenv("MAX_RISK_USDT", FIXED_AMOUNT_USDT * 3))

# --- НОРМАЛИЗАЦИЯ СИМВОЛА ---
original_symbol = SYMBOL
if '_' in SYMBOL and ':' not in SYMBOL and SYMBOL.endswith('USDT'):
    base, quote = SYMBOL.split('_')
    SYMBOL = f"{base}/{quote}:{quote}"
    logger.info(f"СИМВОЛ ИСПРАВЛЕН: {original_symbol} → {SYMBOL}")

logger.info(f"=== MEXC BOT START | {SYMBOL} | {FIXED_AMOUNT_USDT} USDT | {LEVERAGE}x ===")

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=TELEGRAM_TOKEN)
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'timeout': REQUEST_TIMEOUT * 1000,
    'sandbox': os.getenv("SANDBOX", "False").lower() == "true",
})

app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
last_trade_info = None
active_position = False
position_lock = asyncio.Lock()  # Защита от гонок

# === КОНСТАНТЫ MEXC ===
SIDE_BUY = 1
SIDE_SELL = 2
SIDE_CLOSE_LONG = 3
SIDE_CLOSE_SHORT = 4
ORDER_MARKET = 5  # Из документации: 5 = Market
MARGIN_ISOLATED = 1

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
@asynccontextmanager
async def error_handler(operation: str):
    try:
        yield
    except Exception as e:
        error_msg = f"ОШИБКА в {operation}: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg[:4000])
        except:
            pass

async def get_current_price() -> float:
    async with error_handler("get_current_price"):
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.info(f"Цена {SYMBOL}: {price:.6f}")
        return price

async def check_balance_detailed():
    async with error_handler("check_balance"):
        balance = await exchange.fetch_balance()
        total = balance['total'].get('USDT', 0)
        free = balance['free'].get('USDT', 0)
        used = balance['used'].get('USDT', 0)
        logger.info(f"Баланс: Всего {total:.4f}, Свободно {free:.4f}, Занято {used:.4f}")
        return {'total': float(total), 'free': float(free), 'used': float(used)}

async def set_leverage_fixed():
    async with error_handler("set_leverage"):
        params = {'openType': MARGIN_ISOLATED}
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL, params)
            logger.info(f"Плечо установлено: {LEVERAGE}x")
        except ccxt.ExchangeError as e:
            if 'not modified' in str(e).lower():
                logger.info("Плечо уже установлено")
            else:
                raise

async def calculate_qty() -> float:
    async with error_handler("calculate_qty"):
        price = await get_current_price()
        if SYMBOL not in exchange.markets:
            logger.warning("Рынки устарели → перезагрузка")
            await exchange.load_markets(reload=True)
            if SYMBOL not in exchange.markets:
                raise ccxt.ExchangeError(f"Символ {SYMBOL} не найден")

        market = exchange.markets[SYMBOL]
        precision = market['precision']['amount']
        quantity = (FIXED_AMOUNT_USDT * LEVERAGE) / price
        quantity = float(exchange.amount_to_precision(SYMBOL, quantity))

        min_amount = market['limits']['amount']['min'] or 0
        if min_amount and quantity < min_amount:
            quantity = min_amount
            logger.warning(f"Количество увеличено до min: {quantity}")

        order_value = quantity * price
        if order_value > MAX_RISK_USDT:
            raise ValueError(f"Риск превышает лимит: {order_value:.2f} > {MAX_RISK_USDT}")

        logger.info(f"Расчёт: {quantity} @ {price} = {order_value:.2f} USDT")
        return quantity

async def create_order_mexc_format(symbol: str, side: int, vol: float, externalOid: str, tp=None, sl=None):
    order = {
        'symbol': symbol.replace('/', '_').replace(':USDT', ''),
        'vol': vol,
        'leverage': LEVERAGE,
        'side': side,
        'type': ORDER_MARKET,
        'openType': MARGIN_ISOLATED,
        'externalOid': externalOid,
    }
    if tp: order['takeProfitPrice'] = tp
    if sl: order['stopLossPrice'] = sl
    logger.info(f"Ордер MEXC: {json.dumps(order, indent=2)}")
    return order

async def submit_order_mexc(order_data: dict):
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"Попытка {attempt + 1}/{MAX_RETRIES}")
            response = await asyncio.wait_for(
                exchange.contractPrivatePostOrderSubmit(order_data),
                timeout=REQUEST_TIMEOUT
            )
            order_id = response.get('data')
            logger.info(f"УСПЕХ! Order ID: {order_id}")
            return response
        except asyncio.TimeoutError:
            logger.warning(f"ТАЙМАУТ на попытке {attempt + 1}")
        except ccxt.NetworkError as e:
            logger.warning(f"Сеть: {e}")
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            if '510' in str(e):
                await asyncio.sleep(10)
            raise
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_DELAY)
    raise Exception("Ордер не отправлен")

# === ТОРГОВЫЕ ФУНКЦИИ ===
async def open_position_mexc(signal: str, tp_percent: float = None, sl_percent: float = None):
    global last_trade_info, active_position
    async with position_lock:
        if active_position:
            logger.warning("Позиция уже открыта!")
            await bot.send_message(TELEGRAM_CHAT_ID, "ДУБЛЬ: позиция уже активна")
            return

        async with error_handler("open_position"):
            await set_leverage_fixed()
            balance = (await check_balance_detailed())['total']
            if balance < FIXED_AMOUNT_USDT:
                raise ValueError(f"Недостаточно: {balance:.2f} < {FIXED_AMOUNT_USDT}")

            qty = await calculate_qty()
            side = SIDE_BUY if signal == "buy" else SIDE_SELL
            side_text = "BUY/LONG" if side == SIDE_BUY else "SELL/SHORT"

            entry_price = await get_current_price()
            tp_price = round(entry_price * (1 + tp_percent / 100), 6) if tp_percent else None
            sl_price = round(entry_price * (1 - sl_percent / 100), 6) if sl_percent else None

            external_oid = f"bot_{int(time.time())}_{signal}"
            order_data = await create_order_mexc_format(
                SYMBOL, side, qty, external_oid, tp_price, sl_price
            )
            response = await submit_order_mexc(order_data)
            order_id = response.get('data')

            active_position = True
            last_trade_info = {
                "signal": signal, "side": side, "vol": qty, "entry": entry_price,
                "tp": tp_price, "sl": sl_price, "order_id": order_id,
                "timestamp": time.time()
            }

            msg = (f"{side_text} ОТКРЫТА\n"
                   f"Символ: {SYMBOL}\n"
                   f"Количество: {qty}\n"
                   f"Депозит: {FIXED_AMOUNT_USDT} USDT\n"
                   f"Плечо: {LEVERAGE}x\n"
                   f"Цена: ${entry_price:.4f}\n"
                   f"{'TP: ${tp_price:.4f}' if tp_price else ''}\n"
                   f"{'SL: ${sl_price:.4f}' if sl_price else ''}\n"
                   f"Order ID: {order_id}")
            await bot.send_message(TELEGRAM_CHAT_ID, text=msg)
            logger.info("ПОЗИЦИЯ ОТКРЫТА")

async def close_position_mexc():
    global active_position, last_trade_info
    async with position_lock:
        if not active_position:
            return {"status": "error", "message": "Нет позиции"}

        async with error_handler("close_position"):
            positions = await exchange.fetch_positions([SYMBOL])
            pos = next((p for p in positions if p['symbol'] == SYMBOL and float(p['contracts']) > 0), None)
            if not pos:
                active_position = False
                return {"status": "error", "message": "Позиция не найдена"}

            side = SIDE_CLOSE_LONG if pos['side'] == 'long' else SIDE_CLOSE_SHORT
            qty = float(pos['contracts'])
            entry = float(pos['entryPrice'])
            exit_price = await get_current_price()

            external_oid = f"close_{int(time.time())}"
            order_data = await create_order_mexc_format(SYMBOL, side, qty, external_oid)
            response = await submit_order_mexc(order_data)

            pnl = ((exit_price - entry) / entry * 100 * LEVERAGE) * (1 if pos['side'] == 'long' else -1)
            realized = float(pos.get('realizedPnl', 0))

            msg = (f"ПОЗИЦИЯ ЗАКРЫТА\n"
                   f"Тип: {'LONG' if pos['side']=='long' else 'SHORT'}\n"
                   f"Вход: ${entry:.4f} → Выход: ${exit_price:.4f}\n"
                   f"P&L: {pnl:+.2f}% ({realized:+.4f} USDT)\n"
                   f"Order ID: {response.get('data')}")
            await bot.send_message(TELEGRAM_CHAT_ID, text=msg)

            active_position = False
            last_trade_info = None
            return {"status": "ok", "pnl": pnl}

# === FASTAPI ===
@app.on_event("startup")
async def startup():
    async with error_handler("startup"):
        await exchange.load_markets(reload=True)
        await set_leverage_fixed()
        price = await get_current_price()
        balance = (await check_balance_detailed())['total']
        msg = (f"MEXC BOT ЗАПУЩЕН\n"
               f"Баланс: {balance:.2f} USDT\n"
               f"Символ: {SYMBOL}\n"
               f"Сумма: {FIXED_AMOUNT_USDT} USDT\n"
               f"Плечо: {LEVERAGE}x")
        await bot.send_message(TELEGRAM_CHAT_ID, text=msg)

@app.on_event("shutdown")
async def shutdown():
    await exchange.close()
    await bot.send_message(TELEGRAM_CHAT_ID, text="Бот остановлен")

@app.post("/webhook")
@limiter.limit(RATE_LIMIT)
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, "Unauthorized")
    data = await request.json()
    signal = data.get("signal")
    if signal not in ["buy", "sell", "close"]:
        raise HTTPException(400, "signal: buy/sell/close")
    if signal == "close":
        asyncio.create_task(close_position_mexc())
    else:
        tp = data.get("tp")
        sl = data.get("sl")
        asyncio.create_task(open_position_mexc(signal, tp, sl))
    return {"status": "ok"}

@app.post("/close")
async def force_close(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401)
    return await close_position_mexc()

@app.get("/health")
async def health():
    try:
        price = await get_current_price()
        balance = await check_balance_detailed()
        pos = await exchange.fetch_positions([SYMBOL])
        return {
            "status": "healthy",
            "price": price,
            "balance": balance,
            "active": active_position,
            "position": next((p for p in pos if p['contracts'] > 0), None),
            "last_trade": last_trade_info
        }
    except:
        return {"status": "unhealthy"}

@app.get("/")
async def dashboard():
    # ... (ваш HTML, без изменений)
    return HTMLResponse("OK")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
