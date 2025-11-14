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
SYMBOL = "XRP_USDT"
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

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
        
    def _sign(self, params):
        """Генерация подписи"""
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
            
            async with aiohttp.ClientSession() as session:
                if method == 'GET':
                    async with session.get(url, params=all_params, timeout=10) as response:
                        result = await response.json()
                else:
                    async with session.post(url, data=all_params, timeout=10) as response:
                        result = await response.json()
                
                logger.info(f"MEXC API {method} {endpoint}: {result}")
                return result
                
        except Exception as e:
            logger.error(f"Ошибка MEXC API {endpoint}: {e}")
            return None

    async def get_account_assets(self):
        """Получить информацию о аккаунте"""
        return await self._request('GET', '/api/v1/private/account/assets')

    async def get_balance(self):
        """Получить баланс USDT с детальной диагностикой"""
        try:
            result = await self.get_account_assets()
            logger.info(f"Полный ответ баланса: {result}")
            
            if not result:
                raise ValueError("Нет ответа от API")
                
            if not result.get('success'):
                error_msg = result.get('message', 'Unknown error')
                raise ValueError(f"API Error: {error_msg}")
            
            data = result.get('data', [])
            logger.info(f"Данные баланса: {data}")
            
            for asset in data:
                currency = asset.get('currency')
                available = asset.get('availableBalance')
                logger.info(f"Актив: {currency}, доступно: {available}")
                
                if currency == 'USDT':
                    balance = float(available or 0)
                    logger.info(f"Найден баланс USDT: {balance}")
                    return balance
            
            # Если USDT не найден, покажем все доступные валюты
            available_currencies = [f"{a.get('currency')}: {a.get('availableBalance')}" for a in data]
            logger.warning(f"USDT не найден. Доступные валюты: {available_currencies}")
            
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ USDT не найден на фьючерсном счете. Доступные валюты: {available_currencies}"
            )
            
            return 0.0
            
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"❌ Ошибка получения баланса: {str(e)}"
            )
            return 0.0

    async def get_ticker(self, symbol=SYMBOL):
        """Получить тикер"""
        try:
            url = f"{self.base_url}/api/v1/contract/ticker"
            params = {'symbol': symbol}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    result = await response.json()
                    logger.info(f"Ticker response: {result}")
                    
                    if result.get('success'):
                        price = float(result['data']['lastPrice'])
                        logger.info(f"Цена {symbol}: {price}")
                        return price
                    else:
                        raise Exception(f"Ticker error: {result.get('message')}")
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

    async def get_positions(self, symbol=SYMBOL):
        """Получить открытые позиции"""
        params = {'symbol': symbol}
        return await self._request('GET', '/api/v1/private/position/list', params)

    async def close_all_positions(self, symbol=SYMBOL):
        """Закрыть все позиции"""
        try:
            result = await self.get_positions(symbol)
            logger.info(f"Позиции: {result}")
            
            if result and result.get('success'):
                positions = result.get('data', [])
                
                for position in positions:
                    position_amt = float(position.get('position', 0))
                    if position_amt != 0:
                        position_side = position.get('positionType')
                        
                        if position_side == 1:  # long
                            close_side = 3  # close long
                        else:  # short
                            close_side = 4  # close short
                        
                        close_result = await self.place_market_order(
                            symbol=symbol,
                            side=close_side,
                            quantity=abs(position_amt),
                            position_side=position_side
                        )
                        
                        logger.info(f"Результат закрытия: {close_result}")
                        return True
                        
            return False
            
        except Exception as e:
            logger.error(f"Ошибка закрытия позиций: {e}")
            return False

    async def set_leverage(self, symbol, leverage, open_type=1, position_type=1):
        """Установить плечо"""
        params = {
            'symbol': symbol,
            'leverage': leverage,
            'openType': open_type,
            'positionType': position_type
        }
        return await self._request('POST', '/api/v1/private/position/change_margin', params)

# Создаем клиент API
mexc_api = MEXCFuturesAPI()

async def check_api_connection():
    """Проверить подключение к API"""
    try:
        # Проверяем баланс
        balance = await mexc_api.get_balance()
        
        # Проверяем цену
        price = await mexc_api.get_ticker()
        
        # Проверяем доступные символы
        url = f"{mexc_api.base_url}/api/v1/contract/detail"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={'symbol': SYMBOL}) as response:
                symbol_info = await response.json()
        
        diagnostics = f"""
🔍 ДИАГНОСТИКА API:

✅ Баланс USDT: {balance:.2f}
✅ Цена {SYMBOL}: {price:.4f}
✅ Символ {SYMBOL}: {symbol_info.get('success', False)}
✅ API Key: {len(MEXC_API_KEY) > 0}
"""
        
        logger.info(diagnostics)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=diagnostics)
        
        return balance > 0
        
    except Exception as e:
        error_msg = f"❌ Ошибка диагностики API: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        return False

