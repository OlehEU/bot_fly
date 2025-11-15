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
from pydantic import BaseModel
from telegram import Bot

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

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
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === Исправленный формат символов для MEXC ===
SYMBOL_SPOT = "XRPUSDT"  # Для спотовой торговли
SYMBOL_FUTURES = "XRP_USDT"  # Для фьючерсов (правильный формат для MEXC)
SYMBOL = SYMBOL_FUTURES  # Используем фьючерсы

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # Для фьючерсов
        'adjustForTimeDifference': True,
    },
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === Модели данных ===
class WebhookData(BaseModel):
    signal: str
    amount_usd: float = None
    close_current: bool = False

# === Вспомогательные функции ===
@asynccontextmanager
async def error_handler(operation: str):
    """Контекстный менеджер для обработки ошибок"""
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
    """Получить текущую цену символа"""
    async with error_handler("get_current_price"):
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.info(f"Текущая цена {SYMBOL}: {price:.6f}")
        return price

async def check_balance() -> float:
    """Проверить баланс USDT"""
    async with error_handler("check_balance"):
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        logger.info(f"Баланс USDT: {usdt:.4f}")
        return float(usdt)

async def set_leverage():
    """Установить кредитное плечо"""
    async with error_handler("set_leverage"):
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"Плечо установлено: {LEVERAGE}x")
        except Exception as e:
            logger.warning(f"Не удалось установить плечо (может быть уже установлено): {e}")

async def calculate_qty(usd_amount: float) -> float:
    """Рассчитать количество для ордера с учетом минимальных лотов"""
    async with error_handler("calculate_qty"):
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        # Рассчитываем количество
        quantity = usd_amount / price
        
        # Получаем информацию о рынке для минимального количества
        market = await exchange.load_markets()
        symbol_info = market[SYMBOL]
        
        # Округляем до правильного шага
        if symbol_info.get('precision', {}).get('amount'):
            precision = symbol_info['precision']['amount']
            quantity = exchange.amount_to_precision(SYMBOL, quantity)
        
        quantity = float(quantity)
        
        # Проверяем минимальное количество
        min_amount = symbol_info.get('limits', {}).get('amount', {}).get('min', 0)
        if quantity < min_amount:
            quantity = min_amount
            logger.warning(f"Количество увеличено до минимального: {min_amount}")
        
        logger.info(f"Рассчитано количество: {quantity} {SYMBOL} за {usd_amount} USDT")
        return quantity

async def check_order_status(order_id: str):
    """Проверить статус ордера с правильной обработкой для MEXC"""
    async with error_handler("check_order_status"):
        order = await exchange.fetch_order(order_id, SYMBOL)
        
        # Для рыночных ордеров используем cummulativeQuoteQty для расчета реальной цены
        if order['type'] == 'market' and order['filled'] > 0:
            cum_quote_qty = float(order['info'].get('cummulativeQuoteQty', 0))
            filled_qty = float(order['filled'])
            
            if filled_qty > 0:
                actual_price = cum_quote_qty / filled_qty
                logger.info(f"Реальная цена исполнения: {actual_price:.6f}")
                order['actual_price'] = actual_price
        
        return order

