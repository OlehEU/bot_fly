import os
import json
import asyncio
import logging
import time
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
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = "XRP/USDT:USDT"  # Стандартный формат ccxt для фьючерсов
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange ===
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
        price = float(ticker['last'])
        logger.info(f"Текущая цена {SYMBOL}: {price:.6f}")
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return 0.0

async def check_balance() -> float:
    """Проверить баланс USDT"""
    try:
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        logger.info(f"Баланс USDT: {usdt:.4f}")
        return float(usdt)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        return 0.0

async def calculate_qty(usd_amount: float) -> float:
    """Рассчитать количество для ордера"""
    try:
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        # Простой расчет
        quantity = usd_amount / price
        quantity = round(quantity, 1)  # Округляем до 1 знака
        
        if quantity < 1:
            quantity = 1.0
            
        logger.info(f"Рассчитано количество: {quantity} {SYMBOL} за {usd_amount} USDT")
        return quantity
        
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0

async def open_position(signal: str, amount_usd=None):
    """Открыть позицию (упрощенная версия)"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()}")
        
        # Быстрая проверка баланса
        balance = await check_balance()
        logger.info(f"Текущий баланс: {balance:.2f} USDT")
        
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance:.2f} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        logger.info(f"Риск: {RISK_PERCENT}% → {usd:.2f} USDT из {balance:.2f}")

        if usd < 5:
            usd = 5

        # Рассчитываем количество
        qty = await calculate_qty(usd)
        logger.info(f"Рассчитанное количество: {qty}")
        
        if qty <= 0:
            raise ValueError(f"Неверный qty: {qty}")

        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"Открываем {side.upper()} {qty} {SYMBOL}")

        # ПРОСТОЙ ВЫЗОВ - создаем рыночный ордер
        order = await exchange.create_market_order(SYMBOL, side, qty)
        logger.info(f"Ордер создан: {order}")

        # Получаем цену входа
        entry = await get_current_price()

        # Сохраняем информацию о сделке
        active_position = True
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "qty": qty, 
            "entry": entry, 
            "balance": balance,
            "order_id": order.get('id', 'N/A'),
            "timestamp": time.time()
        }

        msg = (f"✅ {side.upper()} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty}\n"
               f"Вход: ${entry:.4f}\n"
               f"Баланс: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

    except Exception as e:
        err_msg = f"❌ Ошибка открытия {signal}: {str(e)}"
        logger.error(err_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        except:
            pass
        active_position = False

# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    """Запуск при старте приложения"""
    try:
        logger.info("🚀 ЗАПУСК БОТА")
        
        balance = await check_balance()
        price = await get_current_price()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

💰 Баланс: {balance:.2f} USDT
📊 Символ: {SYMBOL}
💰 Цена: ${price:.4f}

💡 Готов к работе!"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")
        
    except Exception as e:
        error_msg = f"❌ Ошибка при старте: {e}"
        logger.error(error_msg)

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook для получения торговых сигналов"""
    logger.info("📨 ПОЛУЧЕН WEBHOOK ЗАПРОС")
    
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        
        logger.info(f"Webhook данные: signal={signal}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_position(signal))
        
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def home():
    """Главная страница"""
    global last_trade_info, active_position
    
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
                <h3>📈 Последняя сделка</h3>
                <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
