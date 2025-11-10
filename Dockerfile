FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

# Fly назначает порт через $PORT
ENV PORT=8080

# uvicorn запускается на 0.0.0.0:$PORT
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
