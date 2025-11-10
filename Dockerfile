# --- 1. Используем официальный Python 3.12 slim ---
FROM python:3.12-slim

# --- 2. Устанавливаем зависимости для сборки и системы ---
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# --- 3. Создаем рабочую директорию ---
WORKDIR /app

# --- 4. Копируем файлы проекта ---
COPY requirements.txt .
COPY main.py .

# --- 5. Устанавливаем Python зависимости ---
RUN pip install --no-cache-dir -r requirements.txt

# --- 6. Экспонируем порт FastAPI (Fly автоматически пробрасывает 8080) ---
EXPOSE 8080

# --- 7. Команда запуска ---
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

