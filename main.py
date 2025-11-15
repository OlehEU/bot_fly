# main.py — MEXC Futures Bot (Async HTTPS + httpx)
import os
import json
import asyncio
import logging
import time
import traceback
import hmac
import hashlib
from contextlib import asynccontextmanager
import ccxt.async_support as ccxt
import httpx
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
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5
RATE_LIMIT = "10/minute"

# === СЕКРЕТЫ ===
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
LEVERAGE = max(int(os.getenv("LEVERAGE", 5)), 1)
MAX_RISK_USDT = float(os.getenv("MAX_RISK_USDT", FIXED_AMOUNT_USDT * 3))

# --- НОРМАЛИЗАЦИЯ СИМВОЛА ---
def normalize_symbol_for_api(ccxt_symbol: str) -> str:
    return ccxt_symbol.replace('/', '_').replace(':USDT', '')

original_symbol = SYMBOL
if '_' in SYMBOL and ':' not in SYMBOL:
    base, quote = SYMBOL.split('_')
    SYMBOL = f"{base}/{quote}:{quote}"
    logger.info(f"СИМВОЛ ИСПРАВЛЕН: {original_symbol} → {SYMBOL}")

MEXC_SYMBOL = normalize_symbol_for_api(SYMBOL)
logger.info(f"=== MEXC BOT | {SYMBOL} (API: {MEXC_SYMBOL}) | {FIXED_AMOUNT_USDT} USDT | {LEVERAGE}x ===")

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

# === ГЛОБАЛЬНЫЕ ===
last_trade_info = None
active_position = False
position_lock = asyncio.Lock()

# === КОНСТАНТЫ MEXC ===
SIDE_BUY = 1
SIDE_SELL = 2
SIDE_CLOSE_LONG = 3
SIDE_CLOSE_SHORT = 4
ORDER_MARKET = 5
MARGIN_ISOLATED = 1
POSITION_LONG = 1
POSITION_SHORT = 2

# === ВСПОМОГАТЕЛЬНЫЕ ===
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
        params_template = {'openType': MARGIN_ISOLATED}
        try:
            # LONG
            params = params_template.copy()
            params['positionType'] = POSITION_LONG
            await exchange.set_leverage(LEVERAGE, SYMBOL, params=params)
            logger.info(f"Плечо {LEVERAGE}x установлено для LONG")

            # SHORT
            params = params_template.copy()
            params['positionType'] = POSITION_SHORT
            await exchange.set_leverage(LEVERAGE, SYMBOL, params=params)
            logger.info(f"Плечо {LEVERAGE}x установлено для SHORT")
        except ccxt.ExchangeError as e:
            if 'not modified' in str(e).lower():
                logger.info("Плечо уже установлено")
            else:
                logger.warning(f"Ошибка установки плеча: {e}")

async def calculate_qty() -> float:
    async with error_handler("calculate_qty"):
        price = await get_current_price()
        if SYMBOL not in exchange.markets:
            logger.warning("Рынки устарели → перезагрузка")
            await exchange.load_markets(reload=True)
            if SYMBOL not in exchange.markets:
                raise ccxt.ExchangeError(f"Символ {SYMBOL} не найден")

        market = exchange.markets[SYMBOL]
        quantity = (FIXED_AMOUNT_USDT * LEVERAGE) / price
        quantity = float(exchange.amount_to_precision(SYMBOL, quantity))

        min_amount = market['limits']['amount']['min'] or 0
        if min_amount and quantity < min_amount:
            quantity = min_amount
            logger.warning(f"Количество увеличено до min: {quantity}")

        order_value = quantity * price
        MIN_NOTIONAL = 5.0
        if order_value < MIN_NOTIONAL:
            quantity = (MIN_NOTIONAL / price)
            quantity = float(exchange.amount_to_precision(SYMBOL, quantity))
            order_value = quantity * price
            logger.warning(f"Количество скорректировано для min notional {MIN_NOTIONAL} USDT")

        if order_value > MAX_RISK_USDT:
            raise ValueError(f"Риск превышает лимит: {order_value:.2f} > {MAX_RISK_USDT}")

        logger.info(f"Расчёт: {quantity} @ {price} = {order_value:.2f} USDT")
        return quantity

async def create_order_mexc_format(side: int, vol: float, externalOid: str, tp=None, sl=None, is_close=False):
    order = {
        'symbol': MEXC_SYMBOL,
        'vol': vol,
        'leverage': LEVERAGE,
        'side': side,
        'type': ORDER_MARKET,
        'openType': MARGIN_ISOLATED,
        'positionMode': 2,
        'externalOid': externalOid,
    }
    if is_close:
        order['reduceOnly'] = True
    if tp:
        order['takeProfitPrice'] = round(tp, 6)
    if sl:
        order['stopLossPrice'] = round(sl, 6)
    logger.info(f"Ордер MEXC: {json.dumps(order, indent=2)}")
    return order

