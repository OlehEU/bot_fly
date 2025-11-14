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
        # В реальном приложении это приведет к остановке. Здесь просто логируем и продолжаем для имитации среды.
        logger.error(f"ОШИБКА: {secret} не задан! Используется заглушка/значение по умолчанию.")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TG_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "YOUR_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "YOUR_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = os.getenv("SYMBOL", "XRP/USDT:USDT") # Стандартный формат ccxt для фьючерсов
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "YOUR_SECRET")

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
async def set_initial_settings():
    """Установить режим маржи и кредитное плечо при запуске."""
    try:
        # 1. Установка режима маржи (Cross/Isolated)
        # Для простоты используем Cross (Кросс-маржа), который обычно является безопасным дефолтом.
        logger.info(f"Установка кросс-маржи для {SYMBOL}...")
        await exchange.set_margin_mode('cross', SYMBOL) 
        
        # 2. Установка кредитного плеча
        logger.info(f"Установка кредитного плеча: {LEVERAGE}x для {SYMBOL}...")
        await exchange.set_leverage(LEVERAGE, SYMBOL)
        
        logger.info("Настройки маржи и плеча успешно применены.")
    except Exception as e:
        logger.warning(f"Ошибка установки маржи/плеча. Проверьте, что позиция закрыта: {e}")

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
    """Проверить баланс USDT на фьючерсном аккаунте"""
    try:
        # Явно запрашиваем баланс для фьючерсного аккаунта (если поддерживается)
        balance_data = await exchange.fetch_balance({'type': 'future'})
        
        # MEXC использует 'USDT' в total/free. Для фьючерсов лучше смотреть 'free' или 'used' в 'info'
        usdt_free = balance_data.get('free', {}).get('USDT', 0)
        
        # Запасной вариант: берем общий баланс USDT, если 'free' не сработает
        if usdt_free == 0:
            usdt_free = balance_data.get('total', {}).get('USDT', 0)

        logger.info(f"Свободный баланс USDT (Futures): {usdt_free:.4f}")
        return float(usdt_free)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        return 0.0

async def calculate_qty(usd_amount: float) -> float:
    """
    Рассчитать количество для ордера, учитывая кредитное плечо.
    usd_amount - это используемая маржа.
    """
    try:
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
            
        # --- ИСПРАВЛЕНИЕ: Учет кредитного плеча ---
        # 1. Рассчитываем нольциональный объем (Notional Value)
        # Нольциональный объем = Маржа * Плечо
        notional_value = usd_amount * LEVERAGE
        
        # 2. Рассчитываем количество контрактов/монет
        # Qty = Нольциональный объем / Цена
        quantity = notional_value / price

        # 3. Получаем точность лота (количество знаков после запятой) для символа
        market = await exchange.fetch_market(SYMBOL)
        amount_precision = market['precision']['amount'] if market and 'precision' in market else 1 # Дефолт - 1
        
        # Округляем до нужной точности
        quantity = exchange.decimal_to_precision(quantity, ccxt.ROUND, amount_precision)
        quantity = float(quantity)
        
        if quantity < 1:
            # MEXC часто имеет минимальный размер лота. Если расчет слишком мал, ставим минимум (можно настроить)
            quantity = 1.0 
            
        logger.info(f"Рассчитано: (Маржа: {usd_amount:.2f} * Плечо: {LEVERAGE}) / Цена: {price:.4f} = Qty: {quantity}")
        return quantity
            
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0

