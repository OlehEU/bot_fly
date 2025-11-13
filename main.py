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
SYMBOL = "XRPUSDT"  # Просто используем XRPUSDT
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

# === MEXC API функции ===
class MEXCClient:
    def __init__(self):
        self.base_url = "https://contract.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_API_SECRET
        
    def _generate_signature(self, params):
        sorted_params = sorted(params.items())
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _make_request(self, endpoint, params=None, method='GET'):
        try:
            timestamp = str(int(time.time() * 1000))
            all_params = {
                'api_key': self.api_key,
                'req_time': timestamp,
                **(params or {})
            }
            
            signature = self._generate_signature(all_params)
            all_params['sign'] = signature
            
            url = f"{self.base_url}{endpoint}"
            
            async with aiohttp.ClientSession() as session:
                if method == 'GET':
                    async with session.get(url, params=all_params) as response:
                        result = await response.json()
                else:
                    async with session.post(url, data=all_params) as response:
                        result = await response.json()
                
                logger.info(f"MEXC API {endpoint}: {result}")
                return result
                
        except Exception as e:
            logger.error(f"Ошибка MEXC API {endpoint}: {e}")
            return None

    async def get_balance(self):
        """Получить баланс USDT"""
        result = await self._make_request('/api/v1/private/account/assets')
        if result and result.get('success'):
            for asset in result.get('data', []):
                if asset.get('currency') == 'USDT':
                    return float(asset.get('availableBalance', 0))
        return 0.0

    async def get_price(self):
        """Получить текущую цену"""
        try:
            url = f"https://contract.mexc.com/api/v1/contract/ticker?symbol={SYMBOL}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    result = await response.json()
                    if result and result.get('success'):
                        return float(result['data']['lastPrice'])
            return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения цены: {e}")
            return 0.0

    async def set_leverage(self, leverage):
        """Установить плечо"""
        params = {
            'symbol': SYMBOL,
            'leverage': leverage,
            'openType': 1,  # isolated
            'positionType': 1  # long
        }
        return await self._make_request('/api/v1/private/position/change_margin', params, 'POST')

    async def place_order(self, side, quantity, price=None, order_type='MARKET', reduce_only=False):
        """Разместить ордер"""
        params = {
            'symbol': SYMBOL,
            'positionType': 1 if side == 'BUY' else 2,  # 1=long, 2=short
            'type': order_type,
            'quantity': str(quantity),
            'side': 1 if side == 'BUY' else 2,  # 1=buy, 2=sell
        }
        
        if price:
            params['price'] = str(price)
        if reduce_only:
            params['reduceOnly'] = True
            
        return await self._make_request('/api/v1/private/order/submit', params, 'POST')

    async def close_all_positions(self):
        """Закрыть все позиции"""
        # Сначала получаем открытые позиции
        params = {'symbol': SYMBOL}
        result = await self._make_request('/api/v1/private/position/list', params)
        
        if result and result.get('success'):
            for position in result.get('data', []):
                if float(position.get('position', 0)) > 0:
                    side = 'SELL' if position.get('positionType') == 1 else 'BUY'
                    quantity = abs(float(position.get('position', 0)))
                    
                    # Закрываем позицию
                    close_result = await self.place_order(
                        side=side,
                        quantity=quantity,
                        order_type='MARKET',
                        reduce_only=True
                    )
                    logger.info(f"Закрыта позиция: {close_result}")
                    return True
        return False

# Создаем клиент MEXC
mexc_client = MEXCClient()

# === Основные функции ===
async def calculate_quantity(usd_amount):
    """Простой расчет количества"""
    try:
        price = await mexc_client.get_price()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
            
        # Базовая расчет количества
        quantity = usd_amount / price
        
        # Округляем до 1 decimal (для XRP обычно достаточно)
        quantity = round(quantity, 1)
        
        # Минимальная проверка
        if quantity < 1:  # Минимум 1 XRP
            quantity = 1.0
            
        logger.info(f"Рассчитано количество: {quantity} XRP за {usd_amount} USDT по цене {price}")
        return quantity
        
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        return 0.0

