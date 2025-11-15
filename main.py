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

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан!")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === Символ ===
SYMBOL = "XRP/USDT:USDT"  # Фьючерсный формат

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange (фьючерсы с увеличенными таймаутами) ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # Фьючерсы
        'recvWindow': 15000,
    },
    'timeout': 30000,
    'rateLimit': 1000,
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === Вспомогательные функции ===
@asynccontextmanager
async def error_handler(operation: str):
    try:
        yield
    except Exception as e:
        error_msg = f"❌ Ошибка в {operation}: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg[:4000])
        except:
            pass
        raise

async def create_order_with_retry(symbol, side, qty, max_retries=3):
    """Создать ордер с повторными попытками при таймаутах"""
    for attempt in range(max_retries):
        try:
            logger.info(f"🔄 Попытка {attempt + 1} создать ордер {side} {qty} {symbol}...")
            order = await exchange.create_market_order(symbol, side, qty)
            logger.info(f"✅ Ордер успешно создан на попытке {attempt + 1}")
            return order
        except ccxt.RequestTimeout as e:
            logger.warning(f"⏰ Таймаут попытки {attempt + 1}. Ждем 3 секунды...")
            if attempt == max_retries - 1:  # Последняя попытка
                logger.error("❌ Все попытки создания ордера завершились таймаутом")
                raise e
            await asyncio.sleep(3)
        except ccxt.BaseError as e:
            logger.error(f"🔴 Ошибка API MEXC на попытке {attempt + 1}: {e}")
            raise e
        except Exception as e:
            logger.error(f"🔴 Неизвестная ошибка на попытке {attempt + 1}: {e}")
            raise e

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

async def set_leverage():
    """Установить кредитное плечо"""
    async with error_handler("set_leverage"):
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"⚡ Плечо установлено: {LEVERAGE}x")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить плечо (может быть уже установлено): {e}")

async def calculate_qty(usd_amount: float) -> float:
    async with error_handler("calculate_qty"):
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        # Рассчитываем количество с учетом плеча
        quantity = (usd_amount * LEVERAGE) / price
        
        # Округляем до 1 знака для фьючерсов
        quantity = round(quantity, 1)
        
        # Минимальное количество
        if quantity < 1.0:
            quantity = 1.0
            
        logger.info(f"📊 Рассчитано количество: {quantity} {SYMBOL} за {usd_amount} USDT с плечом {LEVERAGE}x")
        return quantity

async def close_position():
    """Закрыть текущую позицию"""
    global active_position, last_trade_info
    
    if not active_position or not last_trade_info:
        logger.warning("⚠️ Нет активной позиции для закрытия")
        return
    
    async with error_handler("close_position"):
        current_side = last_trade_info['side']
        close_side = 'sell' if current_side == 'buy' else 'buy'
        
        logger.info(f"🔒 Закрываем позицию: {current_side} → {close_side}")
        
        # Создаем рыночный ордер для закрытия с повторами
        order = await create_order_with_retry(SYMBOL, close_side, last_trade_info['qty'])
        
        # Получаем цену выхода
        exit_price = await get_current_price()
        
        # Рассчитываем PnL
        entry = last_trade_info['entry']
        qty = last_trade_info['qty']
        if current_side == 'buy':
            pnl = (exit_price - entry) * qty
        else:
            pnl = (entry - exit_price) * qty
            
        msg = (f"🔒 ПОЗИЦИЯ ЗАКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Направление: {current_side.upper()} → {close_side.upper()}\n"
               f"Вход: ${entry:.4f}\n"
               f"Выход: ${exit_price:.4f}\n"
               f"Количество: {qty}\n"
               f"PnL: ${pnl:.2f}")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        
        active_position = False
        logger.info(f"✅ Позиция закрыта. PnL: ${pnl:.2f}")

async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position
    
    async with error_handler("open_position"):
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()}")
        
        # Устанавливаем плечо
        await set_leverage()
        
        # Проверяем баланс
        balance = await check_balance()
        logger.info(f"💳 Текущий баланс: {balance:.2f} USDT")
        
        if balance <= 5:
            raise ValueError(f"❌ Недостаточно средств: {balance:.2f} USDT")

        # Рассчитываем сумму для торговли
        usd = amount_usd or (balance * RISK_PERCENT / 100)
        logger.info(f"🎯 Риск: {RISK_PERCENT}% → {usd:.2f} USDT из {balance:.2f}")

        if usd < 5:
            usd = 5
            logger.info(f"📈 Сумма увеличена до минимальной: {usd} USDT")

        # Рассчитываем количество
        qty = await calculate_qty(usd)
        logger.info(f"📦 Рассчитанное количество: {qty}")
        
        if qty <= 0:
            raise ValueError(f"❌ Неверное количество: {qty}")

        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"🔄 Открываем {side.upper()} {qty} {SYMBOL}")

        # СОЗДАЕМ ОРДЕР С ПОВТОРАМИ ПРИ ТАЙМАУТАХ
        order = await create_order_with_retry(SYMBOL, side, qty)
        logger.info(f"✅ Ордер создан: {order['id']}")

        # Получаем цену входа
        entry_price = await get_current_price()

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
    async with error_handler("startup"):
        logger.info("🚀 ЗАПУСК БОТА")
        
        # Проверяем подключение к бирже
        try:
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
        except Exception as e:
            error_msg = f"❌ Ошибка при запуске бота: {e}"
            logger.error(error_msg)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)

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
    
    # Проверка авторизации
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        
        logger.info(f"📊 Webhook данные: signal={signal}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_position(signal))
        
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health_check():
    try:
        price = await get_current_price()
        balance = await check_balance()
        
        return {
            "status": "healthy",
            "exchange_connected": price > 0,
            "balance_available": balance > 0,
            "active_position": active_position,
            "current_price": price,
            "balance": balance,
            "symbol": SYMBOL,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"❌ Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

@app.get("/")
async def home():
    global last_trade_info, active_position
    
    try:
        balance = await check_balance()
        price = await get_current_price()
        
        status = "АКТИВНА" if active_position else "НЕТ"
        
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
                    .info {{ color: #74b9ff; }}
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
                </div>
                
                <div class="card">
                    <h3>⚡ НАСТРОЙКИ</h3>
                    <p><b>Плечо:</b> {LEVERAGE}x</p>
                    <p><b>Риск:</b> {RISK_PERCENT}%</p>
                </div>
                
                <div class="card info">
                    <h3>🔄 ФУНКЦИОНАЛ</h3>
                    <p><b>Повторы при таймаутах:</b> 3 попытки</p>
                    <p><b>Таймаут запросов:</b> 30 секунд</p>
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
    uvicorn.run(app, host="0.0.0.0", port=port)
