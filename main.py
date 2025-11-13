import os
import json
import asyncio
import logging
from functools import wraps
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

# NOTE: используем синхронный Bot в отдельном потоке
from telegram import Bot

import ccxt.async_support as ccxt

# -------------------------
# Проверка обязательных секретов (выкинет ошибку при старте если чего-то нет)
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
    raise EnvironmentError(f"ОШИБКА: не заданы секреты: {', '.join(missing)}. Установи: fly secrets set NAME=value")

# -------------------------
# Конфигурация
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Настройки торгов
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))     # % от баланса на одну сделку
SYMBOL = os.getenv("SYMBOL", "XRP/USDT")                # формат: "BASE/QUOTE" — без :USDT
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_USD = float(os.getenv("MIN_USD", 5.0))              # минимальный размер в USD, если меньше — отмена

# -------------------------
# Логирование и Telegram
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    """Отправка сообщения в Telegram в отдельном потоке (Bot.sync)"""
    try:
        await asyncio.to_thread(bot.send_message, TELEGRAM_CHAT_ID, text)
    except Exception as e:
        logger.warning(f"Не удалось отправить Telegram уведомление: {e}")

# -------------------------
# CCXT MEXC async клиент (futures / swap)
# -------------------------
exchange = ccxt.mexc({
    "apiKey": MEXC_API_KEY,
    "secret": MEXC_API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",   # используем swap/futures
    },
})

# -------------------------
# Утилиты: retry decorator
# -------------------------
def retry_on_error(max_retries: int = 4, delay: int = 2):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    text = str(e)
                    # повторяем при rate limit / временных сетевых ошибках
                    if attempt < max_retries and any(code in text for code in ["429", "rate limit", "timeout", "ETIMEDOUT", "ECONNRESET"]):
                        logger.warning(f"Retry {attempt}/{max_retries} after error: {e} — wait {delay}s")
                        await asyncio.sleep(delay)
                        continue
                    logger.exception(f"Финальная ошибка в {func.__name__}: {e}")
                    raise
            return None
        return wrapper
    return decorator

# -------------------------
# Баланс
# -------------------------
@retry_on_error()
async def check_balance() -> float:
    """Возвращает доступный баланс в USDT (float)."""
    bal = await exchange.fetch_balance()
    # в некоторых реализациях USDT может быть в 'total' или 'free' — используем 'total'
    usdt = 0.0
    if isinstance(bal, dict):
        # ccxt: bal['total'] или bal['free']
        total = bal.get("total") or bal.get("free") or {}
        usdt = total.get("USDT", 0) or total.get("usdt", 0) or 0.0
    try:
        usdt = float(usdt)
    except Exception:
        usdt = 0.0
    logger.info(f"Баланс USDT: {usdt:.4f}")
    return usdt

# -------------------------
# Вычисление qty
# -------------------------
@retry_on_error()
async def calculate_qty(usd_amount: float) -> float:
    """Вычисляет кол-во контрактов/лотов по USD и возвращает строку/float с precision."""
    await exchange.load_markets()
    market = exchange.markets.get(SYMBOL)
    if not market:
        raise ValueError(f"Маркет {SYMBOL} не найден в MEXC (load_markets нужен).")
    ticker = await exchange.fetch_ticker(SYMBOL)
    price = ticker.get("last") or ticker.get("close")
    if not price:
        raise ValueError("Не удалось получить цену для расчёта qty.")
    raw_qty = usd_amount / float(price)
    # приводим к точности биржи
    try:
        qty_str = exchange.amount_to_precision(SYMBOL, raw_qty)
        qty = float(qty_str)
    except Exception:
        # fallback: округление по precision (если доступно)
        precision = market.get("precision", {}).get("amount")
        if precision is not None:
            qty = round(raw_qty, precision)
        else:
            qty = round(raw_qty, 6)
    if qty <= 0:
        raise ValueError("Расчитанный qty <= 0")
    logger.info(f"calculate_qty: usd={usd_amount} price={price} qty={qty}")
    return qty

