import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot
from pybit.unified_trading import HTTP

# === Настройки из окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() == "true"
TRADE_TYPE = os.getenv("TRADE_TYPE", "linear")
LEVERAGE = int(os.getenv("LEVERAGE", 1))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # ОБЯЗАТЕЛЬНО!

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
active_position = False  # Защита от дублей


# === Вспомогательные функции ===
def calculate_qty(usd_amount: float, symbol: str = SYMBOL) -> float:
    """Переводит USD в количество монет"""
    try:
        ticker = client.get_tickers(category=TRADE_TYPE, symbol=symbol)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        qty = usd_amount / price
        return round(qty, 3)
    except Exception as e:
        logger.error(f"Ошибка расчёта qty: {e}")
        return 0.0


def check_balance(required_usd: float) -> bool:
    """Проверка баланса USDT"""
    try:
        balance = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        available = float(balance["result"]["list"][0]["coin"][0]["walletBalance"])
        return available >= required_usd * 1.1  # +10% на комиссии
    except Exception as e:
        logger.error(f"Ошибка проверки баланса: {e}")
        return False


# === Уведомление при старте ===
@app.on_event("startup")
async def startup_notify():
    try:
        env = "Тестнет" if BYBIT_TESTNET else "Продакшн"
        msg = f"Бот запущен!\n\n" \
              f"Режим: {env}\n" \
              f"Торговля: {TRADE_TYPE}\n" \
              f"Символ: {SYMBOL}\n" \
              f"Лот: {TRADE_USD} USDT\n" \
              f"Плечо: {LEVERAGE}x"
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
        <b>Пример:</b> <code>{"signal":"buy"}</code></p>
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

        if not check_balance(usd):
            raise ValueError("Недостаточно USDT на балансе")

        qty = calculate_qty(usd, symbol)
        if qty <= 0:
            raise ValueError("Неверный размер позиции")

        # Устанавливаем плечо
        client.set_leverage(
            category=TRADE_TYPE,
            symbol=symbol,
            buyLeverage=str(LEVERAGE),
            sellLeverage=str(LEVERAGE)
        )

        # Закрываем старую позицию
        positions = client.get_positions(category=TRADE_TYPE, symbol=symbol)
        for pos in positions.get("result", {}).get("list", []):
            if float(pos["size"]) > 0:
                opp_side = "Sell" if pos["side"] == "Buy" else "Buy"
                client.place_order(
                    category=TRADE_TYPE,
                    symbol=symbol,
                    side=opp_side,
                    orderType="Market",
                    qty=pos["size"],
                    timeInForce="IOC"
                )
                logger.info(f"Закрыта позиция: {pos['side']} {pos['size']}")

        # Открываем новую
        order = client.place_order(
            category=TRADE_TYPE,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC"
        )

        order_id = order.get("result", {}).get("orderId")
        entry_price = float(order.get("result", {}).get("avgPrice", 0)) or 0

        # Take Profit & Stop Loss
        tp_price = round(entry_price * (1.015 if side == "Buy" else 0.985), 2)
        sl_price = round(entry_price * (0.99 if side == "Buy" else 1.01), 2)

        client.place_order(
            category=TRADE_TYPE,
            symbol=symbol,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Limit",
            qty=str(qty),
            price=str(tp_price),
            timeInForce="GTC",
            reduceOnly=True
        )
        client.place_order(
            category=TRADE_TYPE,
            symbol=symbol,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Limit",
            qty=str(qty),
            price=str(sl_price),
            timeInForce="GTC",
            reduceOnly=True
        )

        active_position = True
        last_trade_info = {
            "signal": signal,
            "side": side,
            "usd": usd,
            "qty": qty,
            "symbol": symbol,
            "order_id": order_id,
            "entry_price": entry_price,
            "tp": tp_price,
            "sl": sl_price
        }

        msg = f"Ордер {side} {qty} {symbol}\n" \
              f"Цена входа: {entry_price}\n" \
              f"TP: {tp_price} | SL: {sl_price}"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(msg)

    except Exception as e:
        err_msg = f"Ошибка {signal}: {e}"
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        logger.error(err_msg)
        active_position = False


# === Webhook ===
@app.post("/webhook")
async def webhook(request: Request):
    # Проверка секрета
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    signal = data.get("signal")
    amount = data.get("amount")
    symbol = data.get("symbol", SYMBOL)

    if signal not in ["buy", "sell"]:
        return {"status": "error", "message": "signal: 'buy' или 'sell'"}

    asyncio.create_task(open_position(signal, amount, symbol))
    return {"status": "ok", "message": f"{signal} сигнал принят"}
