import os
import json
import asyncio
import logging
import hmac
import hashlib
import time
import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = "XRP_USDT"
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === MEXC API Client (ИСПРАВЛЕННАЯ ВЕРСИЯ) ===
class MEXCFuturesAPI:
    def __init__(self):
        self.base_url = "https://contract.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_API_SECRET
        logger.info("MEXC Futures API клиент инициализирован")
        
    def _sign(self, params):
        """Генерация подписи - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        # Сортируем параметры по ключу
        sorted_params = sorted(params.items())
        # Создаем строку для подписи
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        logger.info(f"Строка для подписи: {query_string}")
        
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        logger.info(f"Сгенерированная подпись: {signature}")
        return signature

    async def _request(self, method, endpoint, params=None):
        """Универсальный метод запроса - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        try:
            timestamp = str(int(time.time() * 1000))
            
            # Базовые параметры
            all_params = {
                'api_key': self.api_key,
                'req_time': timestamp,
            }
            
            # Добавляем пользовательские параметры если есть
            if params:
                all_params.update(params)
            
            # Генерируем подпись ДО добавления sign
            signature = self._sign(all_params)
            
            # Добавляем подпись в параметры
            all_params['sign'] = signature
            
            url = f"{self.base_url}{endpoint}"
            
            logger.info(f"MEXC API Request: {method} {endpoint}")
            logger.info(f"Params: {all_params}")
            
            async with aiohttp.ClientSession() as session:
                if method == 'GET':
                    async with session.get(url, params=all_params, timeout=10) as response:
                        result = await response.json()
                else:
                    # Для POST используем data, а не params
                    async with session.post(url, data=all_params, timeout=10) as response:
                        result = await response.json()
                
                logger.info(f"MEXC API Response: {result}")
                return result
                
        except Exception as e:
            logger.error(f"Ошибка MEXC API {endpoint}: {e}")
            return None

    async def get_account_assets(self):
        """Получить информацию о фьючерсном аккаунте"""
        logger.info("Запрос баланса фьючерсного аккаунта...")
        return await self._request('GET', '/api/v1/private/account/assets')

    async def get_balance(self):
        """Получить баланс USDT на фьючерсном счете - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        try:
            result = await self.get_account_assets()
            logger.info(f"Полный ответ баланса: {result}")
            
            if not result:
                logger.error("Нет ответа от API")
                return 0.0
                
            if not result.get('success'):
                error_msg = result.get('message', 'Unknown error')
                error_code = result.get('code', 'No code')
                logger.error(f"API Error {error_code}: {error_msg}")
                
                # Диагностика ошибки
                if error_code == 401:
                    logger.error("Ошибка 401: Проверьте API ключ и секрет")
                elif error_code == 400:
                    logger.error("Ошибка 400: Неверные параметры запроса")
                    
                return 0.0
            
            data = result.get('data', [])
            logger.info(f"Данные баланса: {data}")
            
            for asset in data:
                currency = asset.get('currency')
                available = asset.get('availableBalance')
                wallet_balance = asset.get('walletBalance')
                logger.info(f"Актив: {currency}, доступно: {available}, баланс: {wallet_balance}")
                
                if currency == 'USDT':
                    balance = float(available or 0)
                    logger.info(f"Найден баланс USDT: {balance}")
                    return balance
            
            logger.warning("USDT не найден в ответе")
            return 0.0
            
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return 0.0

    async def get_ticker(self, symbol=SYMBOL):
        """Получить тикер"""
        try:
            url = f"{self.base_url}/api/v1/contract/ticker"
            params = {'symbol': symbol}
            
            logger.info(f"Запрос цены для {symbol}...")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    result = await response.json()
                    logger.info(f"Ответ цены: {result}")
                    
                    if result.get('success'):
                        price = float(result['data']['lastPrice'])
                        logger.info(f"Цена {symbol}: {price}")
                        return price
                    else:
                        logger.error(f"Ошибка цены: {result.get('message')}")
                        return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения цены: {e}")
            return 0.0

    async def place_order(self, symbol, side, order_type, quantity, price=None, position_side=1):
        """Разместить ордер"""
        params = {
            'symbol': symbol,
            'positionType': position_side,
            'type': order_type,
            'quantity': str(quantity),
            'side': side,
        }
        
        if price is not None:
            params['price'] = str(price)
            
        logger.info(f"Размещение ордера: {params}")
        return await self._request('POST', '/api/v1/private/order/submit', params)

    async def place_market_order(self, symbol, side, quantity, position_side=1):
        """Разместить рыночный ордер"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type=2,  # market
            quantity=quantity,
            position_side=position_side
        )

# Создаем клиент API
mexc_api = MEXCFuturesAPI()