# -------------------------
# Основная логика открытия позиции
# -------------------------
last_trade_info: Optional[dict] = None
active_position = False

@retry_on_error()
async def open_position(signal: str, amount_usd: Optional[float] = None):
    """
    signal: 'buy' или 'sell'
    amount_usd: сумма в USD для позиции (если None — берём RISK_PERCENT от баланса)
    """
    global last_trade_info, active_position

    if active_position:
        logger.info("Позиция уже активна — пропуск открытия.")
        return

    try:
        # 1) баланс и размер позиции
        balance = await check_balance()
        usd = amount_usd if amount_usd is not None else (balance * RISK_PERCENT / 100.0)
        if usd < MIN_USD:
            raise ValueError(f"Недостаточно средств для позиции (нужно >= {MIN_USD} USD), рассчитано: {usd:.2f}")

        # 2) вычисляем qty с учётом точности
        qty = await calculate_qty(usd)

        # 3) установка плеча (в ccxt: set_leverage(leverage, symbol))
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL)
        except Exception as e:
            logger.warning(f"Не удалось явно установить плечо через set_leverage: {e} — возможно биржа установит плечо автоматически.")

        # 4) закрыть существующие позиции по этому инструменту (если нужны)
        try:
            positions = await exchange.fetch_positions([SYMBOL])
        except Exception:
            # некоторые версии ccxt возвращают через fetch_positions() без аргументов
            positions = await exchange.fetch_positions()

        # Закрываем только противоположные позиции (и только если contracts > 0)
        for pos in positions:
            # структура pos может отличаться, пробуем извлечь информативно
            contracts = pos.get("contracts") or pos.get("contractAmount") or 0
            side = pos.get("side") or pos.get("positionSide") or pos.get("symbol")  # best-effort
            if not contracts:
                continue
            # pos['side'] часто 'long'/'short'. Закрываем по противоположной стороне рыночным ордером:
            if pos.get("side") in ("long", "LONG", "Long"):
                close_side = "sell"
                close_position_side = "SHORT"
            elif pos.get("side") in ("short", "SHORT", "Short"):
                close_side = "buy"
                close_position_side = "LONG"
            else:
                # если неясно — пропускаем
                continue

            logger.info(f"Закрываем существующую позицию: contracts={contracts} side={pos.get('side')}")
            try:
                await exchange.create_order(
                    SYMBOL,
                    type="market",
                    side=close_side,
                    amount=contracts,
                    params={
                        "positionSide": close_position_side,
                        "reduceOnly": True,
                    },
                )
            except Exception as e:
                logger.warning(f"Не удалось закрыть старую позицию: {e}")

        # 5) открываем новую позицию
        side = "buy" if signal == "buy" else "sell"
        positionSide = "LONG" if signal == "buy" else "SHORT"

        logger.info(f"Открываем позицию: {signal} qty={qty} SYMBOL={SYMBOL} positionSide={positionSide}")

        order = await exchange.create_order(
            SYMBOL,
            type="market",
            side=side,
            amount=qty,
            params={
                "positionSide": positionSide,
                "reduceOnly": False,
            },
        )

        # получаем цену входа
        entry = order.get("average") or order.get("price") or (await exchange.fetch_ticker(SYMBOL)).get("last")
        if entry is None:
            raise ValueError("Не удалось получить цену входа из ответа биржи.")

        # выставляем TP / SL (примерно)
        tp = float(entry) * (1.015 if side == "buy" else 0.985)
        sl = float(entry) * (0.99 if side == "buy" else 1.01)

        # округлим до price precision (если есть)
        market = exchange.markets.get(SYMBOL)
        price_prec = None
        if market:
            price_prec = market.get("precision", {}).get("price")
        if price_prec is not None:
            tp = round(tp, price_prec)
            sl = round(sl, price_prec)
        else:
            tp = round(tp, 6)
            sl = round(sl, 6)

        # TP (limit reduceOnly)
        try:
            await exchange.create_order(
                SYMBOL,
                type="limit",
                side="sell" if side == "buy" else "buy",
                amount=qty,
                price=tp,
                params={"positionSide": positionSide, "reduceOnly": True},
            )
        except Exception as e:
            logger.warning(f"Не удалось выставить TP: {e}")

        # SL (limit reduceOnly)
        try:
            await exchange.create_order(
                SYMBOL,
                type="limit",
                side="sell" if side == "buy" else "buy",
                amount=qty,
                price=sl,
                params={"positionSide": positionSide, "reduceOnly": True},
            )
        except Exception as e:
            logger.warning(f"Не удалось выставить SL: {e}")

        # Обновляем состояние
        last_trade_info = {
            "signal": signal,
            "qty": qty,
            "entry": float(entry),
            "tp": tp,
            "sl": sl,
        }
        active_position = True

        await tg_send(f"✅ {signal.upper()} executed\n{qty} {SYMBOL}\nEntry: {entry}\nTP: {tp}\nSL: {sl}\nБаланс: {balance:.2f} USDT")

    except Exception as e:
        logger.exception("Ошибка при open_position")
        active_position = False
        await tg_send(f"❌ Ошибка {signal}: {e}\nБаланс: {await check_balance():.2f} USDT")