async def open_position(signal: str, amount_usd=None):
    """Открыть позицию (упрощенная версия)"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"🚀 ПОПЫТКА ОТКРЫТИЯ ПОЗИЦИИ {signal.upper()}")
        
        if active_position:
             logger.info("Позиция уже открыта. Пропускаем сигнал.")
             return
        
        # --- ИСПРАВЛЕНИЕ: Установка настроек перед сделкой ---
        # Убеждаемся, что плечо и режим маржи установлены
        await set_initial_settings()
        
        balance = await check_balance()
        logger.info(f"Текущий свободный баланс: {balance:.2f} USDT")
        
        MIN_ORDER_USDT = 5.0 # Минимальный размер ордера, может отличаться
        
        if balance <= MIN_ORDER_USDT:
            raise ValueError(f"Недостаточно средств. Свободный баланс: {balance:.2f} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        
        if usd < MIN_ORDER_USDT:
            usd = MIN_ORDER_USDT
            logger.warning(f"Рисковая сумма ({usd:.2f} USDT) меньше минимальной. Используется {MIN_ORDER_USDT} USDT.")
        
        logger.info(f"Риск: {RISK_PERCENT}% → Используемая маржа: {usd:.2f} USDT")

        # Рассчитываем количество
        qty = await calculate_qty(usd)
        logger.info(f"Количество контрактов: {qty}")
        
        if qty <= 0:
            raise ValueError(f"Неверный qty: {qty}")

        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"Отправка рыночного ордера: {side.upper()} {qty} {SYMBOL}")

        # ПРОСТОЙ ВЫЗОВ - создаем рыночный ордер
        order = await exchange.create_market_order(SYMBOL, side, qty)
        logger.info(f"Ордер создан: {order}")

        # --- ИСПРАВЛЕНИЕ: Использование фактической цены входа ---
        # Цена входа должна быть средней ценой исполнения ордера, а не текущей ценой
        entry = order.get('average', order.get('price')) 
        if not entry:
             entry = await get_current_price() # Запасной вариант, если биржа не вернула цену немедленно

        # Сохраняем информацию о сделке
        active_position = True
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "qty": qty, 
            "entry": float(entry), 
            "margin_usd": usd,
            "leverage": LEVERAGE,
            "balance": balance,
            "order_id": order.get('id', 'N/A'),
            "timestamp": time.time()
        }

        msg = (f"✅ {side.upper()} ПОЗИЦИЯ ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Плечо: {LEVERAGE}x | Маржа: {usd:.2f} USDT\n"
               f"Количество: {qty}\n"
               f"Вход: ${entry:.6f}\n"
               f"Баланс до: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

    except Exception as e:
        err_msg = f"❌ Ошибка открытия {signal}: {type(e).__name__}: {str(e)}"
        logger.error(err_msg)
        # Отправляем ошибку в Telegram
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        except Exception as tg_e:
            logger.error(f"Не удалось отправить ошибку в Telegram: {tg_e}")
        # active_position = False # Не сбрасываем, т.к. может быть частичное исполнение. Лучше проверить позицию.


# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    """Запуск при старте приложения"""
    try:
        logger.info("🚀 ЗАПУСК БОТА")
        
        # Устанавливаем начальные настройки (плечо, режим маржи)
        await set_initial_settings()
        
        balance = await check_balance()
        price = await get_current_price()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

⚙️ Настройки:
Символ: {SYMBOL} | Плечо: {LEVERAGE}x | Риск: {RISK_PERCENT}%

💰 Баланс: {balance:.2f} USDT
💰 Текущая цена: ${price:.6f}

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
    
    # Пытаемся получить актуальные данные
    try:
        balance = await check_balance()
    except Exception:
        balance = 0.0 # В случае ошибки
        
    try:
        price = await get_current_price()
    except Exception:
        price = 0.0
    
    status = "АКТИВНА" if active_position else "НЕТ"
    
    html = f"""
    <html>
        <head>
            <title>MEXC Futures Bot</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: 'Inter', sans-serif; background: #121212; color: #e0e0e0; padding: 20px; }}
                h1 {{ color: #00b894; border-bottom: 2px solid #00b894; padding-bottom: 10px; margin-bottom: 20px; }}
                .card {{ background: #1e1e1e; padding: 20px; margin: 15px 0; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3); }}
                .card h3 {{ color: #bb86fc; margin-top: 0; }}
                .success {{ color: #00b894; font-weight: bold; }}
                .warning {{ color: #ffab40; font-weight: bold; }}
                pre {{ background: #2d2d2d; padding: 15px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; }}
                .key-value b {{ color: #ffffff; display: inline-block; width: 120px; }}
            </style>
        </head>
        <body>
            <h1>🤖 MEXC Futures Bot Status</h1>
            
            <div class="card">
                <h3>⚙️ НАСТРОЙКИ</h3>
                <div class="key-value"><p><b>Символ:</b> {SYMBOL}</p></div>
                <div class="key-value"><p><b>Плечо:</b> {LEVERAGE}x</p></div>
                <div class="key-value"><p><b>Риск:</b> {RISK_PERCENT}%</p></div>
            </div>

            <div class="card">
                <h3>💰 ФИНАНСЫ</h3>
                <div class="key-value"><p><b>USDT (Futures):</b> {balance:.2f}</p></div>
                <div class="key-value"><p><b>Текущая Цена:</b> ${price:.6f}</p></div>
                <div class="key-value"><p><b>Позиция:</b> <span class="{'success' if active_position else 'warning'}">{status}</span></p></div>
            </div>
            
            <div class="card">
                <h3>📈 Последняя сделка</h3>
                <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
            </div>
            
            <div class="card">
                <h3>🔗 WEBHOOK</h3>
                <p>POST /webhook (Authorization: Bearer {WEBHOOK_SECRET})</p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
