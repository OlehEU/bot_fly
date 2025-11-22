# scanner.py — БЕСПЛАТНЫЙ СКАНЕР СИГНАЛОВ 2026 (замена TradingView)
import asyncio
import httpx
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from datetime import datetime
import os

# ====================== НАСТРОЙКИ ======================
WEBHOOK_URL = "https://bot-fly-oz.fly.dev/webhook"   # ← твой бот
WEBHOOK_SECRET = "supersecret123"                    # ← тот же секрет

# Список коинов (добавляй/удаляй сколько угодно)
COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]

# Параметры твоей стратегии OZ
EMA_LENGTH = 5
RSI_LENGTH = 7
RSI_THRESHOLD = 40
VOLUME_MULTIPLIER = 1.5
TIMEFRAME = "5m"           # можно 1m, 3m, 15m и т.д.
CHECK_INTERVAL = 30        # секунд между проверками

# ====================== BINANCE ======================
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})

async def fetch_ohlcv(symbol: str):
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"Ошибка загрузки {symbol}: {e}")
        return pd.DataFrame()

async def send_signal(coin: str, signal: str):
    payload = {
        "secret": WEBHOOK_SECRET,
        "signal": signal,
        "coin": coin
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, json=payload, timeout=10.0)
        print(f"Сигнал {signal.upper()} {coin} — отправлено в бот!")
    except Exception as e:
        print(f"Ошибка отправки сигнала {coin}: {e}")

async def check_coin(coin: str):
    symbol = f"{coin}/USDT"
    df = await fetch_ohlcv(symbol)
    if df.empty or len(df) < 50:
        return

    # Индикаторы
    df['ema'] = ta.ema(df['close'], length=EMA_LENGTH)
    df['rsi'] = ta.rsi(df['close'], length=RSI_LENGTH)
    df['vol_sma'] = df['volume'].rolling(20).mean()

    close = df['close'].iloc[-1]
    ema = df['ema'].iloc[-1]
    rsi = df['rsi'].iloc[-1]
    volume_spike = df['volume'].iloc[-1] > df['vol_sma'].iloc[-1] * VOLUME_MULTIPLIER

    # Текущая позиция
    try:
        pos = await exchange.fetch_positions([symbol])
        has_long = pos[0]['contracts'] > 0
    except:
        has_long = False

    # УСЛОВИЯ ВХОДА / ВЫХОДА
    buy_signal = close > ema and rsi > RSI_THRESHOLD and volume_spike and not has_long
    sell_signal = close < ema and has_long

    if buy_signal:
        await send_signal(coin, "buy")
    elif sell_signal:
        await send_signal(coin, "close_all")

async def main():
    print("СКАНЕР OZ 2026 ЗАПУЩЕН — БЕСПЛАТНО И НАВСЕГДА!")
    print(f"Мониторим: {', '.join(COINS)} | Таймфрейм: {TIMEFRAME} | Каждые {CHECK_INTERVAL} сек")
    
    while True:
        try:
            tasks = [check_coin(coin) for coin in COINS]
            await asyncio.gather(*tasks)
        except Exception as e:
            print(f"Ошибка в цикле: {e}")
        
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