# -------------------------
# FastAPI приложения и endpoints
# -------------------------
app = FastAPI()

@app.on_event("startup")
async def startup_notify():
    try:
        await exchange.load_markets()
    except Exception as e:
        logger.warning(f"Не удалось load_markets при старте: {e}")
    try:
        balance = await check_balance()
        await tg_send(f"MEXC Bot запущен.\nСимвол: {SYMBOL}\nРиск: {RISK_PERCENT}%\nПлечо: {LEVERAGE}x\nБаланс: {balance:.2f} USDT")
    except Exception as e:
        logger.warning(f"Ошибка стартового уведомления: {e}")

@app.on_event("shutdown")
async def shutdown():
    try:
        await exchange.close()
    except Exception as e:
        logger.warning(f"Ошибка при закрытии exchange: {e}")

@app.get("/", response_class=HTMLResponse)
async def home():
    last = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет"
    status = "Активна" if active_position else "Нет"
    return f"""<html><head><meta charset="utf-8"><title>MEXC Bot</title></head>
    <body style="font-family:Arial;background:#1e1e1e;color:#e0e0e0;padding:20px;">
      <h2 style="color:#00b894;">MEXC Futures Bot</h2>
      <ul>
        <li><b>Символ:</b> {SYMBOL}</li>
        <li><b>Риск:</b> {RISK_PERCENT}%</li>
        <li><b>Плечо:</b> {LEVERAGE}×</li>
        <li><b>Позиция:</b> {status}</li>
      </ul>
      <h3>Последняя сделка:</h3>
      <pre style="background:#2d2d2d;padding:10px;">{last}</pre>
      <p><b>Webhook:</b> POST /webhook <br>Header: Authorization: Bearer {WEBHOOK_SECRET}</p>
      <a href="/balance">Баланс</a>
    </body></html>"""

@app.get("/balance", response_class=HTMLResponse)
async def get_balance():
    bal = await check_balance()
    required = bal * RISK_PERCENT / 100.0
    return f"<html><body style='font-family:Arial;background:#1e1e1e;color:#e0e0e0;padding:20px;'><h2>Баланс: {bal:.2f} USDT</h2><p>Риск-сумма: {required:.2f} USDT</p><a href='/'>Назад</a></body></html>"

@app.post("/webhook")
async def webhook(request: Request):
    # авторизация webhook
    auth = request.headers.get("Authorization", "")
    if WEBHOOK_SECRET and auth != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    signal = data.get("signal")
    amount = data.get("amount")  # optional USD amount
    if signal not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="signal must be 'buy' or 'sell'")

    # запуск в фоне
    asyncio.create_task(open_position(signal, amount))
    return {"status": "ok", "message": f"{signal} accepted"}

# -------------------------
# Для локального запуска
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