async def test_api_connection():
    """Тестирование подключения к API"""
    try:
        logger.info("🔧 ТЕСТИРОВАНИЕ API ПОДКЛЮЧЕНИЯ")
        
        # Тест 1: Проверка баланса
        balance = await mexc_api.get_balance()
        logger.info(f"Баланс: {balance}")
        
        # Тест 2: Проверка цены
        price = await mexc_api.get_ticker()
        logger.info(f"Цена: {price}")
        
        # Тест 3: Проверка доступности символа
        url = f"{mexc_api.base_url}/api/v1/contract/detail"
        params = {'symbol': SYMBOL}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                symbol_info = await response.json()
                logger.info(f"Информация о символе: {symbol_info}")
        
        return balance >= 0 and price > 0
        
    except Exception as e:
        logger.error(f"Ошибка тестирования API: {e}")
        return False

async def check_api_connection():
    """Проверить подключение к API"""
    try:
        logger.info("🔍 ПРОВЕРКА ПОДКЛЮЧЕНИЯ К API...")
        
        # Проверяем баланс
        balance = await mexc_api.get_balance()
        logger.info(f"Баланс USDT: {balance:.2f}")
        
        # Проверяем цену
        price = await mexc_api.get_ticker()
        logger.info(f"Цена {SYMBOL}: {price:.4f}")
        
        diagnostics = f"""
🔍 ДИАГНОСТИКА API:

💰 Баланс: {balance:.2f} USDT
📊 Символ: {SYMBOL}
💰 Цена: ${price:.4f}
⚡ Плечо: {LEVERAGE}x
🎯 Риск: {RISK_PERCENT}%

💡 СТАТУС: {'✅ ГОТОВ К ТОРГОВЛЕ' if balance > 5 else '⚠️ МАЛО СРЕДСТВ'}
"""
        
        logger.info(diagnostics)
        
        if balance > 0:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=diagnostics)
            return True
        else:
            error_msg = f"❌ Нет средств на счете. Баланс: {balance} USDT"
            logger.error(error_msg)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
            return False
        
    except Exception as e:
        error_msg = f"❌ Ошибка диагностики API: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        return False

async def calculate_quantity(usd_amount, symbol=SYMBOL):
    """Рассчитать количество для ордера"""
    try:
        logger.info(f"Расчет количества для {usd_amount} USDT")
        
        price = await mexc_api.get_ticker(symbol)
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        quantity = usd_amount / price
        quantity = round(quantity, 1)  # Округляем до 1 знака
        
        if quantity < 1:
            quantity = 1.0
            
        logger.info(f"Рассчитано количество: {quantity} {symbol} за {usd_amount} USDT по цене {price}")
        return quantity
        
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0

async def open_position(signal, amount_usd=None):
    """Открыть позицию"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"=== ПОПЫТКА ОТКРЫТИЯ ПОЗИЦИИ {signal.upper()} ===")
        
        # Проверяем подключение к API
        if not await check_api_connection():
            raise ValueError("Проблемы с подключением к API или нет средств")
        
        # Получаем баланс
        balance = await mexc_api.get_balance()
        logger.info(f"Баланс: {balance} USDT")
        
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance} USDT. Минимум 5 USDT требуется.")
        
        # Определяем сумму для торговли
        usd_amount = amount_usd or (balance * RISK_PERCENT / 100)
        if usd_amount < 5:
            usd_amount = 5
            
        logger.info(f"Сумма для торговли: {usd_amount} USDT")
        
        # Рассчитываем количество
        quantity = await calculate_quantity(usd_amount)
        if quantity <= 0:
            raise ValueError("Неверное количество")
        
        logger.info(f"Количество для ордера: {quantity}")
        
        # Определяем параметры ордера
        if signal == 'buy':
            order_side = 1  # open long
            position_side = 1  # long
        else:  # sell
            order_side = 2  # open short  
            position_side = 2  # short
        
        # Размещаем рыночный ордер
        order_result = await mexc_api.place_market_order(
            symbol=SYMBOL,
            side=order_side,
            quantity=quantity,
            position_side=position_side
        )
        
        logger.info(f"Результат ордера: {order_result}")
        
        if not order_result or not order_result.get('success'):
            error_msg = order_result.get('message', 'Unknown error') if order_result else 'No response'
            raise ValueError(f"Ошибка ордера: {error_msg}")
        
        # Получаем цену входа
        entry_price = await mexc_api.get_ticker(SYMBOL)
        
        # Сохраняем информацию о сделке
        active_position = True
        last_trade_info = {
            'signal': signal,
            'side': 'LONG' if signal == 'buy' else 'SHORT',
            'quantity': quantity,
            'entry_price': entry_price,
            'balance': balance,
            'timestamp': time.time()
        }
        
        # Отправляем уведомление
        msg = f"""✅ ПОЗИЦИЯ ОТКРЫТА
