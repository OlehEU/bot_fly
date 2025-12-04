# =========================================================================================
# OZ TRADING BOT 2025 v1.0 | Бот для исполнения сигналов Сканера на Binance Futures
# =========================================================================================
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

# ==================== КОНФИГУРАЦИЯ ====================
# Проверка наличия всех необходимых переменных окружения
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        raise ValueError(f"Нет переменной окружения: {v}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
try:
    CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
except (ValueError, TypeError):
    raise ValueError("TELEGRAM_CHAT_ID должен быть целым числом.")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30")) # Объем сделки в USD
LEV = int(os.getenv("LEVERAGE", "10")) # Плечо
# Процент отката для Trailing Stop. Установите, например, 0.5 для 0.5%
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "0.5")) 

# Инициализация Telegram и HTTP клиента
bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20)
BASE = "https://fapi.binance.com"
# Множество для отслеживания активных LONG-позиций, открытых ботом
active = set() 

# ================= TELEGRAM УВЕДОМЛЕНИЯ =====================
async def tg(text: str):
    """Отправляет сообщение в Telegram, используя HTML форматирование."""
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")

# ================= BINANCE API ЗАПРОСЫ ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    """
    Универсальная функция для запросов к API Binance Futures. 
    Автоматически добавляет подпись (signature) и временную метку (timestamp).
    """
    url = BASE + path
    p = params.copy() if params else {}
    
    final_params = p
    
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000

        # Функция для корректного форматирования булевых значений
        def format_value(v):
            if isinstance(v, bool):
                return str(v).lower()
            return str(v)

        # 1. Сортируем параметры по ключу и собираем строку запроса вручную
        query_parts = [f"{k}={format_value(v)}" for k, v in sorted(p.items())]
        query_string = "&".join(query_parts)

        # 2. Генерируем подпись
        signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

        # 3. Добавляем строку прямо в URL
        url = f"{url}?{query_string}&signature={signature}"
        
        final_params = None # Передаем параметры через URL, а не через params=...
    
    headers = {"X-MBX-APIKEY": API_KEY}
    
    try:
        r = await client.request(method, url, params=final_params, headers=headers)
        
        if r.status_code != 200:
            # Игнорируем некритическую ошибку -1102 (уже установленный marginType)
            is_benign_margin_error = (
                path == "/fapi/v1/marginType" and 
                r.status_code == 400 and 
                '{"code":-1102,' in r.text
            )
            
            if not is_benign_margin_error:
                err = r.text if len(r.text) < 3800 else r.text[:3800] + "..."
                await tg(f"<b>BINANCE ERROR {r.status_code}</b>\nURL: <code>{url}</code>\nPath: {path}\n<code>{err}</code>")
            
            return None
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ ЗАГРУЗКА АКТИВНЫХ ПОЗИЦИЙ ====================
async def load_active_positions():
    """Загружает открытые LONG позиции с Binance в active set при старте."""
    global active
    try:
        data = await binance("GET", "/fapi/v2/positionRisk", signed=True)
        if data:
            # Фильтруем только LONG позиции с количеством > 0
            open_longs = {
                p["symbol"] for p in data 
                if float(p["positionAmt"]) > 0 and p["positionSide"] == "LONG"
            }
            active = open_longs
            await tg(f"<b>Начальная загрузка позиций:</b>\nНайдено {len(active)} открытых LONG-позиций.")
    except Exception as e:
        await tg(f"<b>Ошибка при загрузке активных позиций:</b> {e}")


# ================ ОКРУГЛЕНИЕ КОЛИЧЕСТВА =======================
def fix_qty(symbol: str, qty: float) -> str:
    """Округляет количество в зависимости от символа, учитывая точность Binance.
    
    ИСПРАВЛЕНИЕ: Добавлена явная обработка для BNBUSDT, требующей 2 знака после запятой.
    """
    # Монеты, требующие нулевой точности (целые числа: мемкоины, 1000X токены и т.д.).
    # Если возникает ошибка "Precision is over the maximum defined", добавьте сюда новый символ.
    zero_prec = ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT","1000SATSUSDT", "FARTCOINUSDT"]
    # Монеты, требующие точности 2 знака после запятой (SOL, ADA, MATIC, DOT, ATOM, BNB и т.д.)
    two_prec = ["SOLUSDT", "ADAUSDT", "TRXUSDT", "MATICUSDT", "DOTUSDT", "ATOMUSDT", "BNBUSDT"]
    
    if symbol in zero_prec:
        # Используем int() для целого числа
        return str(int(qty))
    
    if symbol in two_prec:
        # 2 знака после запятой
        return f"{qty:.2f}".rstrip("0").rstrip(".")

    # Для остальных пар (ETH, XRP) оставляем 3 знака по умолчанию
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ ОТКРЫТИЕ LONG =======================
async def open_long(sym: str):
    # Преобразуем входящий символ (например, DOGE/USDT) в формат Binance (DOGEUSDT)
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"

    if symbol in active:
        await tg(f"<b>{symbol}</b> — уже открыта (пропуск сигнала)")
        return

    # 1. Установка Cross Margin. 
    await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    
    # 2. Установка плеча
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})

    # 3. Расчет количества
    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data or 'price' not in price_data:
        await tg(f"<b>Ошибка:</b> Не удалось получить цену для {symbol}")
        return
        
    price = float(price_data["price"])
    # Расчет количества: (AMOUNT * LEVERAGE) / PRICE
    qty_f = AMOUNT * LEV / price
    qty_str = fix_qty(symbol, qty_f)

    # 4. Открытие LONG позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "positionSide": "LONG", # LONG позиция
        "type": "MARKET",
        "quantity": qty_str
    })

    if order and order.get("orderId"):
        active.add(symbol)
        
        # 5. Размещение TRAILING_STOP_MARKET ордера
        trailing_order = await binance("POST", "/fapi/v1/order", {
            "symbol": symbol, 
            "side": "SELL", # Закрывает LONG позицию
            "positionSide": "LONG",
            "type": "TRAILING_STOP_MARKET",
            "quantity": qty_str, # Объем позиции
            "callbackRate": TRAILING_RATE, # Процент отката
            # Примечание: "reduceOnly" удален, чтобы избежать ошибки -1106,
            # так как позиция открывается в режиме Hedge Mode.
        })

        if trailing_order and trailing_order.get("orderId"):
            await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
        else:
             # Если трейлинг-стоп не установился, все равно уведомляем об открытии позиции
             await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP (СМОТРИТЕ ЛОГ)")

    else:
        await tg(f"<b>Ошибка открытия {symbol}</b>")

# ================= ЗАКРЫТИЕ ПОЗИЦИИ ==========================
async def close(sym: str):
    # Преобразуем входящий символ (например, DOGE/USDT) в формат Binance (DOGEUSDT)
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    
    # 1. Отмена всех активных ордеров (включая Trailing Stop) для данного символа
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    
    # 2. Проверка текущей позиции на бирже
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data:
        await tg(f"<b>{symbol}</b> — Не удалось получить данные о позиции.")
        return
    
    # Ищем LONG позицию с количеством > 0
    qty_str = next((p["positionAmt"] for p in pos_data if p["positionSide"] == "LONG" and float(p["positionAmt"]) > 0), None)
    
    if not qty_str or float(qty_str) <= 0:
        # Если позиции нет, очищаем внутренний список и уведомляем
        active.discard(symbol)
        await tg(f"<b>{symbol}</b> — позиция уже закрыта на бирже")
        return

    # 3. Закрытие LONG позиции (Market)
    close_order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, # Важно: символ-специфичное закрытие
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty_str,
    })
    
    if close_order and close_order.get("orderId"):
        active.discard(symbol)
        await tg(f"<b>CLOSE {symbol} УСПЕШНО</b>\n{qty_str} шт")
    else:
        await tg(f"<b>CRITICAL ERROR: Не удалось закрыть {symbol}</b>")

# ================= FASTAPI ПРИЛОЖЕНИЕ =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Загрузка активных позиций при старте
    await load_active_positions()
    
    await tg("<b>OZ BOT 2025 — ONLINE</b>\nCross Mode | Hedge Mode FIXED\nTrailing Stop (TSL/TTP) АКТИВИРОВАН")
    yield
    await client.aclose() # Закрываем HTTP клиент при завершении работы

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE</h1>")

@app.post("/webhook")
async def webhook(request: Request):
    # Проверка секрета вебхука (критически важно для безопасности)
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

    # Запуск торговых операций в фоновом режиме
    if signal == "LONG":
        asyncio.create_task(open_long(symbol))
    elif signal == "CLOSE":
        asyncio.create_task(close(symbol))

    return {"ok": True}