# === АСИНХРОННЫЙ HTTPS К MEXC API (httpx) ===
def _sign_mexc_request(access_key: str, secret_key: str, body: str) -> tuple[str, str]:
    timestamp = str(int(time.time() * 1000))
    sign_str = access_key + timestamp + body
    signature = hmac.new(
        secret_key.encode('utf-8'),
        sign_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return timestamp, signature

async def submit_order_mexc(order_data: dict):
    """Асинхронный HTTPS запрос — НЕ БЛОКИРУЕТ event loop"""
    url = "https://contract.mexc.com/api/v1/private/order/submit"
    body = json.dumps(order_data)
    timestamp, signature = _sign_mexc_request(MEXC_API_KEY, MEXC_API_SECRET, body)

    headers = {
        'ApiKey': MEXC_API_KEY,
        'Request-Time': timestamp,
        'Signature': signature,
        'Content-Type': 'application/json'
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(f"Попытка {attempt + 1}/{MAX_RETRIES} (async HTTPS)")
                response = await client.post(url, headers=headers, content=body)
                result = response.json()

                if result.get('success'):
                    order_id = result.get('data', 'unknown')
                    logger.info(f"УСПЕХ! Order ID: {order_id}")
                    return result
                else:
                    code = result.get('code', 'unknown')
                    msg = result.get('message', 'no message')
                    logger.error(f"MEXC API Error {code}: {msg}")
                    if code == 510:
                        await asyncio.sleep(10)
                    raise Exception(f"API Error {code}: {msg}")

            except httpx.TimeoutException:
                logger.warning(f"ТАЙМАУТ на попытке {attempt + 1}")
            except Exception as e:
                logger.error(f"Async HTTPS Error: {e}")
                raise

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)

    raise Exception("Ордер не отправлен")

# === ТОРГОВЛЯ ===
async def open_position_mexc(signal: str, tp_percent: float = None, sl_percent: float = None):
    global last_trade_info, active_position
    async with position_lock:
        if active_position:
            logger.warning("Позиция уже открыта!")
            await bot.send_message(TELEGRAM_CHAT_ID, "ДУБЛЬ СИГНАЛА — позиция активна")
            return

        async with error_handler("open_position"):
            await set_leverage_fixed()
            await asyncio.sleep(1)

            balance_data = await check_balance_detailed()
            if balance_data['free'] < FIXED_AMOUNT_USDT:
                raise ValueError(f"Недостаточно средств: {balance_data['free']:.2f} < {FIXED_AMOUNT_USDT}")

            qty = await calculate_qty()
            side = SIDE_BUY if signal.lower() == "buy" else SIDE_SELL
            side_text = "LONG" if side == SIDE_BUY else "SHORT"

            entry_price = await get_current_price()
            tp_price = entry_price * (1 + tp_percent / 100) if tp_percent else None
            sl_price = entry_price * (1 - sl_percent / 100) if sl_percent else None

            external_oid = f"bot_open_{int(time.time())}_{signal}"
            order_data = await create_order_mexc_format(side, qty, external_oid, tp_price, sl_price)
            response = await submit_order_mexc(order_data)
            order_id = response.get('data', 'unknown')

            active_position = True
            last_trade_info = {
                "signal": signal, "side": side_text, "qty": qty, "entry": entry_price,
                "tp": tp_price, "sl": sl_price, "order_id": order_id, "timestamp": time.time()
            }

            position_size = qty * entry_price
            msg = (f"{side_text} ОТКРЫТА\n"
                   f"Символ: {SYMBOL}\n"
                   f"Количество: {qty}\n"
                   f"Размер: {position_size:.2f} USDT\n"
                   f"Плечо: {LEVERAGE}x\n"
                   f"Цена: ${entry_price:.4f}\n"
                   f"{'TP: ${tp_price:.4f}' if tp_price else ''}\n"
                   f"{'SL: ${sl_price:.4f}' if sl_price else ''}\n"
                   f"Order ID: {order_id}")
            await bot.send_message(TELEGRAM_CHAT_ID, text=msg)
            logger.info(f"ПОЗИЦИЯ ОТКРЫТА: {side_text}")

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
            entry_price = float(pos['entryPrice'])
            exit_price = await get_current_price()
            realized_pnl = float(pos.get('realizedPnl', 0))

            pnl_percent = ((exit_price - entry_price) / entry_price * 100 * LEVERAGE) * (1 if pos['side'] == 'long' else -1)

            external_oid = f"bot_close_{int(time.time())}"
            order_data = await create_order_mexc_format(side, qty, external_oid, is_close=True)
            response = await submit_order_mexc(order_data)
            close_order_id = response.get('data', 'unknown')

            msg = (f"ПОЗИЦИЯ ЗАКРЫТА ({pos['side'].upper()})\n"
                   f"Символ: {SYMBOL}\n"
                   f"Количество: {qty}\n"
                   f"Вход: ${entry_price:.4f} → Выход: ${exit_price:.4f}\n"
                   f"P&L: {pnl_percent:+.2f}% | {realized_pnl:+.4f} USDT\n"
                   f"Close Order ID: {close_order_id}")
            await bot.send_message(TELEGRAM_CHAT_ID, text=msg)

            active_position = False
            last_trade_info = None
            logger.info(f"ПОЗИЦИЯ ЗАКРЫТА: PnL {pnl_percent:+.2f}%")
            return {"status": "ok", "pnl_percent": pnl_percent, "pnl_usdt": realized_pnl}

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
               f"Символ: {SYMBOL} (API: {MEXC_SYMBOL})\n"
               f"Сумма: {FIXED_AMOUNT_USDT} USDT\n"
               f"Плечо: {LEVERAGE}x\n"
               f"{'SANDBOX' if exchange.sandbox else 'LIVE'}")
        await bot.send_message(TELEGRAM_CHAT_ID, text=msg)
        logger.info("БОТ ГОТОВ")

