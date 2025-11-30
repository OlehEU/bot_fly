import os
import time
import hmac
import hashlib
from typing import Dict
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# ==================== CONFIG ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        # Используем ValueError вместо EnvironmentError для более чистого лога
        raise ValueError(f"Нет переменной окружения: {v}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# Преобразование в int сразу
try:
    CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
except (ValueError, TypeError):
    raise ValueError("TELEGRAM_CHAT_ID должен быть целым числом.")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20)
BASE = "https://fapi.binance.com"
# Сет для отслеживания активных позиций (в памяти)
active = set()

# ================= TELEGRAM =====================
async def tg(text: str):
    """Отправляет сообщение в Telegram, используя HTML форматирование."""
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        # Просто игнорируем ошибки отправки, чтобы не останавливать торговлю
        pass

# ================= BINANCE API ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    """
    Универсальная функция для запросов к API Binance Futures.
    Исправлена логика подписи для избежания ошибки -1022.
    """
    url = BASE + path
    p = params.copy() if params else {}
    
    # Инициализируем final_params для httpx (используется, если signed=False)
    final_params = p
    
    # Логика подписи
    if signed:
        # Добавляем стандартные параметры для подписанных запросов
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000

        # 1. Сортируем параметры по ключу и собираем строку запроса вручную
        # Это критически важно для согласованности подписи.
        query_parts = [f"{k}={v}" for k, v in sorted(p.items())]
        query_string = "&".join(query_parts)

        # 2. Генерируем подпись на основе этой строки
        signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

        # 3. Формируем финальную строку запроса с подписью
        full_query = f"{query_string}&signature={signature}"

        # 4. Добавляем строку прямо в URL. Это гарантирует, что httpx не изменит порядок
        # (используется для GET и POST с параметрами в query string, что разрешено Binance)
        url = f"{url}?{full_query}"
        
        # Обнуляем final_params, так как все параметры уже в URL
        final_params = None
    
    headers = {"X-MBX-APIKEY": API_KEY}
    
    try:
        # Используем final_params (который None для signed запросов)
        r = await client.request(method, url, params=final_params, headers=headers)
        
        if r.status_code != 200:
            err = r.text if len(r.text) < 3800 else r.text[:3800] + "..."
            # Улучшенный лог с указанием URL
            await tg(f"<b>BINANCE ERROR {r.status_code}</b>\nURL: <code>{url}</code>\nPath: {path}\n<code>{err}</code>")
            return None
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ QTY ROUND =======================
def fix_qty(symbol: str, qty: float) -> str:
    """
    Округляет количество в зависимости от символа.
    ВНИМАНИЕ: Для продакшена лучше использовать exchangeInfo для точного stepSize.
    """
    high_prec = ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT","1000SATSUSDT"]
    if symbol in high_prec:
        # Для монет с очень низкой ценой (шаг 1 или больше)
        return str(int(qty))
    # Для большинства других монет (3 знака после запятой)
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ OPEN LONG =======================
async def open_long(sym: str):
    # Унификация символа
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"

    if symbol in active:
        await tg(f"<b>{symbol}</b> — уже открыта")
        return

    # 1. Установка Cross Margin (должна быть выполнена, даже если есть ошибка)
    # *Важно: если аккаунт в One-Way mode, это может не потребоваться или вызвать ошибку,
    # но для Hedge Mode это стандартный шаг.*
    margin_res = await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    if margin_res is None and not symbol in active:
        # Если не удалось установить, но ошибка не критическая (например, уже CROSS),
        # пробуем продолжить. Если ошибка критическая (-1022), она уже будет в логе.
        pass

    # 2. Установка кредитного плеча
    lev_res = await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})
    if lev_res is None and not symbol in active:
        # Если не удалось установить, это не блокирует позицию, но лог есть.
        pass

    # 3. Получение цены и расчет количества
    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data or 'price' not in price_data:
        await tg(f"<b>Ошибка получения цены для {symbol}</b>")
        return
        
    price = float(price_data["price"])
    # Расчет количества: (USD * LEV) / PRICE = QTY
    qty = fix_qty(symbol, AMOUNT * LEV / price)

    # 4. Открытие LONG позиции
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty
    })

    if order and order.get("orderId"):
        active.add(symbol)
        await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty} шт ≈ ${AMOUNT}\n@ {price:.8f}")
    else:
        await tg(f"<b>Ошибка открытия {symbol}</b>")

# ================= CLOSE ==========================
async def close(sym: str):
    # Унификация символа
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    
    # Проверка активности в памяти
    if symbol not in active:
        return

    # 1. Проверка текущей позиции
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data:
        return
    
    # Ищем LONG позицию с количеством > 0
    qty = next((p["positionAmt"] for p in pos_data if p["positionSide"] == "LONG" and float(p["positionAmt"]) > 0), None)
    
    if not qty:
        # Позиция уже закрыта или не найдена
        active.discard(symbol)
        return

    # 2. Закрытие LONG позиции
    close_order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true" # Гарантирует, что это не откроет шорт-позицию
    })
    
    if close_order and close_order.get("orderId"):
        active.discard(symbol)
        await tg(f"<b>CLOSE {symbol}</b>\n{qty} шт")
    else:
        await tg(f"<b>Ошибка закрытия {symbol}</b>")

# ================= FASTAPI =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск бота и отправка приветствия
    await tg("<b>OZ BOT 2025 — ONLINE</b>\nCross Mode | Hedge Mode FIXED\nОшибки Binance → полные")
    yield
    # Очистка ресурсов при выключении
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    # Проверка секрета вебхука для безопасности
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    symbol = data.get("symbol", "").upper()
    signal = data.get("signal", "").upper()

    if not symbol or not signal:
        raise HTTPException(status_code=400, detail="Missing symbol or signal in payload")

    # Запускаем задачи асинхронно, чтобы не блокировать вебхук
    if signal == "LONG":
        asyncio.create_task(open_long(symbol))
    elif signal == "CLOSE":
        asyncio.create_task(close(symbol))
    # Добавьте сюда обработку SHORT / CLOSE_SHORT, если используете

    return {"ok": True}

# ================= END OF CODE =====================
