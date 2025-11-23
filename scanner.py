# scanner.py — ОТДЕЛЬНЫЙ СКАНЕР 2026 (надёжнее, быстрее, с TP/SL и логами)
import asyncio
import httpx
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime
import os
import json

WEBHOOK = "https://bot-fly-oz.fly.dev/webhook"
SECRET = "supersecret123"
LOG_FILE = "signal_log.json"

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]
TIMEFRAME = "5m"
CHECK_INTERVAL = 25

# TP/SL и трейлинг (в %)
TAKE_PROFIT = 1.5
STOP_LOSS = 1.0
TRAILING = 0.5

exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})

def load_log():
    try:
        with open(LOG_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_log(entry):
    log = load_log()
    log.append(entry)
    if len(log) > 100: log = log[-100:]
    with open(LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)

async def send_signal(coin: str, action: str, extra=None):
    payload = {"secret": SECRET, "signal": action, "coin": coin}
    if extra: payload.update(extra)
    async with httpx.AsyncClient() as c:
        await c.post(WEBHOOK, json=payload, timeout=10)
    
    log_entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%d.%m"),
        "coin": coin,
        "action": "BUY" if action == "buy" else "SELL",
        "price": await get_price(coin)
    }
    save_log(log_entry)
    print(f"{log_entry['time']} → {log_entry['action']} {coin}")

async def get_price(coin):
    try:
        d = await exchange.fetch_ticker(f"{coin}/USDT")
        return round(d['last'], 5)
    except:
        return 0.0

async def check_coin(coin: str):
    try:
        bars = await exchange.fetch_ohlcv(f"{coin}/USDT", TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['ts','o','h','l','c','v'])
        
        df['ema'] = df['c'].ewm(span=5).mean()
        delta = df['c'].diff()
        gain = delta.where(delta > 0, 0).rolling(7).mean()
        loss = -delta.where(delta < 0, 0).rolling(7).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        df['vol_sma'] = df['v'].rolling(20).mean()

        close = df['c'].iloc[-1]
        ema = df['ema'].iloc[-1]
        rsi = df['rsi'].iloc[-1]
        vol_spike = df['v'].iloc[-1] > df['vol_sma'].iloc[-1] * 1.5

        pos = await exchange.fetch_positions([f"{coin}/USDT"])
        has_pos = pos[0]['contracts'] > 0

        if close > ema and rsi > 40 and vol_spike and not has_pos:
            await send_signal(coin, "buy", {
                "tp": TAKE_PROFIT,
                "sl": STOP_LOSS,
                "trail": TRAILING
            })
        elif close < ema and has_pos:
            await send_signal(coin, "close_all")
    except Exception as e:
        print(f"Ошибка {coin}: {e}")

async def main():
    print("СКАНЕР 2026 ЗАПУЩЕН — ОТДЕЛЬНАЯ МАШИНА")
    while True:
        await asyncio.gather(*[check_coin(c) for c in COINS])
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
