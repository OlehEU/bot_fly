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

# === MEXC API Client (на основе официального демо) ===
class MEXCFuturesAPI:
    def __init__(self):
        self.base_url = "https://contract.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_API_SECRET
        logger.info("MEXC Futures API клиент инициализирован")
        
    def _sign(self, params):
        """Генерация подписи как в официальном демо"""
        # Сортируем параметры по ключу
        sorted_params = sorted(params.items())
        # Создаем строку для подписи (как в демо)
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
            
            # Базовые параметры как в демо
            all_params = {
                'api_key': self.api_key,
                'req_time': timestamp,
            }
            
            # Добавляем пользовательские параметры
            if params:
                all_params.update(params)
            
            # Генерируем подпись
            signature = self._sign(all_params)
            all_params['sign'] = signature
            
            url = f"{self.base_url}{endpoint}"
            
            logger.info(f"MEXC API {method} {endpoint}")
            logger.info(f"Params: {all_params}")
            
            async with aiohttp.ClientSession() as session:
                if method == 'GET':
                    async with session.get(url, params=all_params, timeout=10) as response:
                        result = await response.json()
                else:
                    # Для POST используем data (form-encoded)
                    async with session.post(url, data=all_params, timeout=10) as response:
                        result = await response.json()
                
                logger.info(f"MEXC API Response: {result}")
                return result
                
        except Exception as e:
            logger.error(f"Ошибка MEXC API {endpoint}: {e}")
            return None

    # === ACCOUNT METHODS ===
    async def get_account_assets(self):
        """Получить информацию о аккаунте (как в демо)"""
        return await self._request('GET', '/api/v1/private/account/assets')

    async def get_balance(self):
        """Получить баланс USDT"""
        try:
            result = await self.get_account_assets()
            
            if not result or not result.get('success'):
                error_msg = result.get('message', 'Unknown error') if result else 'No response'
                logger.error(f"API Error: {error_msg}")
                return 0.0
            
            data = result.get('data', [])
            logger.info(f"Данные баланса: {json.dumps(data, indent=2)}")
            
            for asset in data:
                if asset.get('currency') == 'USDT':
                    balance = float(asset.get('availableBalance', 0))
                    logger.info(f"Баланс USDT: {balance}")
                    return balance
            
            return 0.0
            
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return 0.0

    # === MARKET METHODS ===
    async def get_ticker(self, symbol=SYMBOL):
        """Получить тикер"""
        try:
            url = f"{self.base_url}/api/v1/contract/ticker"
            params = {'symbol': symbol}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    result = await response.json()
                    if result.get('success'):
                        return float(result['data']['lastPrice'])
                    else:
                        return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения цены: {e}")
            return 0.0

    async def get_contract_detail(self, symbol=SYMBOL):
        """Получить информацию о контракте"""
        try:
            url = f"{self.base_url}/api/v1/contract/detail"
            params = {'symbol': symbol}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    result = await response.json()
                    return result
        except Exception as e:
            logger.error(f"Ошибка получения информации о контракте: {e}")
            return None

    # === ORDER METHODS ===
    async def place_order(self, symbol, side, order_type, quantity, price=None, position_side=1):
        """
        Разместить ордер
        side: 1=open long, 2=open short, 3=close long, 4=close short
        order_type: 1=limit, 2=market
        position_side: 1=long, 2=short
        """
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

    async def place_limit_order(self, symbol, side, quantity, price, position_side=1, reduce_only=False):
        """Разместить лимитный ордер"""
        params = {
            'symbol': symbol,
            'positionType': position_side,
            'type': 1,  # limit
            'quantity': str(quantity),
            'price': str(price),
            'side': side,
        }
        
        if reduce_only:
            params['reduceOnly'] = True
            
        return await self._request('POST', '/api/v1/private/order/submit', params)

    # === POSITION METHODS ===
    async def get_positions(self, symbol=SYMBOL):
        """Получить открытые позиции"""
        params = {'symbol': symbol}
        return await self._request('GET', '/api/v1/private/position/list', params)

    async def close_all_positions(self, symbol=SYMBOL):
        """Закрыть все позиции"""
        try:
            result = await self.get_positions(symbol)
            
            if result and result.get('success'):
                positions = result.get('data', [])
                
                for position in positions:
                    position_amt = float(position.get('position', 0))
                    if position_amt != 0:
                        position_side = position.get('positionType')
                        
                        # Определяем сторону для закрытия
                        if position_side == 1:  # long
                            close_side = 3  # close long
                        else:  # short
                            close_side = 4  # close short
                        
                        # Закрываем позицию
                        close_result = await self.place_market_order(
                            symbol=symbol,
                            side=close_side,
                            quantity=abs(position_amt),
                            position_side=position_side
                        )
                        
                        logger.info(f"Закрыта позиция: {close_result}")
                        return True
                        
            return False
            
        except Exception as e:
            logger.error(f"Ошибка закрытия позиций: {e}")
            return False

    # === LEVERAGE METHODS ===
    async def set_leverage(self, symbol, leverage, open_type=1, position_type=1):
        """Установить плечо"""
        params = {
            'symbol': symbol,
            'leverage': leverage,
            'openType': open_type,  # 1=isolated, 2=cross
            'positionType': position_type  # 1=long, 2=short
        }
        return await self._request('POST', '/api/v1/private/position/change_margin', params)

# Создаем клиент API
mexc_api = MEXCFuturesAPI()

