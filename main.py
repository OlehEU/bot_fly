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

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        logger.error(f"ОШИБКА: {secret} не задан!")
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

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

# === MEXC API Client ===
class MEXCFuturesAPI:
    def __init__(self):
        self.base_url = "https://contract.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_API_SECRET
        logger.info("MEXC Futures API клиент инициализирован")
        
    def _sign(self, params):
        """Генерация подписи для фьючерсов"""
        sorted_params = sorted(params.items())
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _request(self, method, endpoint, params=None):
        """Универсальный метод запроса"""
        try:
            timestamp = str(int(time.time() * 1000))
            all_params = {
                'api_key': self.api_key,
                'req_time': timestamp,
                **(params or {})
            }
            
            signature = self._sign(all_params)
            all_params['sign'] = signature
            
            url = f"{self.base_url}{endpoint}"
            
            logger.info(f"MEXC Futures API Request: {method} {endpoint}")
            
            async with aiohttp.ClientSession() as session:
                if method == 'GET':
                    async with session.get(url, params=all_params, timeout=10) as response:
                        result = await response.json()
                else:
                    async with session.post(url, data=all_params, timeout=10) as response:
                        result = await response.json()
                
                logger.info(f"MEXC Futures API Response: {result}")
                return result
                
        except Exception as e:
            logger.error(f"Ошибка MEXC Futures API {endpoint}: {e}")
            return None

    async def get_account_assets(self):
        """Получить информацию о фьючерсном аккаунте"""
        logger.info("Запрос баланса фьючерсного аккаунта...")
        return await self._request('GET', '/api/v1/private/account/assets')

    async def get_balance(self):
        """Получить баланс USDT на фьючерсном счете"""
        try:
            result = await self.get_account_assets()
            
            if not result:
                logger.error("Нет ответа от API фьючерсов")
                return 0.0
                
            if not result.get('success'):
                error_msg = result.get('message', 'Unknown error')
                logger.error(f"Futures API Error: {error_msg}")
                return 0.0
            
            data = result.get('data', [])
            logger.info(f"Данные фьючерсного баланса: {data}")
            
            for asset in data:
                currency = asset.get('currency')
                available = asset.get('availableBalance')
                wallet_balance = asset.get('walletBalance')
                logger.info(f"Фьючерсный актив: {currency}, доступно: {available}, баланс кошелька: {wallet_balance}")
                
                if currency == 'USDT':
                    balance = float(available or 0)
                    logger.info(f"Найден фьючерсный баланс USDT: {balance}")
                    return balance
            
            # Если USDT не найден
            available_currencies = [f"{a.get('currency')}: {a.get('availableBalance')}" for a in data]
            logger.warning(f"USDT не найден на фьючерсном счете. Доступные валюты: {available_currencies}")
            return 0.0
            
        except Exception as e:
            logger.error(f"Ошибка получения фьючерсного баланса: {e}")
            return 0.0

    async def get_ticker(self, symbol=SYMBOL):
        """Получить тикер фьючерсов"""
        try:
            url = f"{self.base_url}/api/v1/contract/ticker"
            params = {'symbol': symbol}
            
            logger.info(f"Запрос цены фьючерсов для {symbol}...")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    result = await response.json()
                    
                    if result.get('success'):
                        price = float(result['data']['lastPrice'])
                        logger.info(f"Цена фьючерсов {symbol}: {price}")
                        return price
                    else:
                        logger.error(f"Ошибка цены фьючерсов: {result.get('message')}")
                        return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения цены фьючерсов: {e}")
            return 0.0

    async def place_order(self, symbol, side, order_type, quantity, price=None, position_side=1):
        """Разместить ордер на фьючерсы"""
        params = {
            'symbol': symbol,
            'positionType': position_side,
            'type': order_type,
            'quantity': str(quantity),
            'side': side,
        }
        
        if price is not None:
            params['price'] = str(price)
            
        logger.info(f"Размещение фьючерсного ордера: {params}")
        return await self._request('POST', '/api/v1/private/order/submit', params)

    async def place_market_order(self, symbol, side, quantity, position_side=1):
        """Разместить рыночный ордер на фьючерсы"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type=2,  # market
            quantity=quantity,
            position_side=position_side
        )

class MEXCSpotAPI:
    def __init__(self):
        self.base_url = "https://api.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_API_SECRET
        logger.info("MEXC Spot API клиент инициализирован")
        
    def _sign(self, params):
        """Генерация подписи для спота"""
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def get_spot_balance(self):
        """Получить баланс спотового счета"""
        try:
            timestamp = str(int(time.time() * 1000))
            params = {
                'timestamp': timestamp,
                'recvWindow': '5000'
            }
            
            signature = self._sign(params)
            params['signature'] = signature
            
            url = f"{self.base_url}/api/v3/account"
            
            headers = {
                'X-MEXC-APIKEY': self.api_key
            }
            
            logger.info("Запрос спотового баланса...")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=10) as response:
                    result = await response.json()
                    logger.info(f"Spot баланс ответ: {result}")
                    
                    if 'balances' in result:
                        for balance in result['balances']:
                            asset = balance['asset']
                            free = float(balance['free'])
                            if asset == 'USDT' and free > 0:
                                logger.info(f"Найден спотовый баланс USDT: {free}")
                                return free
                    
                    logger.warning("USDT не найден на спотовом счете")
                    return 0.0
                    
        except Exception as e:
            logger.error(f"Ошибка получения спотового баланса: {e}")
            return 0.0

# Создаем клиенты API
mexc_futures = MEXCFuturesAPI()
mexc_spot = MEXCSpotAPI()

async def transfer_to_futures(amount=None):
    """Перевести средства с спота на фьючерсы"""
    try:
        logger.info("Попытка перевода средств на фьючерсный счет...")
        
        # Получаем спотовый баланс
        spot_balance = await mexc_spot.get_spot_balance()
        logger.info(f"Спотовый баланс USDT: {spot_balance}")
        
        if spot_balance <= 0:
            logger.error("Нет средств на спотовом счете для перевода")
            return False
            
        # Определяем сумму для перевода
        transfer_amount = amount or spot_balance
        if transfer_amount > spot_balance:
            transfer_amount = spot_balance
            
        logger.info(f"Попытка перевода {transfer_amount} USDT на фьючерсный счет")
        
        # Здесь должен быть код для перевода средств
        # MEXC API для перевода требует отдельного endpoint
        
        # Временно просто сообщим о необходимости ручного перевода
        msg = f"""⚠️ НУЖЕН РУЧНОЙ ПЕРЕВОД!