async def calculate_quantity(usd_amount, symbol=SYMBOL):
    """Рассчитать количество для ордера"""
    try:
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
        logger.info(f"=== ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()} ===")
        
        # Проверяем подключение к API
        if not await check_api_connection():
            raise ValueError("Проблемы с подключением к API")
        
        # Получаем баланс
        balance = await mexc_api.get_balance()
        logger.info(f"Текущий баланс: {balance} USDT")
        
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
        
        # Закрываем существующие позиции
        await mexc_api.close_all_positions()
        await asyncio.sleep(1)
        
        # Устанавливаем плечо
        position_type = 1 if signal == 'buy' else 2
        leverage_result = await mexc_api.set_leverage(SYMBOL, LEVERAGE, 1, position_type)
        logger.info(f"Результат установки плеча: {leverage_result}")
        
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
            'order_id': order_result.get('data', {}).get('orderId'),
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
        logger.info("Позиция успешно открыта")
        
    except Exception as e:
        error_msg = f"❌ Ошибка открытия позиции: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        active_position = False

# === FastAPI Routes ===
@app.on_event("startup")
async def startup():
    try:
        logger.info("=== ЗАПУСК MEXC БОТА ===")
        
        # Запускаем диагностику
        await check_api_connection()
        
        balance = await mexc_api.get_balance()
        price = await mexc_api.get_ticker()
        
        msg = f"""✅ MEXC Futures Bot запущен!

Символ: {SYMBOL}
Риск: {RISK_PERCENT}%
Плечо: {LEVERAGE}x
Баланс: {balance:.2f} USDT
Цена {SYMBOL}: ${price:.4f}

Для торговли отправьте webhook сигнал."""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Бот успешно запущен")
        
    except Exception as e:
        error_msg = f"❌ Ошибка запуска: {e}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)

@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        amount = data.get("amount")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        asyncio.create_task(open_position(signal, amount))
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def home():
    global last_trade_info, active_position
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
                .error {{ color: #e74c3c; }}
            </style>
        </head>
        <body>
            <h1 class="success">🤖 MEXC Futures Trading Bot</h1>
            
            <div class="card">
                <h3>📊 Статус</h3>
                <p><b>Символ:</b> {SYMBOL}</p>
                <p><b>Позиция:</b> <span class="{'success' if active_position else 'error'}">{status}</span></p>
                <p><b>Риск:</b> {RISK_PERCENT}%</p>
                <p><b>Плечо:</b> {LEVERAGE}x</p>
            </div>
            
            <div class="card">
                <h3>📈 Последняя сделка</h3>
                <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
            </div>
            
            <div class="card">
                <h3>🔧 Действия</h3>
                <p><a href="/diagnostics">Запустить диагностику</a></p>
                <p><a href="/balance">Проверить баланс</a></p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)

@app.get("/balance")
async def get_balance():
    balance = await mexc_api.get_balance()
    return {"balance": balance, "currency": "USDT"}

@app.get("/diagnostics")
async def diagnostics():
    """Страница диагностики"""
    balance = await mexc_api.get_balance()
    price = await mexc_api.get_ticker()
    
    html = f"""
    <html>
        <head><title>Диагностика</title></head>
        <body style="font-family: Arial; background: #1e1e1e; color: white; padding: 20px;">
            <h1>🔧 Диагностика системы</h1>
            <div style="background: #2d2d2d; padding: 20px; border-radius: 10px;">
                <h3>📊 Статус API</h3>
                <p><b>Баланс USDT:</b> {balance:.2f}</p>
                <p><b>Цена {SYMBOL}:</b> ${price:.4f}</p>
                <p><b>API Key:</b> {'✅ Установлен' if MEXC_API_KEY else '❌ Отсутствует'}</p>
                <p><b>Секретный ключ:</b> {'✅ Установлен' if MEXC_API_SECRET else '❌ Отсутствует'}</p>
            </div>
            <br>
            <a href="/">← Назад</a>
        </body>
    </html>
    """
    return HTMLResponse(html)

@app.post("/close")
async def close_positions():
    result = await mexc_api.close_all_positions()
    if result:
        global active_position
        active_position = False
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ Все позиции закрыты")
        return {"status": "ok", "message": "Positions closed"}
    else:
        return {"status": "error", "message": "No positions to close"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
