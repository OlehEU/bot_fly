# Dockerfile
FROM python:3.11-slim

# Устанавливаем зависимости
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Запускаем
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