Символ: {SYMBOL}
Сторона: {'LONG' if signal == 'buy' else 'SHORT'}
Количество: {quantity}
Цена входа: ${entry_price:.4f}
Сумма: {usd_amount:.2f} USDT
Баланс: {balance:.2f} USDT"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")
        
    except Exception as e:
        error_msg = f"❌ Ошибка открытия позиции: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        active_position = False

# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    """Запуск при старте приложения"""
    try:
        logger.info("🚀 FASTAPI STARTUP EVENT ВЫЗВАН")
        
        # Ждем немного для инициализации
        await asyncio.sleep(3)
        
        # Тестируем API подключение
        await test_api_connection()
        
        # Запускаем диагностику
        await check_api_connection()
        
        balance = await mexc_api.get_balance()
        price = await mexc_api.get_ticker()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

📊 СТАТУС:
• Символ: {SYMBOL}
• Риск: {RISK_PERCENT}%
• Плечо: {LEVERAGE}x
• Баланс: {balance:.2f} USDT
• Цена: ${price:.4f}

💡 ДЕЙСТВИЯ:
{f"✅ Готов к работе! Отправьте webhook сигнал." if balance > 5 else "⚠️ Пополните счет минимум 5 USDT!"}"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")
        
    except Exception as e:
        error_msg = f"❌ ОШИБКА ПРИ СТАРТЕ БОТА: {e}"
        logger.error(error_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        except:
            pass

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook для получения торговых сигналов"""
    logger.info("📨 ПОЛУЧЕН WEBHOOK ЗАПРОС")
    
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        logger.warning("Неавторизованный webhook запрос")
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        amount = data.get("amount")
        
        logger.info(f"Webhook данные: signal={signal}, amount={amount}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_position(signal, amount))
        
        logger.info(f"✅ Сигнал {signal} принят в обработку")
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def home():
    """Главная страница"""
    global last_trade_info, active_position
    
    balance = await mexc_api.get_balance()
    price = await mexc_api.get_ticker()
    
    status = "АКТИВНА" if active_position else "НЕТ"
    
    logger.info("📊 Запрос главной страницы")
    
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
                .error {{ color: #e74c3c; }}
            </style>
        </head>
        <body>
            <h1 class="success">🤖 MEXC Futures Trading Bot</h1>
            
            <div class="card">
                <h3>💰 БАЛАНС</h3>
                <p><b>Фьючерсный счет:</b> <span class="{'success' if balance > 0 else 'error'}">{balance:.2f} USDT</span></p>
            </div>
            
            <div class="card">
                <h3>📊 СТАТУС ТОРГОВЛИ</h3>
                <p><b>Символ:</b> {SYMBOL}</p>
                <p><b>Цена:</b> ${price:.4f}</p>
                <p><b>Позиция:</b> <span class="{'success' if active_position else 'warning'}">{status}</span></p>
                <p><b>Риск:</b> {RISK_PERCENT}%</p>
                <p><b>Плечо:</b> {LEVERAGE}x</p>
            </div>
            
            <div class="card">
                <h3>📈 Последняя сделка</h3>
                <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
            </div>
            
            <div class="card">
                <h3>🔧 Действия</h3>
                <p><a href="/diagnostics">🔄 Запустить диагностику</a></p>
                <p><a href="/balance">💰 Проверить баланс</a></p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)

@app.get("/balance")
async def get_balance():
    """Проверить баланс"""
    logger.info("Запрос баланса через API")
    balance = await mexc_api.get_balance()
    
    return {
        "balance": balance,
        "currency": "USDT"
    }

@app.get("/diagnostics")
async def diagnostics():
    """Страница диагностики"""
    logger.info("Запрос страницы диагностики")
    
    balance = await mexc_api.get_balance()
    price = await mexc_api.get_ticker()
    
    html = f"""
    <html>
        <head><title>Диагностика</title></head>
        <body style="font-family: Arial; background: #1e1e1e; color: white; padding: 20px;">
            <h1>🔧 Диагностика системы</h1>
            <div style="background: #2d2d2d; padding: 20px; border-radius: 10px;">
                <h3>💰 БАЛАНС</h3>
                <p><b>Фьючерсный счет:</b> {balance:.2f} USDT</p>
                
                <h3>📊 ТОРГОВЛЯ</h3>
                <p><b>Символ:</b> {SYMBOL}</p>
                <p><b>Цена:</b> ${price:.4f}</p>
                
                <h3>🔑 API СТАТУС</h3>
                <p><b>API Key:</b> {'✅ Установлен' if MEXC_API_KEY else '❌ Отсутствует'}</p>
                <p><b>Secret Key:</b> {'✅ Установлен' if MEXC_API_SECRET else '❌ Отсутствует'}</p>
            </div>
            <br>
            <a href="/">← Назад</a>
        </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 ЗАПУСК UVICORN СЕРВЕРА")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
