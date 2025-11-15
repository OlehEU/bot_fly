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

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# === НАСТРОЙКИ ТАЙМАУТОВ И ПОВТОРОВ ===
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 2

# === Проверка секретов ===
REQUIRED_SECRETS = [
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", 
    "MEXC_API_SECRET", "WEBHOOK_SECRET", "SYMBOL", 
    "FIXED_AMOUNT_USDT", "LEVERAGE"
]

for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан в секретах!")

# === НАСТРОЙКИ ИЗ СЕКРЕТОВ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
SYMBOL = os.getenv("SYMBOL")
FIXED_AMOUNT_USDT = float(os.getenv("FIXED_AMOUNT_USDT"))
LEVERAGE = int(os.getenv("LEVERAGE"))

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")
logger.info(f"📊 Настройки: Символ={SYMBOL}, Сумма={FIXED_AMOUNT_USDT}, Плечо={LEVERAGE}")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    },
    'timeout': REQUEST_TIMEOUT * 1000,
    'sandbox': False,
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === КОНСТАНТЫ MEXC API ===
# Side types
SIDE_BUY = 1      # Open Long
SIDE_SELL = 2     # Open Short  
SIDE_CLOSE_LONG = 3  # Close Long
SIDE_CLOSE_SHORT = 4 # Close Short

# Order types
ORDER_MARKET = 1
ORDER_LIMIT = 2

# Margin types
MARGIN_ISOLATED = 1
MARGIN_CROSS = 2

# === Вспомогательные функции ===
@asynccontextmanager
async def error_handler(operation: str):
    try:
        yield
    except Exception as e:
        error_msg = f"❌ Ошибка в {operation}: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg[:4000])
        except:
            pass
        raise

async def get_current_price() -> float:
    async with error_handler("get_current_price"):
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.info(f"💰 Текущая цена {SYMBOL}: {price:.6f}")
        return price

async def check_balance() -> float:
    async with error_handler("check_balance"):
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        logger.info(f"💳 Баланс USDT: {usdt:.4f}")
        return float(usdt)

async def check_balance_detailed():
    async with error_handler("check_balance_detailed"):
        balance_data = await exchange.fetch_balance()
        
        total_usdt = balance_data['total'].get('USDT', 0)
        free_usdt = balance_data['free'].get('USDT', 0)
        used_usdt = balance_data['used'].get('USDT', 0)
        
        logger.info(f"💳 Баланс USDT - Всего: {total_usdt:.4f}, Свободно: {free_usdt:.4f}, Занято: {used_usdt:.4f}")
        
        return {
            'total': float(total_usdt),
            'free': float(free_usdt), 
            'used': float(used_usdt)
        }

async def set_leverage_fixed():
    """Установить кредитное плечо"""
    async with error_handler("set_leverage"):
        try:
            params = {
                'openType': MARGIN_ISOLATED,
                'positionType': SIDE_BUY,
            }
            await exchange.set_leverage(LEVERAGE, SYMBOL, params)
            logger.info(f"⚡ Плечо установлено: {LEVERAGE}x (isolated)")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить плечо: {e}")

async def calculate_qty_simple() -> float:
    """ПРОСТОЙ РАСЧЕТ: фиксированная сумма / цена"""
    async with error_handler("calculate_qty_simple"):
        price = await get_current_price()
        
        # Расчет с учетом плеча
        quantity = (FIXED_AMOUNT_USDT * LEVERAGE) / price
        
        logger.info(f"🔢 Расчет: ({FIXED_AMOUNT_USDT} * {LEVERAGE}) / {price} = {quantity}")
        
        # Округляем до 1 знака для фьючерсов
        quantity = round(quantity, 1)
        
        # Проверяем минимальное количество
        if quantity < 1.0:
            quantity = 1.0
            logger.warning(f"⚠️ Количество увеличено до минимального: 1")
        
        # Проверяем минимальную сумму
        order_value = quantity * price
        logger.info(f"💵 Стоимость ордера: {quantity} * {price} = {order_value:.2f} USDT")
        
        if order_value < 2.2616:
            min_quantity = 2.2616 / price
            quantity = max(quantity, min_quantity)
            quantity = round(quantity, 1)
            logger.warning(f"⚠️ Количество увеличено для минимальной суммы 2.2616 USDT")
            
        logger.info(f"📊 Итоговое количество: {quantity} {SYMBOL}")
        return quantity