На спотовом счете: {spot_balance:.2f} USDT
На фьючерсном счете: 0 USDT

Переведите средства вручную:
1. Откройте MEXC
2. Перейдите в "Futures"
3. Нажмите "Transfer"
4. Переведите USDT с Spot на Futures
5. Минимум: 5 USDT"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        return False
        
    except Exception as e:
        logger.error(f"Ошибка перевода на фьючерсы: {e}")
        return False

async def check_api_connection():
    """Проверить подключение к API и балансы"""
    try:
        logger.info("🔍 ЗАПУСК ПОЛНОЙ ДИАГНОСТИКИ...")
        
        # Проверяем фьючерсный баланс
        futures_balance = await mexc_futures.get_balance()
        logger.info(f"Фьючерсный баланс USDT: {futures_balance:.2f}")
        
        # Проверяем спотовый баланс
        spot_balance = await mexc_spot.get_spot_balance()
        logger.info(f"Спотовый баланс USDT: {spot_balance:.2f}")
        
        # Проверяем цену
        price = await mexc_futures.get_ticker()
        logger.info(f"Цена фьючерсов {SYMBOL}: {price:.4f}")
        
        diagnostics = f"""
🔍 ДИАГНОСТИКА СИСТЕМЫ:

💰 БАЛАНСЫ:
• Фьючерсный счет: {futures_balance:.2f} USDT
• Спотовый счет: {spot_balance:.2f} USDT

📊 ТОРГОВЛЯ:
• Символ: {SYMBOL}
• Цена: ${price:.4f}
• Риск: {RISK_PERCENT}%
• Плечо: {LEVERAGE}x

🔑 API:
• API Key: {'✅' if MEXC_API_KEY else '❌'}
• Secret Key: {'✅' if MEXC_API_SECRET else '❌'}
"""
        
        logger.info(diagnostics)
        
        # Отправляем диагностику в Telegram
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=diagnostics)
        
        # Если на фьючерсном счете нет средств, но есть на спотовом
        if futures_balance <= 0 and spot_balance > 0:
            await transfer_to_futures()
            
        return futures_balance > 0
        
    except Exception as e:
        error_msg = f"❌ Ошибка диагностики: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        return False

