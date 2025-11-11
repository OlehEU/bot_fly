import os
import json
import asyncio
import logging
import time
from functools import wraps
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from pybit.unified_trading import HTTP

# === Проверка обязательных секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BYBIT_API_KEY", "BYBIT_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

# === Настройки из окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() == "true"
TRADE_TYPE = os.getenv("TRADE_TYPE", "linear")
LEVERAGE = int(os.getenv("LEVERAGE", 1))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fallback_secret")  # fallback, но проверка выше

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bybit-bot")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === Bybit client ===
client = HTTP(
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    testnet=BYBIT_TESTNET
)

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === RETRY ДЕКОРАТОР ===
def retry_on_403(max_retries: int = 4, delay: int = 3):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "403" in str(e) and attempt < max_retries - 1:
                        logger.warning(f"403 — повтор через {delay}s (попытка {attempt+2}/{max_retries})")
                        time.sleep(delay)
                        continue
                    logger.error(f"API ошибка (не 403): {e}")
                    raise
            return 0.0
        return wrapper
    return decorator

@retry_on_403(max_retries=4, delay=3)
def get_usdt_balance_raw(acc_type: str) -> float:
    """Внутренняя функция — один тип аккаунта"""
    resp = client.get_wallet_balance(accountType=acc_type, coin="USDT")
    data = resp.get("result", {}).get("list", [])
    if not data:
        return 0.0
    return float(data[0]["coin"][0]["walletBalance"])

# === УЛУЧШЕННЫЙ check_balance ===
async def check_balance(required_usd: float = 0) -> float:
    """Проверка баланса USDT с fallback и уведомлением"""
    balance = 0.0
    for acc_type in ["UNIFIED", "FUND"]:
        try:
            bal = get_usdt_balance_raw(acc_type)
            if bal > 0:
                logger.info(f"Баланс {acc_type}: {bal:.4f} USDT")
                balance = bal
                break
        except Exception as e:
            logger.warning(f"Ошибка {acc_type}: {e}")
            continue

    if balance == 0:
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text="БАЛАНС = 0 USDT\n\n"
                     "Проверь:\n"
                     "1. IP Fly.io в Bybit API\n"
                     "   → `185.232.66.0/24`\n"
                     "2. USDT в Unified Trading Account\n"
                     "3. Ключ: Read Account + Trade\n"
                     "4. Права ключа в Bybit"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить в Telegram: {e}")

    return balance

# === calculate_qty ===
def calculate_qty(usd_amount: float, symbol: str = SYMBOL) -> float:
    """Переводит USD в количество монет"""
    try:
        ticker = client.get_tickers(category=TRADE_TYPE, symbol=symbol)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        qty = usd_amount / price
        min_usd = 1.0
        if usd_amount < min_usd:
            raise ValueError(f"Сумма {usd_amount} USD < минимума {min_usd} USD")
        return round(qty, 3)
    except Exception as e:
        logger.error(f"Ошибка расчёта qty: {e}")
        return 0.0

# === Уведомление при старте ===
@app.on_event("startup")
async def startup_notify():
    try:
        env = "Тестнет" if BYBIT_TESTNET else "Продакшн"
        balance = await check_balance(0)
        msg = f"Бот запущен!\n\n" \
              f"Режим: {env}\n" \
              f"Торговля: {TRADE_TYPE}\n" \
              f"Символ: {SYMBOL}\n" \
              f"Лот: {TRADE_USD} USDT\n" \
              f"Плечо: {LEVERAGE}x\n" \
              f"Баланс: {balance:.2f} USDT"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Стартовое уведомление отправлено.")
    except Exception as e:
        logger.error(f"Ошибка старта: {e}")

# === Главная страница ===
@app.get("/")
async def home():
    global last_trade_info, active_position
    last_trade_text = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет"
    endpoint = "Тестнет" if BYBIT_TESTNET else "Продакшн"
    status = "Активна" if active_position else "Нет"
    return f"""
    <html>
      <head><title>Bybit Bot</title></head>
      <body style="font-family: Arial; padding:20px; background:#1e1e1e; color:#e0e0e0;">
        <h2 style="color:#00b894;">Bybit Trading Bot</h2>
        <ul>
          <li><b>Mode:</b> {TRADE_TYPE.upper()}</li>
          <li><b>Symbol:</b> {SYMBOL}</li>
          <li><b>Trade USD:</b> {TRADE_USD}</li>
          <li><b>Min profit:</b> {MIN_PROFIT_USDT} USDT</li>
          <li><b>Leverage:</b> {LEVERAGE}×</li>
          <li><b>Environment:</b> {endpoint}</li>
          <li><b>Позиция:</b> {status}</li>
        </ul>
        <h3>Последняя сделка:</h3>
        <pre style="background:#2d2d2d; padding:10px; border-radius:8px;">{last_trade_text}</pre>
        <p><b>Webhook:</b> <code>POST /webhook</code><br>
        <b>Header:</b> <code>Authorization: Bearer {WEBHOOK_SECRET}</code><br>
        <b>Пример:</b> <code>{{"signal":"buy"}}</code><br>
        <a href="/balance">Проверить баланс</a></p>
      </body>
    </html>
    """

