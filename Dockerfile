# --- Базовый образ ---
FROM python:3.12-slim

# --- Рабочая директория ---
WORKDIR /app

# --- Установка зависимостей ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Копируем код бота ---
COPY . .

# --- Открываем порт ---
EXPOSE 8080

# --- Команда запуска ---
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
