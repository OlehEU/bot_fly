@@ -1,259 +1,357 @@
import os
import json
import asyncio
import logging
from functools import wraps
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
import ccxt.async_support as ccxt

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))  # 25% от баланса
SYMBOL = os.getenv("SYMBOL", "XRP/USDT:USDT")  # XRP Futures
SYMBOL = os.getenv("SYMBOL", "XRP_USDT")  # ИСПРАВЛЕНО: формат для фьючерсов
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Client ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === Вспомогательные функции ===
async def get_current_price() -> float:
    """Получить текущую цену символа"""
    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        return float(ticker['last'])
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return 0.0

async def close_existing_positions():
    """Закрыть все существующие позиции"""
    try:
        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos['contracts'] and float(pos['contracts']) > 0:
                logger.info(f"Закрываем существующую позицию: {pos['side']} {pos['contracts']}")
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                await exchange.create_market_order(SYMBOL, close_side, abs(float(pos['contracts'])))
                await asyncio.sleep(1)  # Даём время на закрытие
                return True
        return False
    except Exception as e:
        logger.error(f"Ошибка при закрытии позиций: {e}")
        return False

# === RETRY при 403 ===
def retry_on_403(max_retries: int = 4, delay: int = 3):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if "403" in str(e) and attempt < max_retries - 1:
                        logger.warning(f"403 — повтор через {delay}s (попытка {attempt+2})")
                        await asyncio.sleep(delay)
                        continue
                    logger.error(f"API ошибка: {e}")
                    raise
            return 0.0
        return wrapper
    return decorator

# === Баланс ===
@retry_on_403()
async def check_balance() -> float:
    logger.info("Проверка баланса USDT...")
    try:
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        logger.info(f"Баланс USDT: {usdt:.4f}")
        return float(usdt)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"БАЛАНС = 0 USDT\nОшибка: {e}\n\nПроверь:\n1. API ключ\n2. IP в MEXC\n3. USDT на счёте"
            )
        except:
            pass
        return 0.0

# === РАСЧЁТ qty ===
async def calculate_qty(usd_amount: float) -> float:
    try:
        markets = await exchange.load_markets()
        market = markets[SYMBOL]
        min_qty = market['limits']['amount']['min']
        precision = market['precision']['amount']
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
        logger.info(f"Цена {SYMBOL}: {price:.2f} USDT")
        raw_qty = usd_amount / price
        logger.info(f"Сырой qty: {usd_amount} / {price:.2f} = {raw_qty:.6f}")
        qty = exchange.amount_to_precision(SYMBOL, raw_qty)
        qty = float(qty)
        if qty < min_qty:
            raise ValueError(f"qty {qty} < min {min_qty}")
        logger.info(f"Финальный qty: {qty} (min: {min_qty}, шаг: {precision})")
        return qty
    except Exception as e:
        logger.error(f"Ошибка qty: {e}")
        try:
            balance = await check_balance()
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"Ошибка qty: {e}\nБаланс: {balance:.2f} USDT"
            )
        except:
            pass
        return 0.0

