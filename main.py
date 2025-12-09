# =========================================================================================
# OZ TRADING BOT 2025 v1.1.4 | БЕЗОПАСНОЕ ЛОГИРОВАНИЕ ОШИБОК BINANCE
# =========================================================================================
import os
import time
import hmac
import hashlib
from typing import Dict, Set
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
        # В случае отсутствия переменной, прерываем выполнение
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
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "0.5")) # Процент отката для Trailing Stop

# Инициализация Telegram и HTTP клиента
bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20)
BASE = "https://fapi.binance.com"

# Множества для отслеживания активных позиций (для пропуска повторных сигналов)
active_longs: Set[str] = set() # Активные LONG-позиции
active_shorts: Set[str] = set() # Активные SHORT-позиции

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

        def format_value(v):
            if isinstance(v, bool):
                return str(v).lower()
            return str(v)

        query_parts = [f"{k}={format_value(v)}" for k, v in sorted(p.items())]
        query_string = "&".join(query_parts)

        signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

        url = f"{url}?{query_string}&signature={signature}"
        
        final_params = None
    
    headers = {"X-MBX-APIKEY": API_KEY}
    
    try:
        r = await client.request(method, url, params=final_params, headers=headers)
        
        if r.status_code != 200:
            is_benign_margin_error = (
                path == "/fapi/v1/marginType" and 
                r.status_code == 400 and 
                '{"code":-1102,' in r.text
            )
            
            if not is_benign_margin_error:
                # Отправляем полный текст ошибки (до 3800 символов)
                err = r.text if len(r.text) < 3800 else r.text[:3800] + "..."
                await tg(f"<b>BINANCE ERROR {r.status_code}</b>\nPath: {path}\n<code>{err}</code>")
            
            return None
        
        # Попытка вернуть JSON. Если не JSON (например, HTML с кодом 200), вернем текст для дальнейшего логирования.
        try:
            return r.json()
        except Exception:
            return r.text
            
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None


# ================ ЗАГРУЗКА АКТИВНЫХ ПОЗИЦИЙ ====================
async def load_active_positions():
    """Загружает открытые LONG и SHORT позиции с Binance в соответствующие множества при старте."""
    global active_longs, active_shorts
    try:
        data = await binance("GET", "/fapi/v2/positionRisk", signed=True)
        if data:
            open_longs_temp = set()
            open_shorts_temp = set()
            
            for p in data:
                amt = float(p["positionAmt"])
                if amt > 0 and p["positionSide"] == "LONG":
                    open_longs_temp.add(p["symbol"])
                elif amt < 0 and p["positionSide"] == "SHORT":
                    open_shorts_temp.add(p["symbol"])

            active_longs = open_longs_temp
            active_shorts = open_shorts_temp
            
            await tg(f"<b>Начальная загрузка позиций:</b>\nНайдено {len(active_longs)} LONG и {len(active_shorts)} SHORT позиций.")
    except Exception as e:
        await tg(f"<b>Ошибка при загрузке активных позиций:</b> {e}")


# ================ ОКРУГЛЕНИЕ КОЛИЧЕСТВА =======================
def fix_qty(symbol: str, qty: float) -> str:
    """
    Округляет количество в зависимости от символа, учитывая точность Binance.
    ИСПРАВЛЕНО: AVAXUSDT и NEARUSDT перенесены в группу по умолчанию (3 знака).
    """
    # Список пар, где количество должно быть ЦЕЛЫМ числом (0 знаков)
    zero_prec = [
        "DOGEUSDT", "1000SHIBUSDT", "1000PEPEUSDT", "1000BONKUSDT", 
        "1000FLOKIUSDT", "1000SATSUSDT", "FARTCOINUSDT", "XRPUSDT", 
        "BTTUSDT" # NEARUSDT УДАЛЕН ИЗ ЭТОГО СПИСКА
    ]
    # Список пар, где количество округляется до 2-х знаков
    two_prec = [
        "SOLUSDT", "ADAUSDT", "TRXUSDT", "MATICUSDT", "DOTUSDT", 
        "ATOMUSDT", "BNBUSDT", "LINKUSDT" # AVAXUSDT УДАЛЕН ИЗ ЭТОГО СПИСКА
    ]
    
    if symbol.upper() in zero_prec:
        # Для этих пар количество должно быть целым числом
        return str(int(qty))
    
    if symbol.upper() in two_prec:
        return f"{qty:.2f}".rstrip("0").rstrip(".")

    # Для остальных пар (ETHUSDT, MASKUSDT, PIPPINUSDT, AVAXUSDT, NEARUSDT) оставляем 3 знака по умолчанию. 
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ ФУНКЦИИ ОТКРЫТИЯ =======================

