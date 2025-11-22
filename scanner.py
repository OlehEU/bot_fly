# scanner.py — БЕСПЛАТНЫЙ СКАНЕР OZ 2026 (работает на Python 3.11)
import asyncio
import httpx
import pandas as pd
import ccxt.async_support as ccxt
from datetime import datetime
import os
import numpy as np

# ====================== НАСТРОЙКИ ======================
WEBHOOK_URL = "https://bot-fly-oz.fly.dev/webhook"
SECRET = "supersecret123"

COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]
TIMEFRAME = "5m"
CHECK_INTERVAL = 30  # секунд

# Параметры OZ стратегии
EMA_LENGTH = 5
RSI_LENGTH = 7
RSI_THRESHOLD = 40
VOLUME_MULTIPLIER = 1.5

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# ====================== RSI ФУНКЦИЯ (вместо pandas-ta) ======================
def calculate_rsi(prices, window=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ====================== EMA ФУНКЦИЯ ======================
def calculate_ema(prices, window):
    return prices.ewm(span=window).mean()

# ====================== СИГНАЛЫ ======================
async def send_signal(coin: str, signal: str):
    payload = {
        "secret": SECRET,
        "signal": signal,
        "coin": coin
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, json=payload, timeout=10.0)
        print(f"✅ СИГНАЛ {signal.upper()} {coin} отправлен в бот!")
    except Exception as e:
        print(f"❌ Ошибка отправки {coin}: {e}")

# ====================== ПОЛУЧЕНИЕ ДАННЫХ ======================
async def fetch_ohlcv(symbol: str):
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ Ошибка загрузки {symbol}: {e}")
        return pd.DataFrame()

# ====================== СТРАТЕГИЯ OZ ======================
async def check_oz_strategy(coin: str):
    symbol = f"{coin}/USDT"
    df = await fetch_ohlcv(symbol)
    
    if len(df) < 50:
        return

    # Индикаторы (встроенные в pandas)
    df['ema'] = calculate_ema(df['close'], EMA_LENGTH)
    df['rsi'] = calculate_rsi(df['close'], RSI_LENGTH)
    df['vol_sma'] = df['volume'].rolling(window=20).mean()

    # Текущие значения
    current_close = df['close'].iloc[-1]
    current_ema = df['ema'].iloc[-1]
    current_rsi = df['rsi'].iloc[-1]
    current_volume = df['volume'].iloc[-1]
    vol_sma = df['vol_sma'].iloc[-1]

    volume_spike = current_volume > vol_sma * VOLUME_MULTIPLIER

    # Проверяем текущую позицию
    try:
        positions = await exchange.fetch_positions([symbol])
        has_position = any(p['contracts'] > 0 for p in positions)
    except Exception as e:
        print(f"❌ Ошибка проверки позиции {coin}: {e}")
        has_position = False

    # СИГНАЛЫ
    buy_signal = (current_close > current_ema and 
                  current_rsi > RSI_THRESHOLD and 
                  volume_spike and 
                  not has_position)

    sell_signal = (current_close < current_ema and has_position)

    if buy_signal:
        await send_signal(coin, "buy")
    elif sell_signal:
        await send_signal(coin, "close_all")

# ====================== ГЛАВНЫЙ ЦИКЛ ======================
async def main():
    print("🚀 OZ СКАНЕР 2026 ЗАПУЩЕН — БЕСПЛАТНО!")
    print(f"📊 Мониторим: {', '.join(COINS)}")
    print(f"⏱️  Таймфрейм: {TIMEFRAME} | Проверка каждые {CHECK_INTERVAL} сек")
    print(f"🔗  Сигналы в: {WEBHOOK_URL}")
    print("=" * 50)

    while True:
        try:
            # Проверяем все коины параллельно
            tasks = [check_oz_strategy(coin) for coin in COINS]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            print(f"✅ {datetime.now().strftime('%H:%M:%S')} — Проверка завершена")
        except KeyboardInterrupt:
            print("\n🛑 Сканер остановлен")
            break
        except Exception as e:
            print(f"❌ Критическая ошибка: {e}")
        
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
