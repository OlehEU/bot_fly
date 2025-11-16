import asyncio
import time
import hmac
import hashlib
import json
import httpx
import logging
from aiogram import Bot

# ==================== НАСТРОЙКИ ====================
MEXC_API_KEY = "ВАШ_API_KEY"
MEXC_API_SECRET = "ВАШ_API_SECRET"
SYMBOL = "XRP_USDT"
USDT_AMOUNT = 5  # размер сделки
LEVERAGE = 10
TP_PERCENT = 0.5  # Take Profit в процентах

TELEGRAM_TOKEN = "ВАШ_TELEGRAM_BOT_TOKEN"
CHAT_ID = "ВАШ_CHAT_ID"

REQUEST_TIMEOUT = 10

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")
logger = logging.getLogger("mexc-bot")

# ==================== TELEGRAM ====================
bot = Bot(token=TELEGRAM_TOKEN)

async def send_telegram(msg: str):
    try:
        await bot.send_message(CHAT_ID, msg)
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

# ==================== MEXC API ====================
async def get_price(symbol=SYMBOL):
    """Получение текущей цены"""
    url = f"https://contract.mexc.com/api/v1/contract/ticker?symbol={symbol}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(url)
        data = resp.json()
        price = float(data['data']['lastPrice'])
        logger.info(f"Цена {symbol}: {price}")
        return price

async def check_balance():
    """Проверка баланса USDT с безопасным логированием UTF-8"""
    url = "https://contract.mexc.com/api/v1/private/account/asset/USDT"
    timestamp = str(int(time.time() * 1000))
    body = ""
    sign_str = MEXC_API_KEY + timestamp + body
    signature = hmac.new(
        MEXC_API_SECRET.encode('utf-8'),
        sign_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": timestamp, "Signature": signature}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        data = resp.json()
        try:
            free = float(data['data']['available'])
            total = float(data['data']['total'])
            used = total - free
            logger.info(f"Баланс: total={total}, free={free}, used={used}")
            return {"total": total, "free": free, "used": used}
        except Exception as e:
            logger.error(f"Ошибка баланса: {str(e)} — raw data: {json.dumps(data, ensure_ascii=False)}")
            return {"total": 0, "free": 0, "used": 0}

async def open_order(side: str):
    """Открытие ордера на MEXC"""
    price = await get_price()
    quantity = round(USDT_AMOUNT * LEVERAGE / price, 4)  # количество монеты
    timestamp = str(int(time.time() * 1000))
    body = json.dumps({
        "symbol": SYMBOL,
        "price": str(price),
        "vol": str(quantity),
        "side": side.upper(),  # BUY или SELL
        "type": "LIMIT",
        "open_type": "CROSS",
        "position_id": 0,
        "leverage": LEVERAGE,
        "external_oid": f"{int(time.time())}"
    })
    sign_str = MEXC_API_KEY + timestamp + body
    signature = hmac.new(
        MEXC_API_SECRET.encode('utf-8'),
        sign_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": timestamp, "Signature": signature, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post("https://contract.mexc.com/api/v1/private/order/submit", headers=headers, data=body)
        data = resp.json()
        logger.info(f"Открытие ордера: {json.dumps(data, ensure_ascii=False)}")
        await send_telegram(f"Открыт ордер {side.upper()} {SYMBOL} цена {price}")
        return data

async def check_position():
    """Проверка активных позиций"""
    url = f"https://contract.mexc.com/api/v1/private/position/list?symbol={SYMBOL}"
    timestamp = str(int(time.time() * 1000))
    body = ""
    sign_str = MEXC_API_KEY + timestamp + body
    signature = hmac.new(
        MEXC_API_SECRET.encode('utf-8'),
        sign_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": timestamp, "Signature": signature}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        data = resp.json()
        positions = data.get("data", [])
        return positions

async def main():
    last_order_closed = True
    while True:
        # Тут должна быть логика получения сигнала из TradingView
        # Для примера:
        signal = "buy"  # пример сигнала
        if signal == "buy" and last_order_closed:
            balance = await check_balance()
            if balance['free'] >= USDT_AMOUNT:
                await open_order("BUY")
                last_order_closed = False
        elif signal == "sell" and last_order_closed:
            balance = await check_balance()
            if balance['free'] >= USDT_AMOUNT:
                await open_order("SELL")
                last_order_closed = False
        
        # Проверяем позиции, чтобы понять закрыт ли ордер
        positions = await check_position()
        if not positions:
            last_order_closed = True
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