async def get_symbol_and_qty(sym: str) -> tuple[str, str, float] | None:
    """Вспомогательная функция для получения символа, цены и рассчитанного количества."""
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    
    # 1. Установка Cross Margin и плеча
    await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})

    # 2. Получение цены
    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data or 'price' not in price_data:
        await tg(f"<b>Ошибка:</b> Не удалось получить цену для {symbol}")
        return None
        
    price = float(price_data["price"])
    qty_f = AMOUNT * LEV / price
    qty_str = fix_qty(symbol, qty_f)
    
    return symbol, qty_str, price

async def open_long(sym: str):
    # Преобразуем входящий символ (например, DOGE/USDT) в формат Binance (DOGEUSDT)
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    
    if symbol in active_longs:
        await tg(f"<b>{symbol}</b> — LONG уже открыта (пропуск сигнала)")
        return

    # 3. Открытие LONG позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "positionSide": "LONG", # LONG позиция
        "type": "MARKET",
        "quantity": qty_str
    })

    if order and order.get("orderId"):
        active_longs.add(symbol)
        
        # 4. Размещение TRAILING_STOP_MARKET ордера (SELL для закрытия LONG)
        # ИСПРАВЛЕНИЕ v1.1.1: Используем /fapi/v1/order/algo для Trailing Stop
        trailing_order = await binance("POST", "/fapi/v1/order/algo", { 
            "symbol": symbol, 
            "side": "SELL",
            "positionSide": "LONG",
            "type": "TRAILING_STOP_MARKET",
            "quantity": qty_str,
            "callbackRate": TRAILING_RATE,
        })

        if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("orderId")):
            await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
        else:
            # ИСПРАВЛЕНИЕ v1.1.4: Безопасное логирование ответа Binance
            log_detail = str(trailing_order) if trailing_order else "Пустой или None ответ от Binance"
            
            # Проверяем, не является ли ответ HTML-мусором (начинается с <)
            if log_detail.strip().startswith("<"):
                 log_text = f"ОТВЕТ В ФОРМАТЕ HTML. Обрезан лог: {log_detail[:100]}..."
            else:
                 # Если это JSON-ответ или чистый текст ошибки, отправляем полный лог
                 log_text = log_detail
            
            await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP (СМОТРИТЕ ЛОГ)\n<pre>{log_text}</pre>")
    else:
        await tg(f"<b>Ошибка открытия LONG {symbol}</b>")

