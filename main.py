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
SYMBOL = os.getenv("SYMBOL", "XRPUSDT")  # ИСПРАВЛЕНО: правильный формат для MEXC
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
async def get_current_price(symbol: str = SYMBOL) -> float:
    """Получить текущую цену символа"""
    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = float(ticker['last'])
        logger.info(f"Текущая цена {symbol}: {price:.6f}")
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены для {symbol}: {e}")
        return 0.0

async def close_existing_positions(symbol: str = SYMBOL):
    """Закрыть все существующие позиции"""
    try:
        positions = await exchange.fetch_positions([symbol])
        for pos in positions:
            if pos['contracts'] and float(pos['contracts']) > 0:
                logger.info(f"Закрываем существующую позицию: {pos['side']} {pos['contracts']}")
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                await exchange.create_market_order(symbol, close_side, abs(float(pos['contracts'])))
                await asyncio.sleep(1)  # Даём время на закрытие
                return True
        return False
    except Exception as e:
        logger.error(f"Ошибка при закрытии позиций: {e}")
        return False

async def set_leverage_for_symbol(symbol: str, side: str):
    """Установка плеча с правильными параметрами для MEXC"""
    try:
        position_type = 1 if side == 'buy' else 2  # 1 for long, 2 for short
        
        params = {
            'openType': 1,  # 1 = isolated margin
            'positionType': position_type
        }
        
        await exchange.set_leverage(LEVERAGE, symbol, params)
        logger.info(f"Плечо {LEVERAGE}x установлено для {side} (positionType: {position_type})")
        return True
    except Exception as e:
        logger.error(f"Ошибка установки плеча: {e}")
        return False

async def find_correct_symbol(base_currency: str = "XRP", quote_currency: str = "USDT"):
    """Найти правильный символ для фьючерсов MEXC"""
    try:
        await exchange.load_markets()
        
        # Возможные варианты символов для MEXC фьючерсов
        possible_symbols = [
            f"{base_currency}{quote_currency}",  # XRPUSDT
            f"{base_currency}/{quote_currency}", # XRP/USDT
            f"{base_currency}_{quote_currency}", # XRP_USDT
            f"{base_currency}{quote_currency}-SWAP", # XRPUSDT-SWAP
        ]
        
        for symbol in possible_symbols:
            if symbol in exchange.markets:
                market = exchange.markets[symbol]
                if market['swap'] or market['future']:  # Это фьючерсный контракт
                    logger.info(f"Найден фьючерсный символ: {symbol}")
                    return symbol
        
        # Если не нашли, покажем доступные символы
        available_futures = []
        for symbol, market in exchange.markets.items():
            if (market['swap'] or market['future']) and base_currency in symbol and quote_currency in symbol:
                available_futures.append(symbol)
        
        logger.info(f"Доступные фьючерсы с {base_currency}: {available_futures}")
        
        if available_futures:
            return available_futures[0]
        else:
            raise ValueError(f"Не найдены фьючерсы для {base_currency}/{quote_currency}")
            
    except Exception as e:
        logger.error(f"Ошибка поиска символа: {e}")
        return None

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

# === РАСЧЁТ qty === ИСПРАВЛЕННАЯ ВЕРСИЯ
async def calculate_qty(usd_amount: float, symbol: str = SYMBOL) -> float:
    try:
        logger.info(f"Расчет qty для суммы: {usd_amount} USDT, символ: {symbol}")
        
        # Загружаем рынки
        await exchange.load_markets()
        
        # Проверяем существование символа
        if symbol not in exchange.markets:
            logger.error(f"Символ {symbol} не найден в markets")
            available_symbols = [s for s in exchange.markets.keys() if 'XRP' in s and 'USDT' in s][:10]
            logger.info(f"Доступные символы с XRP: {available_symbols}")
            raise ValueError(f"Символ {symbol} не найден. Доступные: {available_symbols}")
        
        # Получаем информацию о символе
        market = exchange.markets[symbol]
        logger.info(f"Тип рынка: {market['type']}, активный: {market['active']}")
        
        # Получаем текущую цену
        price = await get_current_price(symbol)
        if price <= 0:
            raise ValueError(f"Не удалось получить цену для {symbol}")
        
        # Рассчитываем сырое количество
        raw_qty = usd_amount / price
        logger.info(f"Сырое количество: {usd_amount} / {price:.6f} = {raw_qty:.8f}")
        
        # Применяем precision
        qty = exchange.amount_to_precision(symbol, raw_qty)
        qty = float(qty)
        logger.info(f"Количество после precision: {qty}")
        
        # Проверяем минимальное количество
        min_qty = market['limits']['amount']['min']
        logger.info(f"Минимальное количество: {min_qty}")
        
        if qty < min_qty:
            logger.warning(f"Рассчитанное количество {qty} меньше минимального {min_qty}")
            # Используем минимальное разрешенное количество
            qty = min_qty
            required_usd = qty * price
            logger.info(f"Используем минимальное количество: {qty}, требуется: {required_usd:.2f} USDT")
            
            if required_usd > usd_amount:
                logger.warning(f"Для минимального количества требуется {required_usd:.2f} USDT, но доступно {usd_amount:.2f}")
                # Увеличиваем сумму до минимальной required
                usd_amount = required_usd * 1.1  # +10% для надежности
        
        logger.info(f"Финальное количество: {qty}")
        return qty
        
    except Exception as e:
        logger.error(f"Ошибка расчета qty: {e}")
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"❌ Ошибка расчета количества: {str(e)}"
        )
        return 0.0