async def handle_pending_order(order_id: str, timeout: int = 30):
    """Ожидание исполнения ордера с таймаутом"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        order = await check_order_status(order_id)
        
        if order['status'] == 'closed' or order['status'] == 'filled':
            logger.info("✅ Ордер успешно исполнен")
            return order
        elif order['status'] == 'canceled':
            logger.error("❌ Ордер отменен")
            return None
        elif order['status'] == 'rejected':
            logger.error("❌ Ордер отклонен биржей")
            return None
        
        logger.info(f"Ордер в статусе: {order['status']}, ждем...")
        await asyncio.sleep(2)  # Проверяем каждые 2 секунды
    
    logger.error("⏰ Таймаут ожидания ордера")
    return None

def calculate_pnl(entry: float, exit: float, qty: float, side: str) -> float:
    """Рассчитать PnL"""
    if side == 'buy':
        return (exit - entry) * qty
    else:
        return (entry - exit) * qty

async def close_position():
    """Закрыть текущую позицию"""
    global active_position, last_trade_info
    
    if not active_position or not last_trade_info:
        logger.warning("Нет активной позиции для закрытия")
        return
    
    async with error_handler("close_position"):
        current_side = last_trade_info['side']
        close_side = 'sell' if current_side == 'buy' else 'buy'
        
        logger.info(f"Закрываем позицию: {current_side} → {close_side}")
        
        # Создаем рыночный ордер для закрытия
        order = await exchange.create_market_order(
            SYMBOL, 
            close_side, 
            last_trade_info['qty']
        )
        
        # Ждем исполнения ордера
        executed_order = await handle_pending_order(order['id'])
        
        if executed_order:
            exit_price = executed_order.get('actual_price', await get_current_price())
            pnl = calculate_pnl(last_trade_info['entry'], exit_price, last_trade_info['qty'], current_side)
            
            msg = (f"🔒 ПОЗИЦИЯ ЗАКРЫТА\n"
                   f"Символ: {SYMBOL}\n"
                   f"Направление: {current_side.upper()} → {close_side.upper()}\n"
                   f"Вход: ${last_trade_info['entry']:.4f}\n"
                   f"Выход: ${exit_price:.4f}\n"
                   f"Количество: {last_trade_info['qty']}\n"
                   f"PnL: ${pnl:.2f}")
            
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
            
            active_position = False
            logger.info(f"✅ Позиция закрыта. PnL: ${pnl:.2f}")
        else:
            raise Exception("Не удалось закрыть позицию - ордер не исполнен")

async def get_position_info():
    """Получить информацию о текущей позиции"""
    async with error_handler("get_position_info"):
        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if (pos['symbol'] == SYMBOL.replace("_", "/") and 
                float(pos['contracts']) > 0):
                return pos
        return None

async def open_position(signal: str, amount_usd=None):
    """Открыть позицию (исправленная версия для MEXC)"""
    global last_trade_info, active_position
    
    async with error_handler("open_position"):
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()}")
        
        # Проверяем, есть ли активная позиция
        if active_position:
            await close_position()
            await asyncio.sleep(1)  # Даем время на закрытие
        
        # Устанавливаем плечо
        await set_leverage()
        
        # Проверяем баланс
        balance = await check_balance()
        logger.info(f"Текущий баланс: {balance:.2f} USDT")
        
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance:.2f} USDT")

        # Рассчитываем сумму для торговли
        usd = amount_usd or (balance * RISK_PERCENT / 100)
        logger.info(f"Риск: {RISK_PERCENT}% → {usd:.2f} USDT из {balance:.2f}")

        if usd < 5:
            usd = 5
            logger.info(f"Сумма увеличена до минимальной: {usd} USDT")

        # Рассчитываем количество
        qty = await calculate_qty(usd)
        logger.info(f"Рассчитанное количество: {qty}")
        
        if qty <= 0:
            raise ValueError(f"Неверное количество: {qty}")

        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"Открываем {side.upper()} {qty} {SYMBOL}")

        # Создаем рыночный ордер
        order = await exchange.create_market_order(SYMBOL, side, qty)
        logger.info(f"Ордер создан: {order['id']}")

        # Ждем исполнения и получаем реальную цену
        executed_order = await handle_pending_order(order['id'])
        
        if not executed_order:
            raise Exception("Ордер не исполнен в течение таймаута")
        
        # Получаем реальную цену входа
        entry_price = executed_order.get('actual_price', await get_current_price())

        # Сохраняем информацию о сделке
        active_position = True
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "qty": qty, 
            "entry": entry_price, 
            "balance": balance,
            "order_id": order['id'],
            "timestamp": time.time(),
            "leverage": LEVERAGE
        }

        msg = (f"✅ {side.upper()} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty}\n"
               f"Вход: ${entry_price:.4f}\n"
               f"Плечо: {LEVERAGE}x\n"
               f"Баланс: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    """Запуск при старте приложения"""
    async with error_handler("startup"):
        logger.info("🚀 ЗАПУСК БОТА")
        
        # Проверяем подключение
        balance = await check_balance()
        price = await get_current_price()
        await set_leverage()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

💰 Баланс: {balance:.2f} USDT
📊 Символ: {SYMBOL}
💰 Цена: ${price:.4f}
⚡ Плечо: {LEVERAGE}x
📈 Риск: {RISK_PERCENT}%

💡 Готов к работе!"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")

@app.on_event("shutdown")
async def shutdown_event():
    """Очистка при остановке"""
    logger.info("🛑 ОСТАНОВКА БОТА")
    try:
        await exchange.close()
        msg = "🔴 Бот остановлен"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook для получения торговых сигналов"""
    logger.info("📨 ПОЛУЧЕН WEBHOOK ЗАПРОС")
    
    # Проверка авторизации
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        body = await request.body()
        data = json.loads(body)
        
        signal = data.get("signal", "").lower()
        amount_usd = data.get("amount_usd")
        close_current = data.get("close_current", False)
        
        logger.info(f"Webhook данные: signal={signal}, amount_usd={amount_usd}, close_current={close_current}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        # Закрыть текущую позицию если нужно
        if close_current and active_position:
            await close_position()
            await asyncio.sleep(1)
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_position(signal, amount_usd))
        
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        price = await get_current_price()
        balance = await check_balance()
        position_info = await get_position_info()
        
        return {
            "status": "healthy",
            "exchange_connected": price > 0,
            "balance_available": balance > 0,
            "active_position": active_position,
            "current_price": price,
            "balance": balance,
            "position_info": position_info,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

@app.get("/")
async def home():
    """Главная страница"""
    global last_trade_info, active_position
    
    try:
        balance = await check_balance()
        price = await get_current_price()
        position_info = await get_position_info()
        
        status = "АКТИВНА" if active_position else "НЕТ"
        position_details = ""
        
        if position_info:
            position_details = f"""
            <p><b>Размер позиции:</b> {position_info.get('contracts', 0)}</p>
            <p><b>PnL:</b> ${position_info.get('unrealizedPnl', 0):.2f}</p>
            """
        
        html = f"""
        <html>
            <head>
                <title>MEXC Futures Bot</title>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial; background: #1e1e1e; color: white; padding: 20px; }}
                    .card {{ background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px; }}
                    .success {{ color: #00b894; }}
                    .warning {{ color: #fdcb6e; }}
                    .danger {{ color: #e17055; }}
                </style>
            </head>
            <body>
                <h1 class="success">🤖 MEXC Futures Bot</h1>
                
                <div class="card">
                    <h3>💰 БАЛАНС</h3>
                    <p><b>USDT:</b> {balance:.2f}</p>
                </div>
                
                <div class="card">
                    <h3>📊 СТАТУС</h3>
                    <p><b>Символ:</b> {SYMBOL}</p>
                    <p><b>Цена:</b> ${price:.4f}</p>
                    <p><b>Позиция:</b> <span class="{'success' if active_position else 'warning'}">{status}</span></p>
                    {position_details}
                </div>
                
                <div class="card">
                    <h3>⚡ НАСТРОЙКИ</h3>
                    <p><b>Плечо:</b> {LEVERAGE}x</p>
                    <p><b>Риск:</b> {RISK_PERCENT}%</p>
                </div>
                
                <div class="card">
                    <h3>📈 Последняя сделка</h3>
                    <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
                </div>
                
                <div class="card">
                    <h3>🔧 Действия</h3>
                    <p><a href="/health" style="color: #74b9ff;">Health Check</a></p>
                </div>
            </body>
        </html>
        """
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<h1>Error: {str(e)}</h1>")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )
