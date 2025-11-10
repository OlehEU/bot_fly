# main.py
import os
import json
import logging
from pathlib import Path
from typing import Optional
import math
import requests

from fastapi import FastAPI, Request, HTTPException

# try to import pybit HTTP wrapper (v5 style)
try:
    from pybit import HTTP as BybitHTTP
except Exception as e:
    BybitHTTP = None
    logging.warning("pybit HTTP import failed: %s", e)

# ==========================
# ENV / Конфигурация
# ==========================
# Обязательные (через Secrets / Environment)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Торговые настройки (можно менять через ENV)
TRADE_USD = float(os.getenv("TRADE_USD", "25"))         # сумма в USDT на вход
SYMBOL = os.getenv("SYMBOL", "SOLUSDT").upper()         # SOLUSDT
MIN_PROFIT_USDT = float(os.getenv("MIN_PROFIT_USDT", "0.1"))  # минимальная прибыль для закрытия
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() in ("1", "true", "yes")
TRADE_TYPE = os.getenv("TRADE_TYPE", "futures").lower() # "spot" или "futures"
LEVERAGE = int(os.getenv("LEVERAGE", "1"))             # плечо (для фьючерсов), default 1

# файл состояния
STATE_FILE = Path("trade_state.json")

# Bybit endpoint (Unified public)
BYBIT_ENDPOINT = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Warn if telegram not configured
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logging.warning("Telegram not configured - notifications disabled.")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    logging.warning("Bybit keys not set - trading disabled.")

# ==========================
# Инициализация Bybit клиента (если pybit доступен)
# ==========================
bybit_client = None
if BybitHTTP and BYBIT_API_KEY and BYBIT_API_SECRET:
    try:
        bybit_client = BybitHTTP(endpoint=BYBIT_ENDPOINT, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
        logging.info("Initialized pybit HTTP client (endpoint=%s)", BYBIT_ENDPOINT)
    except Exception as e:
        logging.exception("Failed to init Bybit client: %s", e)
        bybit_client = None

# ==========================
# Telegram helper (HTTP)
# ==========================
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram disabled — message would be: %s", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        if not r.ok:
            logging.warning("Telegram API returned %s: %s", r.status_code, r.text)
    except Exception as e:
        logging.exception("Failed to send telegram message: %s", e)

# ==========================
# State helpers
# ==========================
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"trade": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"trade": None}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

# ensure state exists
if not STATE_FILE.exists():
    save_state({"trade": None})

# ==========================
# Bybit helpers (wrapper around pybit where возможно)
# ==========================
def get_price(symbol: str) -> Optional[float]:
    if not bybit_client:
        logging.warning("Bybit client not initialized - cannot fetch price")
        return None
    try:
        resp = bybit_client.latest_information_for_symbol(symbol=symbol)
        # try typical v5 structure: resp['result'][0]['last_price']
        if isinstance(resp, dict) and "result" in resp:
            r = resp["result"]
            if isinstance(r, list) and r and "last_price" in r[0]:
                return float(r[0]["last_price"])
            # some endpoints return dict with 'price'
            if isinstance(r, dict) and "price" in r:
                return float(r["price"])
        logging.warning("Unexpected price response: %s", resp)
    except Exception as e:
        logging.exception("Get price error: %s", e)
    return None

def try_set_leverage(symbol: str, leverage: int) -> dict:
    """
    Попытаться установить плечо для symbol (для фьючерсов).
    Возвращает API-ответ или пустой dict.
    """
    if not bybit_client:
        raise RuntimeError("Bybit client not available")
    try:
        # pybit может иметь метод set_leverage или set_leverage_usdt. Попробуем несколько вариантов.
        if hasattr(bybit_client, "set_leverage"):
            resp = bybit_client.set_leverage(symbol=symbol, buy_leverage=str(leverage), sell_leverage=str(leverage))
            logging.info("set_leverage response: %s", resp)
            return resp
        if hasattr(bybit_client, "set_leverage_bybit"):
            resp = bybit_client.set_leverage_bybit(symbol=symbol, leverage=str(leverage))
            logging.info("set_leverage_bybit response: %s", resp)
            return resp
        logging.warning("No leverage-setting method found in pybit client; skipping leverage set.")
    except Exception as e:
        logging.exception("Failed to set leverage: %s", e)
        raise
    return {}

def place_order_futures(symbol: str, side: str, qty: float) -> dict:
    """
    Поместить фьючерсный market order через pybit unified endpoint.
    side: "Buy"/"Sell"
    qty: количество контрактов/лота. Для U-based perpetual на Bybit qty — в количестве базовой валюты (например SOL)
    """
    if not bybit_client:
        raise RuntimeError("Bybit client not available")
    try:
        # В unified API нужно указать category="linear" для USDT perpetual
        resp = bybit_client.place_active_order(
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=qty,
            category="linear",   # USDT-M perpetual (обычно)
            time_in_force="GTC"
        )
        logging.info("futures order resp: %s", resp)
        return resp
    except Exception as e:
        logging.exception("Futures order failed: %s", e)
        raise

def place_order_spot(symbol: str, side: str, qty: float) -> dict:
    if not bybit_client:
        raise RuntimeError("Bybit client not available")
    try:
        resp = bybit_client.place_active_order(
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=qty,
            time_in_force="GTC"
        )
        logging.info("spot order resp: %s", resp)
        return resp
    except Exception as e:
        logging.exception("Spot order failed: %s", e)
        raise

def get_spot_balance(coin: str) -> Optional[float]:
    if not bybit_client:
        return None
    try:
        resp = bybit_client.get_wallet_balance(coin=coin)
        if isinstance(resp, dict) and "result" in resp and coin in resp["result"]:
            return float(resp["result"][coin]["available_balance"])
    except Exception as e:
        logging.exception("Get spot balance error: %s", e)
    return None

def get_futures_position_size(symbol: str) -> Optional[float]:
    """
    Получаем текущую открытую позицию по symbol в фьючерсах (кол-во базовой валюты).
    Формат ответа зависит от pybit; мы пробуем несколько путей.
    """
    if not bybit_client:
        return None
    try:
        resp = bybit_client.get_position()  # some versions require symbol param
        # try to find symbol in resp
        if isinstance(resp, dict) and "result" in resp:
            res = resp["result"]
            # if list:
            if isinstance(res, list):
                for item in res:
                    if item.get("symbol") == symbol:
                        # position size can be in 'size' or 'positionValue' etc.
                        if "size" in item and item["size"] is not None:
                            return float(item["size"])
                        if "position_value" in item:
                            # need to convert position_value/usd -> qty, skip for now
                            return float(item["position_value"])
            elif isinstance(res, dict) and symbol in res:
                # some formats use dict keyed by symbol
                item = res[symbol]
                if "size" in item:
                    return float(item["size"])
        logging.warning("Unexpected position response: %s", resp)
    except Exception as e:
        logging.exception("Get futures position error: %s", e)
    return None

# ==========================
# Торговая логика: вход и выход (поддержка spot и futures)
# ==========================
def open_buy(symbol: str, trade_usd: float, trade_type: str, leverage: int):
    price = get_price(symbol)
    if price is None:
        raise RuntimeError("Cannot obtain price")

    qty = trade_usd / price
    qty = float(round(qty, 6))  # округление — при необходимости адаптируйте

    # Если futures: пытаемся выставить плечо (если поддерживается)
    if trade_type == "futures":
        try:
            set_resp = try_set_leverage(symbol, leverage)
            logging.info("Leverage set response: %s", set_resp)
        except Exception as e:
            # продолжим даже если не удалось выставить плечо — возможно оно уже стоит
            send_telegram(f"⚠️ Warning: failed to set leverage {leverage}: {e}")

        # размещаем фьючерсный ордер
        order_resp = place_order_futures(symbol, "Buy", qty)
    else:
        # spot
        order_resp = place_order_spot(symbol, "Buy", qty)

    # сохраняем состояние
    state = load_state()
    state["trade"] = {
        "side": "buy",
        "symbol": symbol,
        "qty": qty,
        "entry_price": price,
        "trade_usd": trade_usd,
        "type": trade_type,
        "leverage": leverage if trade_type == "futures" else None,
        "status": "open",
        "order_response": order_resp
    }
    save_state(state)

    send_telegram(f"✅ OPEN {trade_type.upper()} BUY: {qty} {symbol} @ {price:.6f} (≈{trade_usd} USDT)\nOrder: {order_resp}")
    return {"order": order_resp, "qty": qty, "price": price}

