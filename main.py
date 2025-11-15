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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === ФИКСИРОВАННАЯ СУММА ===
FIXED_AMOUNT_USD = 5  # Всегда торгуем на 5 USDT

# === Символ ===
SYMBOL = "XRP/USDT"

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'timeout': 30000,
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

async def calculate_qty_simple() -> float:
    """ПРОСТОЙ РАСЧЕТ: фиксированная сумма / текущая цена"""
    async with error_handler("calculate_qty_simple"):
        price = await get_current_price()
        
        # Простой расчет: 5 USDT / цена
        quantity = FIXED_AMOUNT_USD / price
        
        # Округляем до целых чисел (XRP обычно целыми)
        quantity = int(quantity)
        
        # Минимум 1 XRP
        if quantity < 1:
            quantity = 1
            
        logger.info(f"📊 Купим {quantity} XRP за {FIXED_AMOUNT_USD} USDT (цена: {price:.4f})")
        return float(quantity)

async def open_position_simple(signal: str):
    global last_trade_info, active_position
    
    async with error_handler("open_position_simple"):
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()} на {FIXED_AMOUNT_USD} USDT")
        
        # Проверяем баланс
        balance = await check_balance()
        if balance < FIXED_AMOUNT_USD:
            raise ValueError(f"❌ Недостаточно средств. Нужно: {FIXED_AMOUNT_USD} USDT, есть: {balance:.2f} USDT")

        # Рассчитываем количество (ПРОСТОЙ РАСЧЕТ)
        qty = await calculate_qty_simple()
        
        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"🔄 Открываем {side.upper()} {qty} {SYMBOL}")

        # Создаем рыночный ордер
        order = await exchange.create_market_order(SYMBOL, side, qty)
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
            "amount_usd": FIXED_AMOUNT_USD,
            "balance": balance,
            "order_id": order['id'],
            "timestamp": time.time()
        }

        msg = (f"✅ {side.upper()} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty} XRP\n"
               f"Сумма: {FIXED_AMOUNT_USD} USDT\n"
               f"Цена: ${entry_price:.4f}\n"
               f"Баланс: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    async with error_handler("startup"):
        logger.info("🚀 ЗАПУСК БОТА")
        
        balance = await check_balance()
        price = await get_current_price()
        
        msg = f"""✅ MEXC Spot Bot ЗАПУЩЕН!

💰 Баланс: {balance:.2f} USDT
📊 Символ: {SYMBOL}
💰 Цена: ${price:.4f}
💵 Фиксированная сумма: {FIXED_AMOUNT_USD} USDT

💡 Готов к работе!"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")

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
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_position_simple(signal))
        
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
            "balance_available": balance > FIXED_AMOUNT_USD,
            "active_position": active_position,
            "current_price": price,
            "balance": balance,
            "fixed_amount": FIXED_AMOUNT_USD,
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
                <title>MEXC Simple Bot</title>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial; background: #1e1e1e; color: white; padding: 20px; }}
                    .card {{ background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px; }}
                    .success {{ color: #00b894; }}
                    .warning {{ color: #fdcb6e; }}
                </style>
            </head>
            <body>
                <h1 class="success">🤖 MEXC Simple Bot</h1>
                
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
                    <p><b>Фиксированная сумма:</b> {FIXED_AMOUNT_USD} USDT</p>
                </div>
                
                <div class="card">
                    <h3>📈 Последняя сделка</h3>
                    <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
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
