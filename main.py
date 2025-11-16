# main.py
import os
import json
import time
import traceback
import logging
import asyncio
from typing import Optional, Dict
import math
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# -------------------------
# Config / Secrets
# -------------------------
REQUIRED_SECRETS = [
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MEXC_API_KEY",
    "MEXC_API_SECRET",
    "WEBHOOK_SECRET",
]
missing = [s for s in REQUIRED_SECRETS if not os.getenv(s)]
if missing:
    raise EnvironmentError(f"ОШИБКА: не заданы секреты: {', '.join(missing)}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))   # $10
LEVERAGE = int(os.getenv("LEVERAGE", "10"))                     # 10x (фикс!)
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.5"))               # +0.5%
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "2.2616"))

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mexc-bot")

# -------------------------
# Telegram helper
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)
async def tg_send(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        logger.info("INFO: Сообщение отправлено в Telegram")
    except Exception as e:
        logger.error(f"ERROR: Не удалось отправить в Telegram: {e}\n{traceback.format_exc()}")

# -------------------------
# MEXC (ccxt async)
# -------------------------
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},  # perpetual futures
    "timeout": 30000
})

# -------------------------
# Symbol auto-detection
# -------------------------
_cached_markets: Optional[Dict[str, str]] = None
async def resolve_symbol(base: str) -> str:
    global _cached_markets
    if _cached_markets is None:
        await exchange.load_markets()
        _cached_markets = {m.split("/")[0]: m for m in exchange.markets.keys() if m.endswith(":USDT")}
    symbol = _cached_markets.get(base.upper())
    if not symbol:
        raise Exception(f"Symbol {base} not found in swap markets")
    return symbol

# -------------------------
# Utilities
# -------------------------
async def safe_ccxt_call(fn, *args, retries=3, **kwargs):
    for attempt in range(retries):
        try:
            result = await fn(*args, **kwargs)
            logger.info(f"CCXT success: {fn.__name__}")
            return result
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            logger.warning(f"Network error #{attempt+1}: {e}")
            await asyncio.sleep(1)
        except ccxt.BaseError as e:
            logger.error(f"CCXT error: {e}\n{traceback.format_exc()}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {e}\n{traceback.format_exc()}")
            return None
    return None

# -------------------------
# Balance / Price / Market info
# -------------------------
async def fetch_balance_usdt() -> float:
    bal = await safe_ccxt_call(exchange.fetch_balance)
    if not bal:
        return 0.0
    usdt = float(bal.get("total", {}).get("USDT", 0) or 0)
    logger.info(f"Баланс USDT: {usdt}")
    return usdt

async def fetch_price(symbol: str) -> float:
    ticker = await safe_ccxt_call(exchange.fetch_ticker, symbol)
    if not ticker:
        return 0.0
    price = float(ticker.get("last") or ticker.get("close") or 0)
    logger.info(f"Цена {symbol}: {price}")
    return price

async def get_market_info(symbol: str) -> dict:
    await exchange.load_markets()
    market = exchange.markets.get(symbol)
    if not market:
        raise Exception(f"Market {symbol} not found")
    info = market.get('info', {})
    vol_unit = float(info.get('volUnit', 1))
    min_vol = float(info.get('minVol', 1))
    price_scale = int(info.get('priceScale', 2))
    return {"vol_unit": vol_unit, "min_vol": min_vol, "price_scale": price_scale}

async def calculate_qty_for_usd(symbol: str, usd_amount: float, leverage: int) -> float:
    price = await fetch_price(symbol)
    if price <= 0:
        raise Exception("Не удалось получить цену")
    market_info = await get_market_info(symbol)
    vol_unit = market_info['vol_unit']
    min_vol = market_info['min_vol']
    qty = (usd_amount * leverage) / price
    if qty * price < MIN_ORDER_USD:
        qty = MIN_ORDER_USD / price
    # Используем ceil для точности (чтобы не меньше требуемого)
    qty = math.ceil(qty / vol_unit) * vol_unit
    if qty < min_vol:
        qty = min_vol
    logger.info(f"Qty: {qty} (USD {usd_amount} × {leverage}x / {price})")
    return qty

# -------------------------
# Leverage & Position
# -------------------------
async def set_leverage_usdt(symbol: str, leverage: int, positionSide: str):
    try:
        params = {"positionSide": positionSide, "openType": 1, "positionType": 1 if positionSide == "LONG" else 2}
        await safe_ccxt_call(exchange.set_leverage, leverage, symbol, params)
        logger.info(f"Плечо {leverage}x установлено для {positionSide}")
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"Не удалось установить плечо: {e}")

async def check_active_position(symbol: str) -> bool:
    """Проверяем реальную позицию по API"""
    try:
        positions = await safe_ccxt_call(exchange.fetch_positions, [symbol])
        if positions:
            for pos in positions:
                if abs(float(pos.get('contracts', 0))) > 0:
                    logger.info(f"Активная позиция найдена: {pos.get('contracts')} {symbol}")
                    return True
        return False
    except Exception as e:
        logger.warning(f"Ошибка проверки позиции: {e}")
        return False

# -------------------------
# Order creation
# -------------------------
async def create_market_position_usdt(symbol: str, side: str, qty: float, leverage: int):
    positionSide = "LONG" if side == "buy" else "SHORT"
    await set_leverage_usdt(symbol, leverage, positionSide)
    params = {
        "openType": 1,  # isolated
        "positionSide": positionSide,
        "positionType": 1 if positionSide == "LONG" else 2,
        "leverage": leverage  # ← КРИТИЧНО: Добавляем для MEXC isolated!
    }
    logger.info(f"Открываю рыночный {side} {qty} {symbol} с leverage {leverage}x")
    order = await safe_ccxt_call(
        exchange.create_order,
        symbol,
        "market",
        side,
        qty,
        None,
        params
    )
    if not order:
        raise Exception("Рыночный ордер не создан")
    logger.info(f"Ордер выполнен: ID {order.get('id')} @ {order.get('average')}")
    return order

async def create_tp_limit(symbol: str, close_side: str, qty: float, price: float, positionSide: str, leverage: int):
    params = {
        "reduceOnly": True,
        "positionSide": positionSide,
        "openType": 1,
        "positionType": 1 if positionSide == "LONG" else 2,
        "leverage": leverage  # ← Добавляем для MEXC
    }
    logger.info(f"Устанавливаю TP: {close_side} {qty} @ {price} с leverage {leverage}x")
    order = await safe_ccxt_call(
        exchange.create_order,
        symbol,
        "limit",
        close_side,
        qty,
        price,
        params
    )
    if not order:
        await tg_send(f"Warning: TP ордер не установлен: {close_side} {qty} @ {price}")
    return order

# -------------------------
# High-level logic
# -------------------------
active_position = False
last_trade_info: Optional[dict] = None

async def open_position_from_signal(signal: str, symbol_base: str = "XRP", fixed_amount_usd: Optional[float] = None):
    global active_position, last_trade_info
    try:
        # Проверяем реальную позицию
        SYMBOL = await resolve_symbol(symbol_base)
        active_position = await check_active_position(SYMBOL)
        if active_position:
            await tg_send("Warning: Позиция уже активна — сигнал проигнорирован.")
            return

        if signal.lower() != "buy":
            await tg_send("Warning: Поддерживается только BUY сигнал.")
            return

        usd_amount = fixed_amount_usd if fixed_amount_usd and fixed_amount_usd > 0 else FIXED_AMOUNT_USD
        if usd_amount < MIN_ORDER_USD:
            await tg_send(f"Error: Сумма {usd_amount:.2f} USDT < min {MIN_ORDER_USD}")
            return

        qty = await calculate_qty_for_usd(SYMBOL, usd_amount, LEVERAGE)
        side = "buy"
        positionSide = "LONG"
        close_side = "sell"

        # Открываем позицию
        order = await create_market_position_usdt(SYMBOL, side, qty, LEVERAGE)
        entry_price = order.get("average") or order.get("price") or await fetch_price(SYMBOL)

        # TP +0.5%
        market_info = await get_market_info(SYMBOL)
        tp_price = round(entry_price * (1 + TP_PERCENT / 100), market_info['price_scale'])

        # Устанавливаем TP
        await create_tp_limit(SYMBOL, close_side, qty, tp_price, positionSide, LEVERAGE)

        # Проверяем позицию после входа
        active_position = await check_active_position(SYMBOL)
        if not active_position:
            raise Exception("Позиция не открыта после ордера!")

        # Сохраняем состояние
        last_trade_info = {
            "symbol": SYMBOL,
            "side": side,
            "qty": qty,
            "entry": entry_price,
            "tp": tp_price,
            "timestamp": time.time()
        }

        msg = (
            f"✅ <b>LONG ОТКРЫТ (MEXC Futures)</b>\n"
            f"Символ: <code>{SYMBOL}</code>\n"
            f"Сумма: <code>${usd_amount}</code>\n"
            f"Плечо: <code>{LEVERAGE}x</code>\n"
            f"Qty: <code>{qty}</code>\n"
            f"Entry: <code>{entry_price:.4f}</code>\n"
            f"TP (+{TP_PERCENT}%): <code>{tp_price:.4f}</code>\n"
            f"SL: <i>не установлен</i>\n"
        )
        await tg_send(msg)
        logger.info("Позиция XRP LONG открыта с TP 0.5%")

    except Exception as e:
        logger.error(f"Ошибка: {e}\n{traceback.format_exc()}")
        await tg_send(f"❌ Error: Ошибка открытия: {str(e)}")
        active_position = False

