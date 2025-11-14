import os
import json
import asyncio
import logging
import hmac
import hashlib
import time
import aiohttp
import urllib.parse
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
SYMBOL = "XRP_USDT"  # Формат для фьючерсов MEXC
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === MEXC Futures API (на основе официальной документации) ===
class MEXCFuturesAPI:
    def __init__(self):
        self.base_url = "https://contract.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_API_SECRET
        logger.info("MEXC Futures API клиент инициализирован")
        
    def _generate_signature(self, params):
        """Генерация подписи согласно документации MEXC"""
        # Сортируем параметры по ключу в алфавитном порядке
        sorted_params = sorted(params.items())
        # Создаем строку запроса
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        # Генерируем подпись
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _make_request(self, endpoint, params=None, method='GET'):
        """Универсальный метод запроса"""
        try:
            # Обязательные параметры для каждого запроса
            timestamp = str(int(time.time() * 1000))
            base_params = {
                'api_key': self.api_key,
                'req_time': timestamp,
            }
            
            # Добавляем пользовательские параметры
            if params:
                base_params.update(params)
            
            # Генерируем подпись
            signature = self._generate_signature(base_params)
            base_params['sign'] = signature
            
            url = f"{self.base_url}{endpoint}"
            
            logger.info(f"MEXC API Request: {method} {endpoint}")
            
            async with aiohttp.ClientSession() as session:
                if method == 'GET':
                    async with session.get(url, params=base_params, timeout=10) as response:
                        result = await response.json()
                else:
                    # Для POST используем form data
                    async with session.post(url, data=base_params, timeout=10) as response:
                        result = await response.json()
                
                logger.info(f"MEXC API Response: {result}")
                return result
                
        except Exception as e:
            logger.error(f"Ошибка MEXC API: {e}")
            return None

    # === ACCOUNT ENDPOINTS ===
    async def get_account_info(self):
        """Получить информацию об аккаунте"""
        return await self._make_request('/api/v1/private/account/assets')

    async def get_balance(self):
        """Получить баланс USDT"""
        try:
            result = await self.get_account_info()
            if result and result.get('success'):
                for asset in result.get('data', []):
                    if asset.get('currency') == 'USDT':
                        balance = float(asset.get('availableBalance', 0))
                        logger.info(f"Баланс USDT: {balance}")
                        return balance
            return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return 0.0

    # === MARKET ENDPOINTS ===
    async def get_ticker(self, symbol=SYMBOL):
        """Получить текущую цену"""
        try:
            url = f"{self.base_url}/api/v1/contract/ticker"
            params = {'symbol': symbol}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    result = await response.json()
                    if result.get('success'):
                        return float(result['data']['lastPrice'])
                    return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения цены: {e}")
            return 0.0

    async def get_contract_info(self, symbol=SYMBOL):
        """Получить информацию о контракте"""
        try:
            url = f"{self.base_url}/api/v1/contract/detail"
            params = {'symbol': symbol}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    return await response.json()
        except Exception as e:
            logger.error(f"Ошибка получения информации о контракте: {e}")
            return None

    # === TRADE ENDPOINTS ===
    async def place_order(self, symbol, side, order_type, quantity, price=None, **kwargs):
        """
        Разместить ордер
        side: 1=open long, 2=open short, 3=close long, 4=close short
        order_type: 1=limit, 2=market
        """
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': str(quantity),
        }
        
        # Обязательные параметры для фьючерсов
        params['positionType'] = kwargs.get('positionType', 1)  # 1=long, 2=short
        
        if price is not None:
            params['price'] = str(price)
            
        if kwargs.get('reduceOnly'):
            params['reduceOnly'] = True
            
        return await self._make_request('/api/v1/private/order/submit', params, 'POST')

    async def place_market_order(self, symbol, side, quantity, position_type=1):
        """Разместить рыночный ордер"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type=2,  # market
            quantity=quantity,
            positionType=position_type
        )

    # === POSITION ENDPOINTS ===
    async def get_positions(self, symbol=SYMBOL):
        """Получить открытые позиции"""
        params = {'symbol': symbol}
        return await self._make_request('/api/v1/private/position/list', params)

    async def set_leverage(self, symbol, leverage, open_type=1, position_type=1):
        """Установить плечо"""
        params = {
            'symbol': symbol,
            'leverage': leverage,
            'openType': open_type,  # 1=isolated, 2=cross
            'positionType': position_type  # 1=long, 2=short
        }
        return await self._make_request('/api/v1/private/position/change_margin', params, 'POST')

# Создаем клиент API
mexc_api = MEXCFuturesAPI()

async def test_connection():
    """Тестирование подключения к API"""
    try:
        logger.info("🔧 ТЕСТИРОВАНИЕ ПОДКЛЮЧЕНИЯ...")
        
        # Тест 1: Баланс
        balance = await mexc_api.get_balance()
        logger.info(f"Баланс: {balance}")
        
        # Тест 2: Цена
        price = await mexc_api.get_ticker()
        logger.info(f"Цена: {price}")
        
        # Тест 3: Информация о контракте
        contract_info = await mexc_api.get_contract_info()
        logger.info(f"Контракт: {contract_info}")
        
        return balance > 0
    except Exception as e:
        logger.error(f"Ошибка тестирования: {e}")
        return False

async def calculate_quantity(usd_amount):
    """Рассчитать количество для ордера"""
    try:
        price = await mexc_api.get_ticker()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        # Получаем информацию о контракте для точного расчета
        contract_info = await mexc_api.get_contract_info()
        if contract_info and contract_info.get('success'):
            min_qty = float(contract_info['data'].get('minOrderQuantity', 1))
            quantity = usd_amount / price
            
            if quantity < min_qty:
                quantity = min_qty
                
            logger.info(f"Рассчитано количество: {quantity}")
            return quantity
        else:
            # Упрощенный расчет
            quantity = usd_amount / price
            quantity = round(quantity, 1)
            if quantity < 1:
                quantity = 1.0
            logger.info(f"Рассчитано количество (упрощенно): {quantity}")
            return quantity
            
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0

async def open_position(signal, amount_usd=None):
    """Открыть позицию"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"🚀 ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()}")
        
        # Тестируем подключение
        if not await test_connection():
            raise ValueError("Проблемы с подключением к API")
        
        # Получаем баланс
        balance = await mexc_api.get_balance()
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance} USDT")
        
        # Рассчитываем сумму
        usd_amount = amount_usd or (balance * RISK_PERCENT / 100)
        if usd_amount < 5:
            usd_amount = 5
            
        logger.info(f"Сумма для торговли: {usd_amount:.2f} USDT")
        
        # Рассчитываем количество
        quantity = await calculate_quantity(usd_amount)
        if quantity <= 0:
            raise ValueError("Неверное количество")
        
        logger.info(f"Количество: {quantity}")
        
        # Определяем параметры
        if signal == 'buy':
            order_side = 1  # open long
            position_type = 1  # long
        else:
            order_side = 2  # open short
            position_type = 2  # short
        
        # Устанавливаем плечо
        leverage_result = await mexc_api.set_leverage(SYMBOL, LEVERAGE, 1, position_type)
        logger.info(f"Плечо установлено: {leverage_result}")
        
        # Размещаем ордер
        order_result = await mexc_api.place_market_order(
            symbol=SYMBOL,
            side=order_side,
            quantity=quantity,
            position_type=position_type
        )
        
        logger.info(f"Результат ордера: {order_result}")
        
        if not order_result or not order_result.get('success'):
            error_msg = order_result.get('message', 'Unknown error') if order_result else 'No response'
            raise ValueError(f"Ошибка ордера: {error_msg}")
        
        # Получаем цену входа
        entry_price = await mexc_api.get_ticker()
        
        # Сохраняем информацию
        active_position = True
        last_trade_info = {
            'signal': signal,
            'side': 'LONG' if signal == 'buy' else 'SHORT',
            'quantity': quantity,
            'entry_price': entry_price,
            'amount': usd_amount,
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
        logger.info("🚀 ЗАПУСК БОТА")
        
        await test_connection()
        
        balance = await mexc_api.get_balance()
        price = await mexc_api.get_ticker()
        
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
        
        asyncio.create_task(open_position(signal))
        
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