# === Старт ===
@app.on_event("startup")
async def startup_notify():
    try:
        logger.info("=== ЗАПУСК БОТА ===")
        balance = await check_balance()
        logger.info(f"СТАРТОВЫЙ БАЛАНС: {balance:.4f} USDT")
        msg = f"MEXC Бот запущен!\n\n" \
        msg = f"✅ MEXC Бот запущен!\n\n" \
              f"Символ: {SYMBOL}\n" \
              f"Риск: {RISK_PERCENT}%\n" \
              f"Плечо: {LEVERAGE}x\n" \
              f"Баланс: {balance:.2f} USDT"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Стартовое уведомление отправлено.")
    except Exception as e:
        error_msg = f"ОШИБКА ПРИ СТАРТЕ: {e}"
        error_msg = f"❌ ОШИБКА ПРИ СТАРТЕ: {e}"
        logger.error(error_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        except:
            pass

# === Главная ===
@app.get("/", response_class=HTMLResponse)
async def home():
    global last_trade_info, active_position
    last_trade_text = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет"
    status = "Активна" if active_position else "Нет"
    return f"""
    <html><head><title>MEXC Bot</title><meta charset="utf-8"></head>
    <body style="font-family: Arial; background:#1e1e1e; color:#e0e0e0; padding:20px;">
      <h2 style="color:#00b894;">MEXC Futures Bot</h2>
      <ul>
        <li><b>Биржа:</b> MEXC</li>
        <li><b>Символ:</b> {SYMBOL}</li>
        <li><b>Лот:</b> {RISK_PERCENT}% от баланса</li>
        <li><b>Плечо:</b> {LEVERAGE}×</li>
        <li><b>Позиция:</b> {status}</li>
      </ul>
      <h3>Последняя сделка:</h3>
      <pre style="background:#2d2d2d; padding:10px;">{last_trade_text}</pre>
      <p><b>Webhook:</b> <code>POST /webhook</code><br>
      <b>Header:</b> <code>Authorization: Bearer {WEBHOOK_SECRET}</code></p>
      <a href="/balance">Проверить баланс</a>
    </body></html>
    """

# === Баланс ===
@app.get("/balance", response_class=HTMLResponse)
async def get_balance():
    balance = await check_balance()
    required = balance * (RISK_PERCENT / 100) * 1.1
    status = "Достаточно" if balance >= required else "Недостаточно"
    color = "#00b894" if balance >= required else "#e74c3c"
    return f"""
    <html><head><title>Баланс</title></head>
    <body style="font-family: Arial; background:#1e1e1e; color:#e0e0e0; padding:20px;">
      <h2>Баланс USDT</h2>
      <p><b>Доступно:</b> <span style="color:{color}">{balance:.2f}</span> USDT</p>
      <p><b>Требуется (25% + 10%):</b> {required:.2f} USDT</p>
      <p><b>Статус:</b> {status}</p>
      <a href="/">На главную</a>
    </body></html>
    """

# === Открытие позиции ===
async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position
    if active_position:
        logger.info("Позиция уже открыта — пропускаем.")
        return

    
    try:
        # Закрываем существующие позиции
        had_position = await close_existing_positions()
        if had_position:
            await asyncio.sleep(2)  # Даём больше времени на закрытие

        balance = await check_balance()
        if balance <= 0:
            raise ValueError("Баланс = 0 USDT")
            raise ValueError(f"Баланс = {balance:.2f} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        logger.info(f"Риск: {RISK_PERCENT}% → {usd:.2f} USDT из {balance:.2f}")

        if usd < 5:
            raise ValueError(f"Слишком маленький лот: {usd:.2f} USDT")
            raise ValueError(f"Слишком маленький лот: {usd:.2f} USDT (мин. 5 USDT)")

        # Устанавливаем плечо и режим маржи
        await exchange.set_leverage(LEVERAGE, SYMBOL)
        await exchange.set_margin_mode('isolated', SYMBOL)
        
        # Рассчитываем количество
        qty = await calculate_qty(usd)
        if qty <= 0:
            raise ValueError("Неверный qty")
            raise ValueError(f"Неверный qty: {qty}")

        side = "buy" if signal == "buy" else "sell"
        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"Открываем {side.upper()} {qty} {SYMBOL}")

        await exchange.set_leverage(LEVERAGE, SYMBOL)
        # Создаем рыночный ордер
        order = await exchange.create_market_order(SYMBOL, side, qty)
        logger.info(f"Ордер создан: {order}")

        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos['contracts'] > 0:
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                logger.info(f"Закрываем {close_side} {pos['contracts']} {SYMBOL}")
                await exchange.create_market_order(SYMBOL, close_side, pos['contracts'])
        # Получаем цену входа
        if order.get('filled', 0) > 0:
            entry = order.get('average') or await get_current_price()
        else:
            entry = await get_current_price()

        order = await exchange.create_market_order(SYMBOL, side, qty)
        entry = order['average'] or order['price']
        tp = round(entry * (1.015 if side == "buy" else 0.985), 6)
        sl = round(entry * (0.99 if side == "buy" else 1.01), 6)
        # Рассчитываем TP/SL
        if side == "buy":
            tp = entry * 1.015  # +1.5%
            sl = entry * 0.99   # -1%
        else:
            tp = entry * 0.985  # -1.5%
            sl = entry * 1.01   # +1%

        await exchange.create_order(SYMBOL, 'limit', 'sell' if side == "buy" else 'buy', qty, tp, {'reduceOnly': True})
        await exchange.create_order(SYMBOL, 'limit', 'sell' if side == "buy" else 'buy', qty, sl, {'reduceOnly': True})
        # Создаем TP/SL ордера (лимитные)
        try:
            tp_order = await exchange.create_order(
                SYMBOL, 'limit', 
                'sell' if side == "buy" else 'buy', 
                qty, tp, 
                {'reduceOnly': True}
            )
            logger.info(f"TP ордер создан: {tp}")
        except Exception as e:
            logger.warning(f"Не удалось создать TP: {e}")

        try:
            sl_order = await exchange.create_order(
                SYMBOL, 'limit', 
                'sell' if side == "buy" else 'buy', 
                qty, sl, 
                {'reduceOnly': True}
            )
            logger.info(f"SL ордер создан: {sl}")
        except Exception as e:
            logger.warning(f"Не удалось создать SL: {e}")

        active_position = True
        last_trade_info = {"signal": signal, "qty": qty, "entry": entry, "tp": tp, "sl": sl}
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "qty": qty, 
            "entry": round(entry, 6), 
            "tp": round(tp, 6), 
            "sl": round(sl, 6),
            "order_id": order.get('id', 'N/A'),
            "timestamp": asyncio.get_event_loop().time()
        }

        msg = f"{side.upper()} {qty} {SYMBOL}\nEntry: ${entry}\nTP: ${tp} | SL: ${sl}\nБаланс: {balance:.2f} USDT"
        msg = (f"✅ {side.upper()} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty}\n"
               f"Вход: ${entry:.4f}\n"
               f"TP: ${tp:.4f} | SL: ${sl:.4f}\n"
               f"Баланс: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(msg)

    except Exception as e:
        err_msg = f"Ошибка {signal}: {e}\nБаланс: {await check_balance():.2f} USDT"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        err_msg = f"❌ Ошибка открытия {signal}: {str(e)}\nБаланс: {await check_balance():.2f} USDT"
        logger.error(err_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        except:
            pass
        active_position = False

# === Webhook ===
@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
    except:
        return {"status": "error", "message": "Invalid JSON"}

    signal = data.get("signal")
    amount = data.get("amount")

    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal: buy или sell"}

    asyncio.create_task(open_position(signal, amount))
    return {"status": "ok", "message": f"{signal} принят"}

# === Закрытие позиции ===
@app.post("/close")
async def close_position(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")
    
    try:
        global active_position
        had_position = await close_existing_positions()
        
        if had_position:
            active_position = False
            msg = "✅ Позиция закрыта"
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
            return {"status": "ok", "message": "Позиция закрыта"}
        else:
            return {"status": "error", "message": "Нет открытых позиций"}
            
    except Exception as e:
        error_msg = f"❌ Ошибка закрытия позиции: {e}"
        logger.error(error_msg)
        return {"status": "error", "message": str(e)}

# === ЗАПУСК ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