async def create_order_mexc_format(symbol: str, side: int, vol: float, price: float = None, 
                                 leverage: int = LEVERAGE, openType: int = MARGIN_ISOLATED, 
                                 externalOid: str = None):
    """Создать ордер в формате MEXC API"""
    
    order_params = {
        'symbol': symbol,
        'vol': vol,
        'leverage': leverage,
        'side': side,
        'type': ORDER_MARKET,  # всегда рыночный ордер
        'openType': openType,
    }
    
    if externalOid:
        order_params['externalOid'] = externalOid
    
    logger.info(f"🎯 Создание ордера MEXC формате:")
    logger.info(f"   Параметры: {json.dumps(order_params, indent=2)}")
    
    return order_params

async def submit_order_mexc(order_data: dict):
    """Отправить ордер на MEXC с повторными попытками"""
    
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"🔄 Попытка {attempt + 1}/{MAX_RETRIES} отправки ордера")
            
            # Используем низкоуровневый API вызов для MEXC
            response = await exchange.contractPrivatePostOrderSubmit(order_data)
            
            logger.info(f"✅ Ордер отправлен успешно на попытке {attempt + 1}!")
            logger.info(f"   Ответ: {response}")
            
            return response
            
        except ccxt.RequestTimeout as e:
            logger.warning(f"⏰ Таймаут на попытке {attempt + 1}: {str(e)}")
            
            if attempt < MAX_RETRIES - 1:
                logger.info(f"💤 Повтор через {RETRY_DELAY} сек...")
                await asyncio.sleep(RETRY_DELAY)
                continue
            else:
                logger.error(f"🔴 Все попытки завершились таймаутом")
                raise
                
        except ccxt.NetworkError as e:
            logger.warning(f"🌐 Ошибка сети на попытке {attempt + 1}: {str(e)}")
            
            if attempt < MAX_RETRIES - 1:
                logger.info(f"💤 Повтор через {RETRY_DELAY} сек...")
                await asyncio.sleep(RETRY_DELAY)
                continue
            else:
                logger.error(f"🔴 Все попытки завершились ошибкой сети")
                raise
                
        except Exception as e:
            logger.error(f"🔴 Ошибка на попытке {attempt + 1}: {str(e)}")
            raise

    return None

async def open_position_mexc(signal: str):
    global last_trade_info, active_position
    
    async with error_handler("open_position_mexc"):
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()} на {FIXED_AMOUNT_USDT} USDT с плечом {LEVERAGE}x")
        
        # Устанавливаем плечо
        try:
            await set_leverage_fixed()
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"⚠️ Продолжаем без установки плеча: {e}")
        
        # Проверяем баланс
        balance_data = await check_balance_detailed()
        balance = balance_data['total']
        logger.info(f"💳 Баланс: {balance:.2f} USDT, Требуется: {FIXED_AMOUNT_USDT} USDT")
        
        if balance < FIXED_AMOUNT_USDT:
            raise ValueError(f"❌ Недостаточно средств. Нужно: {FIXED_AMOUNT_USDT} USDT, есть: {balance:.2f} USDT")

        # Рассчитываем количество
        qty = await calculate_qty_simple()
        
        # Определяем сторону для MEXC API
        if signal.lower() == "buy":
            side = SIDE_BUY
            side_text = "BUY/LONG"
        else:
            side = SIDE_SELL  
            side_text = "SELL/SHORT"
        
        logger.info(f"🎯 Финальные параметры ордера: {side_text} {qty} {SYMBOL}")

        # Создаем ордер в формате MEXC
        external_oid = f"bot_{int(time.time())}_{signal}"
        order_data = await create_order_mexc_format(
            symbol=SYMBOL,
            side=side,
            vol=qty,
            leverage=LEVERAGE,
            openType=MARGIN_ISOLATED,
            externalOid=external_oid
        )

        # Отправляем ордер
        response = await submit_order_mexc(order_data)
        
        if not response:
            raise Exception("Не удалось отправить ордер после всех попыток")

        logger.info(f"✅ Ордер успешно отправлен: {response}")

        # Даем бирже время обработать ордер
        await asyncio.sleep(2)

        # Получаем цену входа
        entry_price = await get_current_price()

        # Сохраняем информацию о сделке
        active_position = True
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "side_text": side_text,
            "vol": qty, 
            "entry": entry_price, 
            "amount_usdt": FIXED_AMOUNT_USDT,
            "leverage": LEVERAGE,
            "balance": balance,
            "order_data": order_data,
            "response": response,
            "externalOid": external_oid,
            "timestamp": time.time()
        }

        position_size = qty * entry_price
        
        msg = (f"✅ {side_text} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty}\n"
               f"Депозит: {FIXED_AMOUNT_USDT} USDT\n"
               f"Плечо: {LEVERAGE}x\n"
               f"Размер позиции: {position_size:.2f} USDT\n"
               f"Цена: ${entry_price:.4f}\n"
               f"External OID: {external_oid}")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

