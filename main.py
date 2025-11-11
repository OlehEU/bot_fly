import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot
from pybit.unified_trading import HTTP

# === НАСТРОЙКИ ПО УМОЛЧАНИЮ — MAINNET + ФЬЮЧЕРСЫ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TRADE_USD = float(os.getenv("TRADE_USD", 25))
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", 0.1))
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() == "true"  # ПО УМОЛЧАНИЮ: False
TRADE_TYPE = os.getenv("TRADE_TYPE", "linear")  # linear = фьючерсы
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # ОБЯЗАТЕЛЬНО!

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bybit-bot")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === Bybit client — MAINNET + ФЬЮЧЕРСЫ ===
client = HTTP(
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    testnet=BYBIT_TESTNET  # False = mainnet
)

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === Вспомогательные функции ===
def calculate_qty(usd_amount: float, symbol: str = SYMBOL) -> float:
    try:
        ticker = client.get_tickers(category=TRADE_TYPE, symbol=symbol)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        qty = usd_amount / price
        return round(qty, 3)
    except Exception as e:
        logger.error(f"Ошибка расчёта qty: {e}")
        return 0.0

def check_balance(required_usd: float = 0) -> float:
    try:
        balance = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        available = float(balance["result"]["list"][0]["coin"][0]["walletBalance"])
        logger.info(f"Баланс USDT (UTA): {available}")
        return available
    except Exception as e:
        logger.error(f"Ошибка проверки баланса: {e}")
        return 0.0

# === Уведомление при старте ===
@app.on_event("startup")
async def startup_notify():
    try:
        env = "Тестнет" if BYBIT_TESTNET else "Продакшн"
        msg = (
            f"Bybit Бот запущен! [{env}]\n\n"
            f"Режим: ФЬЮЧЕРСЫ (linear)\n"
            f"Символ: {SYMBOL}\n"
            f"Лот: {TRADE_USD} USDT\n"
            f"Плечо: {LEVERAGE}x\n"
            f"Время: 23:31 CET, 11 ноября 2025"
        )
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("Стартовое уведомление отправлено.")
    except Exception as e:
        logger.error(f"Ошибка старта: {e}")

# === Главная страница ===
@app.get("/", response_class=HTMLResponse)
async def home():
    global last_trade_info, active_position
    last_trade_text = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "нет"
    endpoint = "Тестнет" if BYBIT_TESTNET else "Продакшн"
    status = "Активна" if active_position else "Нет"
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
      <head>
        <meta charset="utf-8">
        <title>Bybit Futures Bot</title>
        <style>
          body {{ font-family: 'Segoe UI', sans-serif; background:#1e1e1e; color:#e0e0e0; padding:20px; }}
          h2 {{ color:#00b894; }}
          pre {{ background:#2d2d2d; padding:12px; border-radius:8px; }}
          code {{ background:#333; padding:2px 6px; border-radius:4px; }}
          a {{ color:#00b894; }}
          .status.active {{ background:#00b894; color:#000; padding:4px 8px; border-radius:4px; }}
          .status.inactive {{ background:#555; color:#eee; padding:4px 8px; border-radius:4px; }}
        </style>
      </head>
      <body>
        <h2>Bybit Futures Bot</h2>
        <ul>
          <li><b>Режим:</b> ФЬЮЧЕРСЫ (linear)</li>
          <li><b>Символ:</b> {SYMBOL}</li>
          <li><b>Лот:</b> {TRADE_USD} USDT</li>
          <li><b>Плечо:</b> {LEVERAGE}×</li>
          <li><b>Environment:</b> {endpoint}</li>
          <li><b>Позиция:</b> <span class="status {'active' if active_position else 'inactive'}">{status}</span></li>
        </ul>
        <h3>Последняя сделка:</h3>
        <pre>{last_trade_text}</pre>
        <p>
          <b>Webhook:</b> <code>POST /webhook</code><br>
          <b>Header:</b> <code>Authorization: Bearer {WEBHOOK_SECRET}</code><br>
          <b>Пример:</b> <code>{{"signal":"buy", "amount":10}}</code><br>
          <a href="/balance">Проверить баланс</a>
        </p>
        <hr>
        <small>Время: 23:31 CET, 11 ноября 2025 | DE</small>
      </body>
    </html>
    """

# === Баланс в JSON ===
@app.get("/balance")
async def get_balance():
    balance = check_balance()
    return {"usdt_balance": balance, "min_required": TRADE_USD * 1.1}

# === Открытие позиции ===
async def open_position(signal: str, amount_usd=None, symbol: str = SYMBOL):
    global last_trade_info, active_position
    if active_position:
        logger.info("Позиция уже открыта.")
        return
    try:
        side = "Buy" if signal.lower() == "buy" else "Sell"
        usd = float(amount_usd) if amount_usd else TRADE_USD
        available = check_balance(usd)
        if available < usd * 1.1:
            raise ValueError(f"Недостаточно USDT: {available:.2f} < {usd * 1.1:.2f}")
        qty = calculate_qty(usd, symbol)
        if qty <= 0:
            raise ValueError("Неверный размер позиции")

        # Плечо
        client.set_leverage(category=TRADE_TYPE, symbol=symbol, buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE))

        # Закрываем старую
        positions = client.get_positions(category=TRADE_TYPE, symbol=symbol)
        for pos in positions.get("result", {}).get("list", []):
            if float(pos["size"]) > 0:
                opp_side = "Sell" if pos["side"] == "Buy" else "Buy"
                client.place_order(category=TRADE_TYPE, symbol=symbol, side=opp_side, orderType="Market", qty=pos["size"], timeInForce="IOC")

        # Открываем новую
        order = client.place_order(category=TRADE_TYPE, symbol=symbol, side=side, orderType="Market", qty=str(qty), timeInForce="IOC")
        order_id = order.get("result", {}).get("orderId")
        entry_price = float(order.get("result", {}).get("avgPrice") or 0)

        # TP/SL
        tp_price = round(entry_price * (1.015 if side == "Buy" else 0.985), 2)
        sl_price = round(entry_price * (0.99 if side == "Buy" else 1.01), 2)
        client.place_order(category=TRADE_TYPE, symbol=symbol, side="Sell" if side == "Buy" else "Buy", orderType="Limit", qty=str(qty), price=str(tp_price), timeInForce="GTC", reduceOnly=True)
        client.place_order(category=TRADE_TYPE, symbol=symbol, side="Sell" if side == "Buy" else "Buy", orderType="Limit", qty=str(qty), price=str(sl_price), timeInForce="GTC", reduceOnly=True)

        active_position = True
        last_trade_info = {
            "signal": signal, "side": side, "usd": usd, "qty": qty,
            "symbol": symbol, "order_id": order_id, "entry_price": entry_price,
            "tp": tp_price, "sl": sl_price
        }
        msg = (
            f"{side} {qty:.3f} {symbol}\n"
            f"Цена: ${entry_price:.2f}\n"
            f"TP: ${tp_price:.2f} | SL: ${sl_price:.2f}\n"
            f"Баланс: {available:.2f} USDT"
        )
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(msg)
    except Exception as e:
        err_msg = f"Ошибка {signal}: {e}\nБаланс: {check_balance():.2f} USDT"
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