# НОВАЯ ФУНКЦИЯ ДЛЯ ОТКРЫТИЯ SHORT
async def open_short(sym: str):
    # Преобразуем входящий символ (например, DOGE/USDT) в формат Binance (DOGEUSDT)
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    
    if symbol in active_shorts:
        await tg(f"<b>{symbol}</b> — SHORT уже открыта (пропуск сигнала)")
        return

    # 3. Открытие SHORT позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL", # Продаем (открываем SHORT)
        "positionSide": "SHORT", # SHORT позиция
        "type": "MARKET",
        "quantity": qty_str
    })

    if order and order.get("orderId"):
        active_shorts.add(symbol)
        
        # 4. Размещение TRAILING_STOP_MARKET ордера (BUY для закрытия SHORT)
        # ИСПРАВЛЕНИЕ v1.1.1: Используем /fapi/v1/order/algo для Trailing Stop
        trailing_order = await binance("POST", "/fapi/v1/order/algo", { 
            "symbol": symbol, 
            "side": "BUY", # Покупаем (закрываем SHORT позицию)
            "positionSide": "SHORT",
            "type": "TRAILING_STOP_MARKET",
            "quantity": qty_str,
            "callbackRate": TRAILING_RATE,
        })

        if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("orderId")):
            await tg(f"<b>SHORT ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
        else:
            # ИСПРАВЛЕНИЕ v1.1.4: Безопасное логирование ответа Binance
            log_detail = str(trailing_order) if trailing_order else "Пустой или None ответ от Binance"
            
            # Проверяем, не является ли ответ HTML-мусором (начинается с <)
            if log_detail.strip().startswith("<"):
                 log_text = f"ОТВЕТ В ФОРМАТЕ HTML. Обрезан лог: {log_detail[:100]}..."
            else:
                 # Если это JSON-ответ или чистый текст ошибки, отправляем полный лог
                 log_text = log_detail

            await tg(f"<b>SHORT ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP (СМОТРИТЕ ЛОГ)\n<pre>{log_text}</pre>")

    else:
        await tg(f"<b>Ошибка открытия SHORT {symbol}</b>")


# ================= ФУНКЦИИ ЗАКРЫТИЯ ==========================
async def close_position(sym: str, position_side: str, active_set: Set[str]):
    """Универсальная функция для закрытия LONG или SHORT позиции."""
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    
    # 1. Отмена всех активных ордеров (включая Trailing Stop)
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    
    # 2. Проверка текущей позиции на бирже
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data:
        await tg(f"<b>{symbol}</b> — Не удалось получить данные о позиции.")
        return
    
    # Ищем позицию с указанным position_side
    qty_str = next((p["positionAmt"] for p in pos_data if p["positionSide"] == position_side and abs(float(p["positionAmt"])) > 0), None)
    
    if not qty_str or float(qty_str) == 0:
        # Если позиции нет, очищаем внутренний список и уведомляем
        active_set.discard(symbol)
        await tg(f"<b>{position_side} {symbol}</b> — позиция уже закрыта на бирже")
        return

    # Определяем сторону ордера для закрытия
    # Для LONG (positionSide=LONG, positionAmt > 0) закрывающий ордер: SELL
    # Для SHORT (positionSide=SHORT, positionAmt < 0) закрывающий ордер: BUY
    close_side = "SELL" if position_side == "LONG" else "BUY"
    
    # Количество для закрытия должно быть положительным
    qty_to_close = fix_qty(symbol, abs(float(qty_str)))

    # 3. Закрытие позиции (Market)
    close_order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": qty_to_close,
    })
    
    if close_order and close_order.get("orderId"):
        active_set.discard(symbol)
        await tg(f"<b>CLOSE {position_side} {symbol} УСПЕШНО</b>\n{qty_to_close} шт")
    else:
        await tg(f"<b>CRITICAL ERROR: Не удалось закрыть {position_side} {symbol}</b>")

async def close_long(sym: str):
    await close_position(sym, "LONG", active_longs)

async def close_short(sym: str):
    await close_position(sym, "SHORT", active_shorts)

# ================= FASTAPI ПРИЛОЖЕНИЕ =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Загрузка активных позиций при старте
    await load_active_positions()
    
    await tg("<b>OZ BOT 2025 — ONLINE (v1.1.4)</b>\nИсправлена точность NEARUSDT и ошибка парсинга логов.")
    yield
    await client.aclose() # Закрываем HTTP клиент при завершении работы

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE (v1.1.4)</h1>")

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
    signal = data.get("signal", "").upper() # Здесь мы принимаем сигнал от сканера

    if not symbol or not signal:
        raise HTTPException(status_code=400, detail="Missing symbol or signal in payload")

    # ================== ИСПРАВЛЕННАЯ ЛОГИКА ОБРАБОТКИ СИГНАЛОВ ==================
    if signal == "LONG":
        asyncio.create_task(open_long(symbol))
    elif signal == "CLOSE_LONG": # Обрабатывает сигнал закрытия LONG от сканера
        asyncio.create_task(close_long(symbol))
    elif signal == "SHORT": # Обрабатывает сигнал открытия SHORT
        asyncio.create_task(open_short(symbol))
    elif signal == "CLOSE_SHORT": # Обрабатывает сигнал закрытия SHORT
        asyncio.create_task(close_short(symbol))
    else:
        # Неизвестный сигнал
        print(f"[WARNING] Получен неизвестный сигнал: {signal} для {symbol}")
        return {"ok": False, "message": f"Unknown signal: {signal}"}
    # ============================================================================

    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
