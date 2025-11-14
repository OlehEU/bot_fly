import os
import json
import asyncio
import logging
import time
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан!")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = "XRP/USDT:USDT"
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram (в отдельном потоке) ===
bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str):
    def _send():
        try:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Telegram error: {e}")
    try:
        await asyncio.to_thread(_send)
    except Exception as e:
        logger.error(f"Telegram thread error: {e}")

# === MEXC Exchange ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === Вспомогательные функции ===
async def get_current_price() -> float:
    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        price = float(ticker['last'])
        logger.info(f"Цена {SYMBOL}: {price:.6f}")
        return price
    except Exception as e:
        logger.error(f"Ошибка цены: {e}")
        return 0.0

async def check_balance() -> float:
    try:
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        logger.info(f"Баланс USDT: {usdt:.4f}")
        return float(usdt)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        return 0.0

async def calculate_qty(usd_amount: float) -> float:
    try:
        await exchange.load_markets()
        market = exchange.markets[SYMBOL]
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Цена = 0")

        raw_qty = usd_amount / price
        qty = exchange.amount_to_precision(SYMBOL, raw_qty)
        qty = float(qty)

        min_qty = market['limits']['amount']['min']
        if qty < min_qty:
            qty = min_qty

        logger.info(f"qty: {usd_amount} USD → {qty} @ {price}")
        return qty
    except Exception as e:
        logger.error(f"Ошибка qty: {e}")
        return 0.0

async def open_position(signal: str, amount_usd=None):
    global last_trade_info, active_position
    if active_position:
        logger.info("Позиция уже открыта")
        return

    try:
        logger.info(f"ОТКРЫТИЕ {signal.upper()}")

        balance = await check_balance()
        if balance <= 5:
            raise ValueError(f"Мало средств: {balance:.2f} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        if usd < 5:
            usd = 5

        qty = await calculate_qty(usd)
        if qty <= 0:
            raise ValueError(f"qty = {qty}")

        side = "buy" if signal == "buy" else "sell"
        pos_type = 1 if signal == "buy" else 2

        # Установка плеча
        try:
            await exchange.set_leverage(LEVERAGE, SYMBOL, params={"openType": 1, "positionType": pos_type})
        except Exception as e:
            logger.warning(f"set_leverage: {e}")

        # Закрываем старую позицию
        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            contracts = pos.get("contracts", 0)
            if contracts > 0:
                close_side = "sell" if pos["side"] == "long" else "buy"
                await exchange.create_order(
                    SYMBOL, "market", close_side, contracts,
                    params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE, "reduceOnly": True}
                )

        # Открываем новую
        order = await exchange.create_order(
            SYMBOL, "market", side, qty,
            params={"openType": 1, "positionType": pos_type, "leverage": LEVERAGE}
        )

        entry = await get_current_price()

        active_position = True
        last_trade_info = {
            "signal": signal,
            "side": side,
            "qty": qty,
            "entry": entry,
            "balance": balance,
            "order_id": order.get('id', 'N/A'),
            "timestamp": time.time()
        }

        msg = (
            f"<b>{side.upper()} ОТКРЫТА</b>\n"
            f"Символ: <code>{SYMBOL}</code>\n"
            f"Количество: <code>{qty}</code>\n"
            f"Вход: <code>${entry:.4f}</code>\n"
            f"Баланс: <code>{balance:.2f}</code> USDT"
        )
        await tg_send(msg)
        logger.info("ПОЗИЦИЯ ОТКРЫТА!")

    except Exception as e:
        err_msg = f"<b>ОШИБКА {signal}</b>\n<code>{e}</code>"
        await tg_send574(err_msg)
        logger.error(f"Ошибка: {e}")
        active_position = False

# === FastAPI ===
@app.on_event("startup")
async def startup_event():
    try:
        await exchange.load_markets()
        balance = await check_balance()
        price = await get_current_price()
        msg = (
            f"<b>MEXC Bot ЗАПУЩЕН</b>\n"
            f"Баланс: <code>{balance:.2f}</code> USDT\n"
            f"Символ: <code>{SYMBOL}</code>\n"
            f"Цена: <code>${price:.4f}</code>"
        )
        await tg_send(msg)
    except Exception as e:
        logger.error(f"Startup error: {e}")

@app.on_event("shutdown")
async def shutdown():
    await exchange.close()

@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, "Unauthorized")
    try:
        data = await request.json()
        signal = data.get("signal")
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be buy/sell"}
        asyncio.create_task(open_position(signal))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/")
async def home():
    balance = await check_balance()
    price = await get_current_price()
    status = "АКТИВНА" if active_position else "НЕТ"
    last = json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"
    return HTMLResponse(f"""
    <html><head><meta charset="utf-8"><title>MEXC Bot</title>
    <style>body {{font-family:Arial;background:#1e1e1e;color:white;padding:20px}}
    .card {{background:#2d2d2d;padding:20px;margin:10px 0;border-radius:10px}}
    .success {{color:#00b894}} .warning {{color:#fdcb6e}}</style></head>
    <body>
    <h1 class="success">MEXC Futures Bot</h1>
    <div class="card"><h3>БАЛАНС</h3><p>USDT: {balance:.2f}</p></div>
    <div class="card"><h3>СТАТУС</h3>
    <p>Символ: {SYMBOL}</p>
    <p>Цена: ${price:.4f}</p>
    <p>Позиция: <span class="{'success' if active_position else 'warning'}">{status}</span></p></div>
    <div class="card"><h3>ПОСЛЕДНЯЯ СДЕЛКА</h3><pre>{last}</pre></div>
    </body></html>
    """)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