async def calculate_quantity(usd_amount, symbol=SYMBOL):
    """Рассчитать количество для ордера"""
    try:
        logger.info(f"Расчет количества для {usd_amount} USDT")
        
        price = await mexc_futures.get_ticker(symbol)
        if price <= 0:
            raise ValueError("Не удалось получить цену фьючерсов")
        
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
        
        # Проверяем подключение к API и балансы
        if not await check_api_connection():
            raise ValueError("Проблемы с подключением к API или нет средств")
        
        # Получаем фьючерсный баланс
        balance = await mexc_futures.get_balance()
        logger.info(f"Фьючерсный баланс: {balance} USDT")
        
        if balance <= 5:
            raise ValueError(f"Недостаточно средств на фьючерсном счете: {balance} USDT. Минимум 5 USDT требуется.")
        
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
        order_result = await mexc_futures.place_market_order(
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
        entry_price = await mexc_futures.get_ticker(SYMBOL)
        
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
        
        # Запускаем полную диагностику
        await check_api_connection()
        
        futures_balance = await mexc_futures.get_balance()
        price = await mexc_futures.get_ticker()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

📊 СТАТУС:
• Символ: {SYMBOL}
• Риск: {RISK_PERCENT}%
• Плечо: {LEVERAGE}x
• Фьючерсный баланс: {futures_balance:.2f} USDT
• Цена: ${price:.4f}

💡 ДЕЙСТВИЯ:
{f"✅ Готов к работе! Отправьте webhook сигнал." if futures_balance > 5 else "⚠️ Переведите USDT на фьючерсный счет!"}"""
        
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
    
    futures_balance = await mexc_futures.get_balance()
    spot_balance = await mexc_spot.get_spot_balance()
    price = await mexc_futures.get_ticker()
    
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
                <h3>💰 БАЛАНСЫ</h3>
                <p><b>Фьючерсный счет:</b> <span class="{'success' if futures_balance > 0 else 'error'}">{futures_balance:.2f} USDT</span></p>
                <p><b>Спотовый счет:</b> <span class="{'success' if spot_balance > 0 else 'warning'}">{spot_balance:.2f} USDT</span></p>
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
    futures_balance = await mexc_futures.get_balance()
    spot_balance = await mexc_spot.get_spot_balance()
    
    return {
        "futures_balance": futures_balance,
        "spot_balance": spot_balance,
        "currency": "USDT"
    }

@app.get("/diagnostics")
async def diagnostics():
    """Страница диагностики"""
    logger.info("Запрос страницы диагностики")
    
    futures_balance = await mexc_futures.get_balance()
    spot_balance = await mexc_spot.get_spot_balance()
    price = await mexc_futures.get_ticker()
    
    html = f"""
    <html>
        <head><title>Диагностика</title></head>
        <body style="font-family: Arial; background: #1e1e1e; color: white; padding: 20px;">
            <h1>🔧 Диагностика системы</h1>
            <div style="background: #2d2d2d; padding: 20px; border-radius: 10px;">
                <h3>💰 БАЛАНСЫ</h3>
                <p><b>Фьючерсный счет:</b> {futures_balance:.2f} USDT</p>
                <p><b>Спотовый счет:</b> {spot_balance:.2f} USDT</p>
                
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