async def open_simple_position(signal, amount_usd=None):
    """Простое открытие позиции"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"=== ОТКРЫТИЕ ПОЗИЦИИ {signal} ===")
        
        # Получаем баланс
        balance = await mexc_client.get_balance()
        logger.info(f"Баланс: {balance} USDT")
        
        if balance <= 5:
            raise ValueError(f"Недостаточно средств: {balance} USDT")
        
        # Определяем сумму для торговли
        usd_amount = amount_usd or (balance * RISK_PERCENT / 100)
        if usd_amount < 5:
            usd_amount = 5  # Минимум 5 USDT
            
        logger.info(f"Сумма для торговли: {usd_amount} USDT")
        
        # Рассчитываем количество
        quantity = await calculate_quantity(usd_amount)
        if quantity <= 0:
            raise ValueError("Неверное количество")
            
        logger.info(f"Количество для ордера: {quantity}")
        
        # Устанавливаем плечо
        await mexc_client.set_leverage(LEVERAGE)
        
        # Закрываем существующие позиции
        await mexc_client.close_all_positions()
        await asyncio.sleep(1)
        
        # Определяем сторону
        side = 'BUY' if signal.lower() == 'buy' else 'SELL'
        
        # Размещаем ордер
        order_result = await mexc_client.place_order(
            side=side,
            quantity=quantity,
            order_type='MARKET'
        )
        
        logger.info(f"Результат ордера: {order_result}")
        
        if order_result and order_result.get('success'):
            # Получаем цену исполнения
            entry_price = await mexc_client.get_price()
            
            # Рассчитываем TP/SL
            if side == 'BUY':
                tp_price = entry_price * 1.01  # +1%
                sl_price = entry_price * 0.99  # -1%
            else:
                tp_price = entry_price * 0.99  # -1%
                sl_price = entry_price * 1.01  # +1%
            
            # Размещаем TP ордер
            await mexc_client.place_order(
                side='SELL' if side == 'BUY' else 'BUY',
                quantity=quantity,
                price=tp_price,
                order_type='LIMIT',
                reduce_only=True
            )
            
            # Размещаем SL ордер  
            await mexc_client.place_order(
                side='SELL' if side == 'BUY' else 'BUY',
                quantity=quantity,
                price=sl_price,
                order_type='LIMIT', 
                reduce_only=True
            )
            
            # Сохраняем информацию
            active_position = True
            last_trade_info = {
                'signal': signal,
                'side': side,
                'quantity': quantity,
                'entry_price': entry_price,
                'tp_price': tp_price,
                'sl_price': sl_price,
                'timestamp': time.time()
            }
            
            # Отправляем уведомление
            msg = f"""✅ ПОЗИЦИЯ ОТКРЫТА
Символ: {SYMBOL}
Сторона: {side}
Количество: {quantity}
Цена входа: ${entry_price:.4f}
TP: ${tp_price:.4f} | SL: ${sl_price:.4f}
Баланс: {balance:.2f} USDT"""
            
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
            return True
            
        else:
            error_msg = order_result.get('message', 'Unknown error') if order_result else 'No response'
            raise ValueError(f"Ошибка ордера: {error_msg}")
            
    except Exception as e:
        error_msg = f"❌ Ошибка открытия позиции: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        active_position = False
        return False

# === Старт ===
@app.on_event("startup")
async def startup():
    try:
        logger.info("=== ЗАПУСК БОТА ===")
        
        # Тестируем подключение
        balance = await mexc_client.get_balance()
        price = await mexc_client.get_price()
        
        msg = f"""✅ MEXC Бот запущен!

Символ: {SYMBOL}
Риск: {RISK_PERCENT}%
Плечо: {LEVERAGE}x
Баланс: {balance:.2f} USDT
Цена {SYMBOL}: ${price:.4f}

Готов к работе!"""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Бот успешно запущен")
        
    except Exception as e:
        error_msg = f"❌ Ошибка запуска: {e}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)

# === Webhook ===
@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_simple_position(signal))
        
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

# === Главная страница ===
@app.get("/")
async def home():
    global last_trade_info, active_position
    status = "АКТИВНА" if active_position else "НЕТ"
    
    html = f"""
    <html>
        <head>
            <title>MEXC Trading Bot</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial; background: #1e1e1e; color: white; padding: 20px; }}
                .card {{ background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px; }}
                .success {{ color: #00b894; }}
                .error {{ color: #e74c3c; }}
            </style>
        </head>
        <body>
            <h1 class="success">🤖 MEXC Trading Bot</h1>
            
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
                <h3>🔗 Webhook</h3>
                <p>Endpoint: <code>POST /webhook</code></p>
                <p>Header: <code>Authorization: Bearer YOUR_SECRET</code></p>
                <p>Body: <code>{{"signal": "buy"}}</code></p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)

# === Баланс ===
@app.get("/balance")
async def get_balance():
    balance = await mexc_client.get_balance()
    return {"balance": balance, "currency": "USDT"}

# === Закрыть позиции ===  
@app.post("/close")
async def close_positions():
    result = await mexc_client.close_all_positions()
    if result:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ Все позиции закрыты")
        return {"status": "ok", "message": "Positions closed"}
    else:
        return {"status": "error", "message": "No positions to close"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