@app.on_event("shutdown")
async def shutdown():
    await exchange.close()
    await bot.send_message(TELEGRAM_CHAT_ID, text="Бот остановлен")

@app.post("/webhook")
@limiter.limit(RATE_LIMIT)
async def webhook(request: Request):
    logger.info("Webhook получен")
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, "Unauthorized")
    data = await request.json()
    signal = data.get("signal")
    if signal not in ["buy", "sell", "close"]:
        raise HTTPException(400, "signal: buy/sell/close")
    if signal == "close":
        asyncio.create_task(close_position_mexc())
    else:
        tp = data.get("tp_percent")
        sl = data.get("sl_percent")
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
        positions = await exchange.fetch_positions([SYMBOL])
        pos_info = next((p for p in positions if p['symbol'] == SYMBOL and float(p['contracts']) > 0), None)
        return {
            "status": "healthy",
            "price": price,
            "balance": balance,
            "active": active_position,
            "position": pos_info,
            "last_trade": last_trade_info
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

@app.get("/")
async def dashboard():
    try:
        balance_data = await check_balance_detailed()
        price = await get_current_price()
        positions = await exchange.fetch_positions([SYMBOL])
        pos_details = next((p for p in positions if p['symbol'] == SYMBOL and float(p['contracts']) > 0), None)
        
        status = "АКТИВНА" if active_position else "НЕТ"
        html = f"""
        <html><head><title>MEXC Bot</title>
        <style>body{{font-family:Arial;background:#1e1e1e;color:white;padding:20px;}}
        .card{{background:#2d2d2d;padding:20px;margin:10px 0;border-radius:10px;}}
        .success{{color:#00b894;}} .warning{{color:#fdcb6e;}} .info{{color:#74b9ff;}}
        button{{background:#00b894;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;}}
        .danger-btn{{background:#e74c3c;}} pre{{background:#1a1a1a;padding:15px;border-radius:5px;}}
        </style></head><body>
        <h1>MEXC Futures Bot</h1>
        <div class="card"><h3>БАЛАНС</h3>
        <p>Всего: <span class="info">{balance_data['total']:.2f}</span> USDT</p>
        <p>Свободно: {balance_data['free']:.2f} USDT</p></div>
        <div class="card"><h3>РЫНОК</h3>
        <p>Символ: <span class="info">{SYMBOL}</span></p>
        <p>Цена: <span class="info">${price:.4f}</span></p>
        <p>Позиция: <span class="{'success' if active_position else 'warning'}">{status}</span></p></div>
        """
        if pos_details:
            pnl_class = "success" if pos_details['unrealizedPnl'] > 0 else "danger"
            html += f"<div class='card'><h3>ПОЗИЦИЯ</h3><p>Сторона: <span class='{pnl_class}'>{pos_details['side'].upper()}</span></p></div>"
        html += f"<div class='card'><h3>НАСТРОЙКИ</h3><p>Сумма: {FIXED_AMOUNT_USDT} USDT</p><p>Плечо: {LEVERAGE}x</p></div>"
        if last_trade_info:
            html += f"<div class='card'><h3>ПОСЛЕДНЯЯ СДЕЛКА</h3><pre>{json.dumps(last_trade_info, indent=2, default=str)}</pre></div>"
        if active_position:
            html += '<div class="card"><form action="/close" method="post"><button class="danger-btn">Закрыть позицию</button></form></div>'
        html += '</body></html>'
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<h1>Ошибка: {str(e)}</h1>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