# === Проверка баланса ===
@app.get("/balance", response_class=HTMLResponse)
async def get_balance():
    balance = await check_balance(0)
    required = TRADE_USD * 1.1
    status = "Достаточно" if balance >= required else "Недостаточно"
    color = "#00b894" if balance >= required else "#e74c3c"
    return f"""
    <html>
      <head><title>Баланс USDT</title><meta charset="utf-8"></head>
      <body style="font-family: Arial; background:#1e1e1e; color:#e0e0e0; padding:20px;">
        <h2>Баланс USDT</h2>
        <p><b>Доступно:</b> <span style="color:{color}">{balance:.2f}</span> USDT</p>
        <p><b>Требуется:</b> {required:.2f} USDT (с комиссией)</p>
        <p><b>Статус:</b> {status}</p>
        <a href="/">На главную</a>
      </body>
    </html>
    """

# === Открытие позиции ===
async def open_position(signal: str, amount_usd=None, symbol: str = SYMBOL):
    global last_trade_info, active_position
    if active_position:
        logger.info("Позиция уже открыта — пропускаем.")
        return
    try:
        side = "Buy" if signal.lower() == "buy" else "Sell"
        usd = float(amount_usd) if amount_usd else TRADE_USD
        available = await check_balance(usd)
        if available < usd * 1.1:
            raise ValueError(f"Недостаточно USDT: {available:.2f} < {usd * 1.1:.2f}")
        qty = calculate_qty(usd, symbol)
        if qty <= 0:
            raise ValueError("Неверный размер позиции")

        client.set_leverage(
            category=TRADE_TYPE,
            symbol=symbol,
            buyLeverage=str(LEVERAGE),
            sellLeverage=str(LEVERAGE)
        )

        positions = client.get_positions(category=TRADE_TYPE, symbol=symbol)
        for pos in positions.get("result", {}).get("list", []):
            if float(pos["size"]) > 0:
                opp_side = "Sell" if pos["side"] == "Buy" else "Buy"
                client.place_order(
                    category=TRADE_TYPE, symbol=symbol, side=opp_side,
                    orderType="Market", qty=pos["size"], timeInForce="IOC"
                )
                logger.info(f"Закрыта позиция: {pos['side']} {pos['size']}")

        order = client.place_order(
            category=TRADE_TYPE, symbol=symbol, side=side,
            orderType="Market", qty=str(qty), timeInForce="IOC"
        )
        order_id = order.get("result", {}).get("orderId")
        entry_price = float(order.get("result", {}).get("avgPrice", 0)) or 0

        tp_price = round(entry_price * (1.015 if side == "Buy" else 0.985), 2)
        sl_price = round(entry_price * (0.99 if side == "Buy" else 1.01), 2)

        client.place_order(
            category=TRADE_TYPE, symbol=symbol,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Limit", qty=str(qty), price=str(tp_price),
            timeInForce="GTC", reduceOnly=True
        )
        client.place_order(
            category=TRADE_TYPE, symbol=symbol,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Limit", qty=str(qty), price=str(sl_price),
            timeInForce="GTC", reduceOnly=True
        )

        active_position = True
        last_trade_info = {
            "signal": signal, "side": side, "usd": usd, "qty": qty,
            "symbol": symbol, "order_id": order_id, "entry_price": entry_price,
            "tp": tp_price, "sl": sl_price
        }
        msg = f"Ордер {side} {qty:.3f} {symbol}\n" \
              f"Цена входа: ${entry_price:.2f}\n" \
              f"TP: ${tp_price:.2f} | SL: ${sl_price:.2f}\n" \
              f"Баланс: {available:.2f} USDT"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(msg)
    except Exception as e:
        err_msg = f"Ошибка {signal}: {e}\nБаланс: {await check_balance(0):.2f} USDT"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        logger.error(err_msg)
        active_position = False

# === Webhook ===
@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        data = await request.json()
    except:
        return {"status": "error", "message": "Invalid JSON"}
    signal = data.get("signal")
    amount = data.get("amount")
    symbol = data.get("symbol", SYMBOL)
    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal: 'buy' или 'sell'"}
    asyncio.create_task(open_position(signal, amount, symbol))
    return {"status": "ok", "message": f"{signal} сигнал принят"}

# === Локальный запуск ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