# -------------------------
# FastAPI lifespan
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ЗАПУСК БОТА")
    try:
        balance = await fetch_balance_usdt()
        SYMBOL = await resolve_symbol("XRP")
        price = await fetch_price(SYMBOL)
        active_position = await check_active_position(SYMBOL)
    except Exception:
        balance = price = SYMBOL = "N/A"
        active_position = False

    start_msg = (
        f"🤖 Bot started\n"
        f"Символ: {SYMBOL}\n"
        f"Баланс: {balance} USDT\n"
        f"Цена: {price}\n"
        f"Сумма: ${FIXED_AMOUNT_USD}\n"
        f"Плечо: {LEVERAGE}x\n"
        f"TP: +{TP_PERCENT}%\n"
        f"Позиция: {'АКТИВНА' if active_position else 'НЕТ'}\n"
        f"Webhook: /webhook"
    )
    await tg_send(start_msg)
    yield
    logger.info("ОСТАНОВКА БОТА")
    await exchange.close()
    await tg_send("🔴 Bot stopped")

app = FastAPI(lifespan=lifespan)

# -------------------------
# Routes
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    try:
        SYMBOL = await resolve_symbol("XRP")
        balance = await fetch_balance_usdt()
        price = await fetch_price(SYMBOL)
        pos_status = await check_active_position(SYMBOL)
    except Exception:
        SYMBOL = balance = price = "N/A"
        pos_status = False
    status = "АКТИВНА" if pos_status else "НЕТ"
    return HTMLResponse(f"""
    <html><body>
    <h1>🤖 MEXC XRP Bot</h1>
    <p>Символ: {SYMBOL}</p>
    <p>Баланс: {balance} USDT</p>
    <p>Цена: {price}</p>
    <p>Позиция: {status}</p>
    </body></html>
    """)

@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Webhook-Secret") or request.headers.get("Authorization", "")
    if secret.startswith("Bearer "):
        secret = secret.split(" ", 1)[1]
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    signal = payload.get("signal", "").lower()
    symbol = payload.get("symbol", "XRP")
    amount = payload.get("fixed_amount_usd")

    if signal != "buy":
        raise HTTPException(400, "Только 'buy' сигнал")

    asyncio.create_task(open_position_from_signal("buy", symbol, amount))
    await tg_send(f"📨 Получен сигнал: BUY {symbol}")
    return {"status": "ok"}

@app.get("/health")
async def health():
    try:
        SYMBOL = await resolve_symbol("XRP")
        price = await fetch_price(SYMBOL)
        balance = await fetch_balance_usdt()
        pos_status = await check_active_position(SYMBOL)
        return {
            "status": "ok",
            "symbol": SYMBOL,
            "price": price,
            "balance": balance,
            "position": pos_status
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
