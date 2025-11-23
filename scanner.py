# scanner.py — ПОЛНАЯ ЗАМЕНА TRADINGVIEW (бесплатно, быстро, надёжно)
import asyncio
import httpx
import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime

# === НАСТРОЙКИ ===
WEBHOOK = "https://bot-fly-oz.fly.dev/webhook"
SECRET = "supersecret123"

# Коины, которые мониторим (добавляй сколько хочешь)
COINS = ["XRP", "SOL", "ETH", "BTC", "DOGE"]

# Параметры твоей стратегии OZ (точно как в Pine Script)
EMA_LENGTH = 5
RSI_LENGTH = 7
RSI_THRESHOLD = 40
VOLUME_MULTIPLIER = 1.5
TIMEFRAME = "5m"
CHECK_INTERVAL = 25  # секунд (можно и 10)

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# === RSI и EMA вручную (без pandas-ta) ===
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def send_signal(coin: str, signal: str):
    payload = {"secret": SECRET, "signal": signal, "coin": coin}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(WEBHOOK, json=payload, timeout=10)
            print(f"{datetime.now().strftime('%H:%M:%S')} → {signal.upper()} {coin}")
        except:
            print(f"Ошибка отправки сигнала {coin}")

async def check_coin(coin: str):
    try:
        symbol = f"{coin}/USDT"
        bars = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        
        df['ema'] = df['close'].ewm(span=EMA_LENGTH).mean()
        df['rsi'] = rsi(df['close'], RSI_LENGTH)
        df['vol_sma'] = df['volume'].rolling(20).mean()

        close = df['close'].iloc[-1]
        ema = df['ema'].iloc[-1]
        rsi_val = df['rsi'].iloc[-1]
        vol_spike = df['volume'].iloc[-1] > df['vol_sma'].iloc[-1] * VOLUME_MULTIPLIER

        # Проверяем, есть ли уже позиция
        positions = await exchange.fetch_positions([symbol])
        has_position = any(p['contracts'] > 0 for p in positions)

        # === ТОЧНО КАК В ТВОЕЙ СТРАТЕГИИ ===
        buy = close > ema and rsi_val > RSI_THRESHOLD and vol_spike and not has_position
        sell = close < ema and has_position

        if buy:
            await send_signal(coin, "buy")
        elif sell:
            await send_signal(coin, "close_all")

    except Exception as e:
        print(f"Ошибка {coin}: {e}")

async def main():
    print("ЗАМЕНА TRADINGVIEW ЗАПУЩЕНА — СВОБОДА!")
    print(f"Мониторим: {', '.join(COINS)} | {TIMEFRAME} | Каждые {CHECK_INTERVAL}с")
    while True:
        await asyncio.gather(*[check_coin(c) for c in COINS])
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
