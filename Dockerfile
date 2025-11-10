# Используем официальный Python 3.12 slim
FROM python:3.12-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем только файлы зависимостей сначала (для кэширования)
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код приложения
COPY . .

# Экспортируем порт для Fly.io
EXPOSE 8080

# Переменные окружения (опционально, можно задавать через Fly secrets)
# ENV TELEGRAM_TOKEN=your_token
# ENV TELEGRAM_CHAT_ID=your_chat_id
# ENV BYBIT_API_KEY=your_key
# ENV BYBIT_API_SECRET=your_secret
# ENV TRADE_USD=25
# ENV SYMBOL=SOLUSDT
# ENV MIN_PROFIT_USDT=0.1
# ENV BYBIT_TESTNET=True
# ENV TRADE_TYPE=futures
# ENV LEVERAGE=1

# Команда для запуска FastAPI через Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]