def close_sell_if_profit(symbol: str, min_profit_usdt: float):
    state = load_state()
    trade = state.get("trade")
    if not trade or trade.get("status") != "open":
        raise RuntimeError("No open trade to close")

    qty = float(trade.get("qty"))
    entry_price = float(trade.get("entry_price") or 0.0)
    trade_type = trade.get("type", "spot")
    trade_usd = float(trade.get("trade_usd", TRADE_USD))

    price = get_price(symbol)
    if price is None:
        raise RuntimeError("Cannot obtain current price for profit calculation")

    current_value = qty * price
    profit = current_value - trade_usd

    logging.info("Profit calc: current=%.6f, entry_value=%.6f, profit=%.6f", current_value, trade_usd, profit)

    if profit >= min_profit_usdt:
        if trade_type == "futures":
            order_resp = place_order_futures(symbol, "Sell", qty)
        else:
            order_resp = place_order_spot(symbol, "Sell", qty)

        # обновляем состояние
        state["trade"]["status"] = "closed"
        state["trade"]["exit_price"] = price
        state["trade"]["profit_usdt"] = profit
        state["trade"]["close_order_response"] = order_resp
        save_state(state)

        send_telegram(f"✅ CLOSE {trade_type.upper()} SELL: {qty} {symbol} @ {price:.6f}. Profit: {profit:.6f} USDT\nOrder: {order_resp}")
        return {"order": order_resp, "profit": profit, "price": price}
    else:
        send_telegram(f"⚠️ SELL signal: profit {profit:.6f} USDT < min {min_profit_usdt} — skipping close.")
        return {"skipped": True, "profit": profit, "price": price}

# ==========================
# FastAPI app
# ==========================
app = FastAPI(title="Bybit Trading Bot (spot/futures)")

@app.get("/")
async def root_status():
    state = load_state()
    trade = state.get("trade")
    html = f"""
    <html>
      <head><title>Bybit bot status</title></head>
      <body style="font-family: Arial; padding:20px;">
        <h2>Bybit Trading Bot</h2>
        <ul>
          <li>Mode: {TRADE_TYPE.upper()}</li>
          <li>Symbol: {SYMBOL}</li>
          <li>Trade USD: {TRADE_USD}</li>
          <li>Min profit to sell: {MIN_PROFIT_USDT} USDT</li>
          <li>Leverage (futures): {LEVERAGE}</li>
          <li>Bybit endpoint: {BYBIT_ENDPOINT}</li>
        </ul>
        <h3>Last trade:</h3>
        <pre>{json.dumps(trade, indent=2)}</pre>
        <p>Webhook: POST /webhook with JSON {"{signal: 'buy'|'sell', optional: amount, symbol}"}</p>
      </body>
    </html>
    """
    return html

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    signal = (data.get("signal") or "").lower()
    symbol = (data.get("symbol") or SYMBOL).upper()
    amount = float(data.get("amount")) if data.get("amount") else TRADE_USD

    state = load_state()
    if signal == "buy":
        if state.get("trade") and state["trade"].get("status") == "open":
            send_telegram("⚠️ BUY signal received but a trade is already open — skipping.")
            return {"status": "error", "message": "trade_already_open"}
        try:
            result = open_buy(symbol, amount, TRADE_TYPE, LEVERAGE)
            return {"status": "ok", "signal": "buy", "result": result}
        except Exception as e:
            logging.exception("Buy failed")
            send_telegram(f"❌ Buy failed: {e}")
            return {"status": "error", "message": str(e)}
    elif signal == "sell":
        if not state.get("trade") or state["trade"].get("status") != "open":
            send_telegram("⚠️ SELL signal received but no open trade — skipping.")
            return {"status": "error", "message": "no_open_trade"}
        try:
            result = close_sell_if_profit(symbol, MIN_PROFIT_USDT)
            return {"status": "ok", "signal": "sell", "result": result}
        except Exception as e:
            logging.exception("Sell failed")
            send_telegram(f"❌ Sell failed: {e}")
            return {"status": "error", "message": str(e)}
    else:
        raise HTTPException(status_code=422, detail="Invalid signal; expected 'buy' or 'sell'")

# ==========================
# End
# ==========================