async def calculate_quantity(usd_amount, symbol=SYMBOL):
    """Рассчитать количество для ордера"""
    try:
        price = await mexc_api.get_ticker(symbol)
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        # Получаем информацию о контракте для точного расчета
        contract_info = await mexc_api.get_contract_detail(symbol)
        if contract_info and contract_info.get('success'):
            min_qty = float(contract_info['data'].get('minOrderQuantity', 1))
            quantity_precision = int(contract_info['data'].get('quantityPrecision', 1))
            
            quantity = usd_amount / price
            quantity = round(quantity, quantity_precision)
            
            if quantity < min_qty:
                quantity = min_qty
                
            logger.info(f"Рассчитано количество: {quantity} (min: {min_qty}, precision: {quantity_precision})")
            return quantity
        else:
            # Простой расчет если не удалось получить информацию о контракте
            quantity = usd_amount / price
            quantity = round(quantity, 1)
            if quantity < 1:
                quantity = 1.0
            logger.info(f"Рассчитано количество (упрощенно): {quantity}")
            return quantity
        
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0

async def check_api_connection():
    """Проверить подключение к API"""
    try:
        logger.info("🔍 ПРОВЕРКА ПОДКЛЮЧЕНИЯ К API...")
        
        balance = await mexc_api.get_balance()
        price = await mexc_api.get_ticker()
        contract_info = await mexc_api.get_contract_detail()
        
        diagnostics = f"""
🔍 ДИАГНОСТИКА API:

💰 Баланс: {balance:.2f} USDT
📊 Символ: {SYMBOL}
💰 Цена: ${price:.4f}
✅ Контракт: {'Доступен' if contract_info and contract_info.get('success') else 'Ошибка'}

💡 СТАТУС: {'✅ ГОТОВ К ТОРГОВЛЕ' if balance > 5 else '⚠️ МАЛО СРЕДСТВ'}
"""
        
        logger.info(diagnostics)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=diagnostics)
        
        return balance > 5
        
    except Exception as e:
        error_msg = f"❌ Ошибка диагностики API: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        return False

async def open_position(signal, amount_usd=None):
    """Открыть позицию с правильными параметрами MEXC"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"=== ОТКРЫТИЕ ПОЗИЦИИ {signal.upper()} ===")
        
        if not await check_api_connection():
            raise ValueError("Проблемы с подключением к API или нет средств")
        
        balance = await mexc_api.get_balance()
        logger.info(f"Баланс: {balance} USDT")
        
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance} USDT")
        
        usd_amount = amount_usd or (balance * RISK_PERCENT / 100)
        if usd_amount < 5:
            usd_amount = 5
            
        logger.info(f"Сумма для торговли: {usd_amount} USDT")
        
        quantity = await calculate_quantity(usd_amount)
        if quantity <= 0:
            raise ValueError("Неверное количество")
        
        logger.info(f"Количество для ордера: {quantity}")
        
        # Закрываем существующие позиции
        await mexc_api.close_all_positions()
        await asyncio.sleep(1)
        
        # Устанавливаем плечо
        position_type = 1 if signal == 'buy' else 2
        await mexc_api.set_leverage(SYMBOL, LEVERAGE, 1, position_type)
        
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
        
        # Рассчитываем TP/SL
        if signal == 'buy':
            tp_price = round(entry_price * 1.01, 6)  # +1%
            sl_price = round(entry_price * 0.99, 6)  # -1%
            tp_side = 3  # close long
            sl_side = 3  # close long
        else:
            tp_price = round(entry_price * 0.99, 6)  # -1%
            sl_price = round(entry_price * 1.01, 6)  # +1%
            tp_side = 4  # close short
            sl_side = 4  # close short
        
        # Размещаем TP ордер
        try:
            await mexc_api.place_limit_order(
                symbol=SYMBOL,
                side=tp_side,
                quantity=quantity,
                price=tp_price,
                position_side=position_side,
                reduce_only=True
            )
            logger.info(f"TP ордер размещен: {tp_price}")
        except Exception as e:
            logger.warning(f"Не удалось разместить TP: {e}")
        
        # Размещаем SL ордер
        try:
            await mexc_api.place_limit_order(
                symbol=SYMBOL,
                side=sl_side,
                quantity=quantity,
                price=sl_price,
                position_side=position_side,
                reduce_only=True
            )
            logger.info(f"SL ордер размещен: {sl_price}")
        except Exception as e:
            logger.warning(f"Не удалось разместить SL: {e}")
        
        # Сохраняем информацию о сделке
        active_position = True
        last_trade_info = {
            'signal': signal,
            'side': 'LONG' if signal == 'buy' else 'SHORT',
            'quantity': quantity,
            'entry_price': entry_price,
            'tp_price': tp_price,
            'sl_price': sl_price,
            'order_id': order_result.get('data', {}).get('orderId'),
            'timestamp': time.time()
        }
        
        # Отправляем уведомление
        msg = f"""✅ ПОЗИЦИЯ ОТКРЫТА
Символ: {SYMBOL}
Сторона: {'LONG' if signal == 'buy' else 'SHORT'}
Количество: {quantity}
Цена входа: ${entry_price:.4f}
TP: ${tp_price:.4f} | SL: ${sl_price:.4f}
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
        await asyncio.sleep(2)
        
        await check_api_connection()
        
        balance = await mexc_api.get_balance()
        price = await mexc_api.get_ticker()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

📊 СТАТУС:
• Символ: {SYMBOL}
• Цена: ${price:.4f}
• Баланс: {balance:.2f} USDT
• Плечо: {LEVERAGE}x
• Риск: {RISK_PERCENT}%

💡 Отправьте webhook сигнал для торговли."""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")
        
    except Exception as e:
        error_msg = f"❌ Ошибка при старте: {e}"
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
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        amount = data.get("amount")
        
        logger.info(f"Webhook данные: signal={signal}, amount={amount}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
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
                <p><b>USDT:</b> <span class="{'success' if balance > 0 else 'error'}">{balance:.2f} USDT</span></p>
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
        </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