# === Старт ===
@app.on_event("startup")
async def startup_notify():
    try:
        logger.info("=== ЗАПУСК БОТА ===")
        
        # Находим правильный символ
        global SYMBOL
        correct_symbol = await find_correct_symbol()
        if correct_symbol:
            SYMBOL = correct_symbol
            logger.info(f"Используем символ: {SYMBOL}")
        
        balance = await check_balance()
        logger.info(f"СТАРТОВЫЙ БАЛАНС: {balance:.4f} USDT")
        
        # Тестируем расчет количества
        test_qty = await calculate_qty(10, SYMBOL)  # Тест с 10 USDT
        logger.info(f"Тестовый расчет: 10 USDT = {test_qty} {SYMBOL}")
        
        msg = f"✅ MEXC Бот запущен!\n\n" \
              f"Символ: {SYMBOL}\n" \
              f"Риск: {RISK_PERCENT}%\n" \
              f"Плечо: {LEVERAGE}x\n" \
              f"Баланс: {balance:.2f} USDT\n" \
              f"Тест расчета: {test_qty} {SYMBOL} за 10 USDT"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Стартовое уведомление отправлено.")
    except Exception as e:
        error_msg = f"❌ ОШИБКА ПРИ СТАРТЕ: {e}"
        logger.error(error_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        except:
            pass

# === Главная ===
@app.get("/", response_class=HTMLResponse)
async def home():
    global last_trade_info, active_position, SYMBOL
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
    required = balance * (RISK_PERCENT / 100)
    status = "Достаточно" if balance >= required else "Недостаточно"
    color = "#00b894" if balance >= required else "#e74c3c"
    return f"""
    <html><head><title>Баланс</title></head>
    <body style="font-family: Arial; background:#1e1e1e; color:#e0e0e0; padding:20px;">
      <h2>Баланс USDT</h2>
      <p><b>Доступно:</b> <span style="color:{color}">{balance:.2f}</span> USDT</p>
      <p><b>Требуется ({RISK_PERCENT}%):</b> {required:.2f} USDT</p>
      <p><b>Статус:</b> {status}</p>
      <a href="/">На главную</a>
    </body></html>
    """

# === Открытие позиции ===
async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position, SYMBOL
    
    try:
        logger.info(f"=== ПОПЫТКА ОТКРЫТИЯ ПОЗИЦИИ {signal.upper()} ===")
        
        # Находим правильный символ если нужно
        if SYMBOL not in exchange.markets:
            correct_symbol = await find_correct_symbol()
            if correct_symbol:
                SYMBOL = correct_symbol
                logger.info(f"Автоматически установлен символ: {SYMBOL}")
            else:
                raise ValueError("Не удалось найти подходящий символ для торговли")
        
        # Закрываем существующие позиции
        had_position = await close_existing_positions(SYMBOL)
        if had_position:
            await asyncio.sleep(2)

        balance = await check_balance()
        logger.info(f"Текущий баланс: {balance:.2f} USDT")
        
        if balance <= 0:
            raise ValueError(f"Баланс = {balance:.2f} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        logger.info(f"Сумма для позиции: {usd:.2f} USDT ({RISK_PERCENT}% от баланса)")

        # Рассчитываем количество
        qty = await calculate_qty(usd, SYMBOL)
        logger.info(f"Рассчитанное количество: {qty}")
        
        if qty <= 0:
            raise ValueError(f"Неверный qty: {qty}")

        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"Открываем {side.upper()} позицию: {qty} {SYMBOL}")

        # Устанавливаем плечо
        await set_leverage_for_symbol(SYMBOL, side)

        # Создаем рыночный ордер
        logger.info(f"Создаем рыночный ордер: {side} {qty} {SYMBOL}")
        order = await exchange.create_market_order(SYMBOL, side, qty)
        logger.info(f"Ордер создан: {order}")

        # Получаем цену входа
        entry = await get_current_price(SYMBOL)
        if order.get('filled', 0) > 0 and order.get('average'):
            entry = order['average']

        # Рассчитываем TP/SL
        if side == "buy":
            tp = entry * 1.015  # +1.5%
            sl = entry * 0.99   # -1%
        else:
            tp = entry * 0.985  # -1.5%
            sl = entry * 1.01   # +1%

        logger.info(f"TP: {tp:.6f}, SL: {sl:.6f}")

        # Создаем TP/SL ордера
        try:
            await exchange.create_order(
                SYMBOL, 'limit', 
                'sell' if side == "buy" else 'buy', 
                qty, tp, 
                {'reduceOnly': True}
            )
            logger.info("TP ордер создан")
        except Exception as e:
            logger.warning(f"Не удалось создать TP: {e}")

        try:
            await exchange.create_order(
                SYMBOL, 'limit', 
                'sell' if side == "buy" else 'buy', 
                qty, sl, 
                {'reduceOnly': True}
            )
            logger.info("SL ордер создан")
        except Exception as e:
            logger.warning(f"Не удалось создать SL: {e}")

        active_position = True
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "qty": qty, 
            "entry": round(entry, 6), 
            "tp": round(tp, 6), 
            "sl": round(sl, 6),
            "symbol": SYMBOL,
            "order_id": order.get('id', 'N/A')
        }

        msg = (f"✅ {side.upper()} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty}\n"
               f"Вход: ${entry:.6f}\n"
               f"TP: ${tp:.6f} | SL: ${sl:.6f}\n"
               f"Баланс: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(f"Позиция успешно открыта: {msg}")

    except Exception as e:
        err_msg = f"❌ Ошибка открытия {signal}: {str(e)}"
        logger.error(err_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
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