async def close_position_mexc():
    global active_position, last_trade_info
    
    async with error_handler("close_position_mexc"):
        if not active_position:
            logger.info("❌ Нет активной позиции для закрытия")
            return {"status": "error", "message": "No active position"}

        # Получаем текущую позицию
        positions = await exchange.fetch_positions([SYMBOL])
        current_pos = None
        
        for pos in positions:
            if pos['symbol'] == SYMBOL and float(pos['contracts']) > 0:
                current_pos = pos
                break
        
        if not current_pos:
            logger.warning("⚠️ Позиция не найдена на бирже")
            active_position = False
            return {"status": "error", "message": "Position not found on exchange"}

        # Определяем сторону для закрытия
        if current_pos['side'] == "long":
            close_side = SIDE_CLOSE_LONG
            close_side_text = "CLOSE_LONG"
        else:
            close_side = SIDE_CLOSE_SHORT
            close_side_text = "CLOSE_SHORT"

        qty = float(current_pos['contracts'])
        
        logger.info(f"🔒 Закрытие позиции: {close_side_text} {qty} {SYMBOL}")

        # Создаем ордер закрытия в формате MEXC
        external_oid = f"close_{int(time.time())}"
        order_data = await create_order_mexc_format(
            symbol=SYMBOL,
            side=close_side,
            vol=qty,
            leverage=LEVERAGE,
            openType=MARGIN_ISOLATED,
            externalOid=external_oid
        )

        # Отправляем ордер закрытия
        response = await submit_order_mexc(order_data)
        
        if not response:
            raise Exception("Не удалось отправить ордер закрытия после всех попыток")

        # Даем бирже время обработать ордер
        await asyncio.sleep(2)
        
        exit_price = await get_current_price()
        
        # Расчет PnL
        entry_price = last_trade_info['entry'] if last_trade_info else float(current_pos['entryPrice'])
        pnl_percent = ((exit_price - entry_price) / entry_price * 100 * LEVERAGE * 
                      (1 if close_side == SIDE_CLOSE_LONG else -1))
        
        msg = (f"🔒 ПОЗИЦИЯ ЗАКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Тип: {close_side_text}\n"
               f"Количество: {qty}\n"
               f"Цена входа: ${entry_price:.4f}\n"
               f"Цена выхода: ${exit_price:.4f}\n"
               f"P&L: {pnl_percent:+.2f}%\n"
               f"External OID: {external_oid}")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        
        active_position = False
        logger.info("✅ ПОЗИЦИЯ УСПЕШНО ЗАКРЫТА")
        
        return {
            "status": "ok", 
            "message": "Position closed", 
            "pnl_percent": pnl_percent,
            "close_order": order_data
        }

# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    async with error_handler("startup"):
        logger.info("🚀 ЗАПУСК БОТА")
        
        try:
            await set_leverage_fixed()
        except:
            logger.warning("⚠️ Плечо не установлено при старте - продолжим без него")
        
        balance_data = await check_balance_detailed()
        balance = balance_data['total']
        price = await get_current_price()
        
        msg = (f"✅ MEXC Futures Bot ЗАПУЩЕН!\n\n"
               f"💰 Баланс: {balance:.2f} USDT\n"
               f"📊 Символ: {SYMBOL}\n"
               f"💰 Цена: ${price:.4f}\n"
               f"💵 Фиксированная сумма: {FIXED_AMOUNT_USDT} USDT\n"
               f"⚡ Плечо: {LEVERAGE}x\n"
               f"🔧 Формат: MEXC Native API\n\n"
               f"💡 Готов к работе!")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 ОСТАНОВКА БОТА")
    try:
        await exchange.close()
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🔴 Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("📨 ПОЛУЧЕН WEBHOOK ЗАПРОС")
    
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        
        logger.info(f"📊 Webhook данные: signal={signal}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        asyncio.create_task(open_position_mexc(signal))
        
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/close")
async def close_position_endpoint(request: Request):
    """Принудительно закрыть позицию"""
    logger.info("🔒 ЗАПРОС НА ЗАКРЫТИЕ ПОЗИЦИИ")
    
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")
    
    try:
        result = await close_position_mexc()
        return result
    except Exception as e:
        logger.error(f"❌ Close position error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/order/mexc")
async def create_order_mexc_endpoint(request: Request):
    """Создать ордер в формате MEXC API"""
    logger.info("🎯 СОЗДАНИЕ ОРДЕРА MEXC ФОРМАТ")
    
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")
    
    try:
        data = await request.json()
        
        # Валидация обязательных полей
        required_fields = ['symbol', 'side', 'vol']
        for field in required_fields:
            if field not in data:
                return {"status": "error", "message": f"Missing required field: {field}"}
        
        # Устанавливаем значения по умолчанию
        order_data = {
            'symbol': data['symbol'],
            'side': data['side'],
            'vol': data['vol'],
            'leverage': data.get('leverage', LEVERAGE),
            'type': data.get('type', ORDER_MARKET),
            'openType': data.get('openType', MARGIN_ISOLATED),
        }
        
        if 'externalOid' in data:
            order_data['externalOid'] = data['externalOid']
        
        logger.info(f"📦 Создание кастомного ордера: {json.dumps(order_data, indent=2)}")
        
        # Отправляем ордер
        response = await submit_order_mexc(order_data)
        
        if response:
            return {
                "status": "ok", 
                "message": "Order created",
                "order_data": order_data,
                "response": response
            }
        else:
            return {"status": "error", "message": "Failed to create order"}
            
    except Exception as e:
        logger.error(f"❌ Create order error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health_check():
    try:
        price = await get_current_price()
        balance_data = await check_balance_detailed()
        balance = balance_data['total']
        
        # Проверяем активные позиции
        positions = await exchange.fetch_positions([SYMBOL])
        position_info = None
        for pos in positions:
            if pos['symbol'] == SYMBOL and float(pos['contracts']) > 0:
                position_info = {
                    'side': pos['side'],
                    'contracts': float(pos['contracts']),
                    'entry_price': float(pos['entryPrice']),
                    'unrealized_pnl': float(pos['unrealizedPnl'])
                }
                break
        
        return {
            "status": "healthy",
            "exchange_connected": price > 0,
            "balance_available": balance > FIXED_AMOUNT_USDT,
            "active_position": active_position,
            "position_info": position_info,
            "current_price": price,
            "balance": balance_data,
            "fixed_amount": FIXED_AMOUNT_USDT,
            "leverage": LEVERAGE,
            "symbol": SYMBOL,
            "last_trade": last_trade_info,
            "mexc_constants": {
                "SIDE_BUY": SIDE_BUY,
                "SIDE_SELL": SIDE_SELL, 
                "SIDE_CLOSE_LONG": SIDE_CLOSE_LONG,
                "SIDE_CLOSE_SHORT": SIDE_CLOSE_SHORT,
                "ORDER_MARKET": ORDER_MARKET,
                "ORDER_LIMIT": ORDER_LIMIT,
                "MARGIN_ISOLATED": MARGIN_ISOLATED,
                "MARGIN_CROSS": MARGIN_CROSS
            },
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"❌ Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

@app.get("/")
async def home():
    global last_trade_info, active_position
    
    try:
        balance_data = await check_balance_detailed()
        balance = balance_data['total']
        price = await get_current_price()
        
        # Получаем информацию о позиции
        positions = await exchange.fetch_positions([SYMBOL])
        position_details = None
        for pos in positions:
            if pos['symbol'] == SYMBOL and float(pos['contracts']) > 0:
                position_details = {
                    'side': pos['side'],
                    'contracts': float(pos['contracts']),
                    'entry_price': float(pos['entryPrice']),
                    'unrealized_pnl': float(pos['unrealizedPnl'])
                }
                break
        
        status = "АКТИВНА" if active_position else "НЕТ"
        status_color = "success" if active_position else "warning"
        
        # Исправленная HTML строка без обратных слешей в f-строках
        html_content = f"""
        <html>
            <head>
                <title>MEXC Futures Bot</title>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial; background: #1e1e1e; color: white; padding: 20px; }}
                    .card {{ background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px; }}
                    .success {{ color: #00b894; }}
                    .warning {{ color: #fdcb6e; }}
                    .danger {{ color: #e74c3c; }}
                    .info {{ color: #74b9ff; }}
                    button {{ background: #00b894; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin: 5px; }}
                    .danger-btn {{ background: #e74c3c; }}
                    pre {{ background: #1a1a1a; padding: 10px; border-radius: 5px; overflow-x: auto; }}
                </style>
            </head>
            <body>
                <h1 class="success">🤖 MEXC Futures Bot (Native API)</h1>
                
                <div class="card">
                    <h3>💰 БАЛАНС</h3>
                    <p><b>USDT Всего:</b> {balance:.2f}</p>
                    <p><b>USDT Свободно:</b> {balance_data['free']:.2f}</p>
                    <p><b>USDT Занято:</b> {balance_data['used']:.2f}</p>
                </div>
                
                <div class="card">
                    <h3>📊 СТАТУС РЫНКА</h3>
                    <p><b>Символ:</b> {SYMBOL}</p>
                    <p><b>Текущая цена:</b> ${price:.4f}</p>
                    <p><b>Позиция:</b> <span class="{status_color}">{status}</span></p>
                </div>
        """
        
        if position_details:
            pnl_class = "success" if position_details['unrealized_pnl'] > 0 else "danger"
            html_content += f"""
                <div class="card">
                    <h3>📈 ИНФОРМАЦИЯ О ПОЗИЦИИ</h3>
                    <p><b>Сторона:</b> {position_details['side'].upper()}</p>
                    <p><b>Контракты:</b> {position_details['contracts']}</p>
                    <p><b>Цена входа:</b> ${position_details['entry_price']:.4f}</p>
                    <p><b>Незакрытый P&L:</b> <span class="{pnl_class}">{position_details['unrealized_pnl']:.4f} USDT</span></p>
                </div>
            """
        
        html_content += f"""
                <div class="card">
                    <h3>⚡ НАСТРОЙКИ</h3>
                    <p><b>Фиксированная сумма:</b> {FIXED_AMOUNT_USDT} USDT</p>
                    <p><b>Плечо:</b> {LEVERAGE}x</p>
                    <p><b>Формат API:</b> MEXC Native</p>
                </div>
                
                <div class="card">
                    <h3>🔧 КОНСТАНТЫ MEXC API</h3>
                    <pre>SIDE_BUY = {SIDE_BUY} (Open Long)
SIDE_SELL = {SIDE_SELL} (Open Short)  
SIDE_CLOSE_LONG = {SIDE_CLOSE_LONG} (Close Long)
SIDE_CLOSE_SHORT = {SIDE_CLOSE_SHORT} (Close Short)
ORDER_MARKET = {ORDER_MARKET}
ORDER_LIMIT = {ORDER_LIMIT}
MARGIN_ISOLATED = {MARGIN_ISOLATED}
MARGIN_CROSS = {MARGIN_CROSS}</pre>
                </div>
        """
        
        if last_trade_info:
            html_content += f"""
                <div class="card">
                    <h3>📈 Последняя сделка</h3>
                    <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False, default=str)}</pre>
                </div>
            """
        
        # Исправленная часть с кнопками
        close_button = ""
        if active_position:
            close_button = '<form action="/close" method="post" style="margin: 10px 0;"><button type="submit" class="danger-btn">🔒 Принудительно закрыть позицию</button></form>'
        
        order_link = ""
        if last_trade_info and 'order_id' in last_trade_info:
            order_link = f'<p><a href="/order/{last_trade_info["order_id"]}" style="color: #74b9ff;">🔍 Проверить статус ордера</a></p>'
        
        html_content += f"""
                <div class="card">
                    <h3>🔧 Действия</h3>
                    <p><a href="/health" style="color: #74b9ff;">Health Check</a></p>
                    {close_button}
                    {order_link}
                </div>
            </body>
        </html>
        """
        
        return HTMLResponse(html_content)
    except Exception as e:
        return HTMLResponse(f"<h1>Error: {str(e)}</h1>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
