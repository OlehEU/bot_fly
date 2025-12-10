# =========================================================================================
# OZ TRADING BOT 2025 v1.5.0 | ИСПРАВЛЕНИЕ МЕНЮ: Переход на Telegram Webhook (без Polling)
# =========================================================================================
import os
import time
import hmac
import hashlib
import json
from typing import Dict, Set
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
# Импортируем необходимые классы для Telegram-бота
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, constants
from telegram.error import TelegramError
from contextlib import asynccontextmanager

# ==================== КОНФИГУРАЦИЯ ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET", "PUBLIC_HOST_URL"]
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
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL").rstrip('/') # Убираем слэш в конце
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30")) # Объем сделки в USD
LEV = int(os.getenv("LEVERAGE", "10")) # Плечо
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "0.5")) # Процент отката для Trailing Stop

# Инициализация HTTP клиента
client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"

# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
symbol_precision: Dict[str, int] = {} 
active_longs: Set[str] = set() 
active_shorts: Set[str] = set() 
active_trailing_enabled: bool = os.getenv("TRAILING_ENABLED", "true").lower() in ('true', '1', 't')

# Инициализация Telegram Bot для всех функций
tg_bot = Bot(token=TELEGRAM_TOKEN) 

# ================= TELEGRAM УВЕДОМЛЕНИЯ (Не изменены) =====================
async def tg(text: str):
    """Отправляет сообщение в Telegram, используя HTML форматирование."""
    try:
        await tg_bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"[ERROR] Telegram send failed (HTML parse error). Sending as plain text: {e}")
        try:
             clean_text = text.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', '').replace('<pre>', '\n').replace('</pre>', '\n').replace('&nbsp;', ' ')
             await tg_bot.send_message(CHAT_ID, clean_text, disable_web_page_preview=True)
        except Exception as plain_e:
             print(f"[CRITICAL ERROR] Telegram send failed even as plain text: {plain_e}")

# ================= BINANCE API ЗАПРОСЫ (Не изменены) ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    """Универсальная функция для запросов к API Binance Futures."""
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
            err_text = r.text if len(r.text) < 3800 else r.text[:3800] + "..."
            
            if r.status_code != 400 or '{"code":-1102,' not in r.text:
                await tg(f"<b>BINANCE ERROR {r.status_code}</b>\nPath: {path}\n<code>{err_text}</code>")
            
            return None
        
        try:
            return r.json()
        except Exception:
            return r.text
            
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Загрузка инфо, позиций, округление) ================
# ... (Оставим эти функции без изменений для экономии места)
def calculate_precision_from_stepsize(step_size: str) -> int:
    s = step_size.rstrip('0')
    if '.' not in s: return 0
    return len(s.split('.')[-1])

async def load_exchange_info():
    global symbol_precision
    try:
        data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
        if not data or not isinstance(data, dict) or 'symbols' not in data:
            await tg("<b>Ошибка:</b> Не удалось загрузить информацию о бинарных символах.")
            return
        for symbol_info in data['symbols']:
            sym = symbol_info['symbol']
            lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size_filter:
                step_size = lot_size_filter['stepSize']
                precision = calculate_precision_from_stepsize(step_size)
                symbol_precision[sym] = precision
        await tg(f"<b>Загружена информация о бинарных символах:</b> Точность определена для {len(symbol_precision)} пар.")
    except Exception as e:
        await tg(f"<b>Критическая ошибка при загрузке exchangeInfo:</b> {e}")


async def load_active_positions():
    global active_longs, active_shorts
    try:
        data = await binance("GET", "/fapi/v2/positionRisk", signed=True)
        if data and isinstance(data, list):
            open_longs_temp = set()
            open_shorts_temp = set()
            for p in data:
                amt = float(p.get("positionAmt", 0))
                if amt > 0 and p.get("positionSide") == "LONG":
                    open_longs_temp.add(p["symbol"])
                elif amt < 0 and p.get("positionSide") == "SHORT":
                    open_shorts_temp.add(p["symbol"])

            active_longs = open_longs_temp
            active_shorts = open_shorts_temp
            await tg(f"<b>Начальная загрузка позиций:</b>\nНайдено {len(active_longs)} LONG и {len(active_shorts)} SHORT позиций.")
        elif data:
             await tg(f"<b>Ошибка при загрузке активных позиций:</b> Некорректный ответ Binance:\n<pre>{str(data)[:1500]}</pre>")
    except Exception as e:
        await tg(f"<b>Ошибка при загрузке активных позиций:</b> {e}")


def fix_qty(symbol: str, qty: float) -> str:
    precision = symbol_precision.get(symbol.upper(), 3)
    if precision == 0: return str(int(qty)) 
    return f"{qty:.{precision}f}".rstrip("0").rstrip(".")

async def get_symbol_and_qty(sym: str) -> tuple[str, str, float] | None:
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})

    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data or 'price' not in price_data:
        await tg(f"<b>Ошибка:</b> Не удалось получить цену для {symbol}")
        return None
        
    price = float(price_data["price"])
    qty_f = AMOUNT * LEV / price
    qty_str = fix_qty(symbol, qty_f)
    return symbol, qty_str, price 
# ================ КОНЕЦ ВСПОМОГАТЕЛЬНЫХ ФУНКЦИЙ ===============================


# ================= ФУНКЦИИ ОТКРЫТИЯ/ЗАКРЫТИЯ (Логика не изменена) =======================
# ... (open_long, open_short, close_position, close_long, close_short - без изменений)
async def open_long(sym: str):
    global active_trailing_enabled
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    is_open_on_exchange = False
    if pos_data and isinstance(pos_data, list):
        if next((p for p in pos_data if p.get("positionSide") == "LONG" and float(p.get("positionAmt", 0)) > 0), None):
            is_open_on_exchange = True
            
    if is_open_on_exchange:
        active_longs.add(symbol) 
        await tg(f"<b>{symbol}</b> — LONG уже открыта на бирже. Пропуск.")
        return

    active_longs.discard(symbol) 
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": qty_str
    })

    if order and order.get("orderId"):
        active_longs.add(symbol)
        rate_str = f"{TRAILING_RATE:.2f}" 
        activation_price_str = f"{price:.8f}".rstrip("0").rstrip(".") 
        await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт...\n@ {price:.8f}\n\nПопытка установить Trailing Stop.")
        
        if active_trailing_enabled:
            trailing_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": "TRAILING_STOP_MARKET", "quantity": qty_str, "callbackRate": rate_str, "activationPrice": activation_price_str, 
            })
            if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("algoId")):
                await tg(f"<b>LONG ×{LEV} (Cross+Hedge) {symbol}</b>\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
            else:
                log_detail = str(trailing_order) if trailing_order else "Пустой или None ответ от Binance"
                await tg(f"<b>LONG ×{LEV} (Cross+Hedge) {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP\n<pre>{log_detail[:200]}</pre>")
        else:
             await tg(f"<b>LONG ×{LEV} (Cross+Hedge) {symbol}</b>\n🚫 TRAILING STOP ОТКЛЮЧЕН настройкой бота.")
    else:
        await tg(f"<b>Ошибка открытия LONG {symbol}</b>")

async def open_short(sym: str):
    global active_trailing_enabled
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    is_open_on_exchange = False
    if pos_data and isinstance(pos_data, list):
        if next((p for p in pos_data if p.get("positionSide") == "SHORT" and float(p.get("positionAmt", 0)) < 0), None):
            is_open_on_exchange = True

    if is_open_on_exchange:
        active_shorts.add(symbol) 
        await tg(f"<b>{symbol}</b> — SHORT уже открыта на бирже. Пропуск.")
        return

    active_shorts.discard(symbol) 
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": qty_str
    })

    if order and order.get("orderId"):
        active_shorts.add(symbol)
        rate_str = f"{TRAILING_RATE:.2f}"
        activation_price_str = f"{price:.8f}".rstrip("0").rstrip(".") 
        await tg(f"<b>SHORT ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт...\n@ {price:.8f}\n\nПопытка установить Trailing Stop.")

        if active_trailing_enabled:
            trailing_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
                "type": "TRAILING_STOP_MARKET", "quantity": qty_str, "callbackRate": rate_str, "activationPrice": activation_price_str, 
            })
            if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("algoId")):
                await tg(f"<b>SHORT ×{LEV} (Cross+Hedge) {symbol}</b>\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
            else:
                log_detail = str(trailing_order) if trailing_order else "Пустой или None ответ от Binance"
                await tg(f"<b>SHORT ×{LEV} (Cross+Hedge) {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP\n<pre>{log_detail[:200]}</pre>")
        else:
            await tg(f"<b>SHORT ×{LEV} (Cross+Hedge) {symbol}</b>\n🚫 TRAILING STOP ОТКЛЮЧЕН настройкой бота.")
    else:
        await tg(f"<b>Ошибка открытия SHORT {symbol}</b>")

async def close_position(sym: str, position_side: str, active_set: Set[str]):
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data: await tg(f"<b>{symbol}</b> — Не удалось получить данные о позиции."); return
    qty_str = next((p["positionAmt"] for p in pos_data if p["positionSide"] == position_side and abs(float(p["positionAmt"])) > 0), None)
    if not qty_str or float(qty_str) == 0:
        active_set.discard(symbol)
        await tg(f"<b>{position_side} {symbol}</b> — позиция уже закрыта на бирже"); return
    close_side = "SELL" if position_side == "LONG" else "BUY"
    qty_to_close = fix_qty(symbol, abs(float(qty_str)))
    close_order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": close_side, "positionSide": position_side, "type": "MARKET", "quantity": qty_to_close,
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
# ================= КОНЕЦ ФУНКЦИЙ ОТКРЫТИЯ/ЗАКРЫТИЯ =======================


# ==================== TELEGRAM WEBHOOK HANDLER (НОВЫЙ БЛОК) =====================

def create_trailing_menu(current_status: bool):
    """Создает клавиатуру для меню Trailing Stop."""
    status_text = "ВКЛЮЧЕН" if current_status else "ОТКЛЮЧЕН"
    
    text = f"<b>⚙️ Управление ботом</b>\n\nТекущий статус Trailing Stop: <b>{status_text}</b>\n(Комиссия: {TRAILING_RATE}%)"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Включить Трейлинг", callback_data='set_trailing_true'),
        ],
        [
            InlineKeyboardButton("❌ Отключить Трейлинг", callback_data='set_trailing_false'),
        ],
        [
            InlineKeyboardButton("🔄 Обновить статус", callback_data='refresh_trailing_status'),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    return text, reply_markup

async def handle_telegram_update(update_json: Dict):
    """
    Основная функция для обработки всех входящих сообщений и callback-запросов от Telegram.
    """
    global active_trailing_enabled
    
    update = Update.de_json(update_json, tg_bot)
    
    # 1. Обработка команд (/start, /menu)
    if update.message and update.message.text:
        message = update.message
        
        # Проверка CHAT_ID для безопасности
        if message.chat.id != CHAT_ID:
            await tg_bot.send_message(message.chat.id, "Доступ запрещен.")
            return

        text_lower = message.text.lower()
        if text_lower == '/start' or text_lower == '/menu':
            text, reply_markup = create_trailing_menu(active_trailing_enabled)
            await message.reply_html(text, reply_markup=reply_markup)
            return

    # 2. Обработка нажатий на кнопки (CallbackQuery)
    elif update.callback_query:
        query = update.callback_query
        
        # Проверка CHAT_ID для безопасности
        if query.message.chat.id != CHAT_ID:
            await query.answer("Доступ запрещен.", show_alert=True)
            return

        data = query.data
        new_state = None
        message = None
        
        if data == 'set_trailing_true':
            new_state = True
            message = "Трейлинг Стоп успешно ВКЛЮЧЕН."
        elif data == 'set_trailing_false':
            new_state = False
            message = "Трейлинг Стоп успешно ОТКЛЮЧЕН."
        
        await query.answer() # Всегда отвечаем на callback
        
        # Изменение состояния
        if new_state is not None and new_state != active_trailing_enabled:
            active_trailing_enabled = new_state
            
            # Обновление меню и отправка уведомления
            text, reply_markup = create_trailing_menu(active_trailing_enabled)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=constants.ParseMode.HTML)
            
            status = "ВКЛЮЧЕН" if active_trailing_enabled else "ОТКЛЮЧЕН"
            await tg(f"<b>⚙️ Настройка бота изменена через Telegram</b>\nТрейлинг Стоп: <b>{status}</b>")
            
        # Обновление статуса или отсутствие изменения
        elif data == 'refresh_trailing_status' or new_state is not None:
            text, reply_markup = create_trailing_menu(active_trailing_enabled)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=constants.ParseMode.HTML)


async def set_telegram_webhook(url: str):
    """
    Регистрирует Webhook URL в Telegram при старте приложения.
    """
    try:
        response = await tg_bot.set_webhook(url=url)
        if response:
            print(f"✅ Telegram Webhook установлен на: {url}")
            await tg(f"<b>Telegram Webhook установлен</b>\nURL: <code>{url}</code>")
        else:
            print(f"❌ Ошибка установки Webhook. URL: {url}")
            await tg(f"<b>Ошибка!</b> Не удалось установить Telegram Webhook на: <code>{url}</code>")
    except TelegramError as e:
        print(f"❌ Критическая ошибка Telegram API при установке Webhook: {e}")
        await tg(f"<b>Критическая ошибка Telegram API</b>\nНе удалось установить Webhook: <code>{e}</code>")


# ================= FASTAPI ПРИЛОЖЕНИЕ =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_exchange_info()
    await load_active_positions()
    
    # 1. Установка вебхука при старте
    webhook_url = f"{PUBLIC_HOST_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    await set_telegram_webhook(webhook_url)
    
    status = "ВКЛЮЧЕН" if active_trailing_enabled else "ОТКЛЮЧЕН"
    await tg(f"<b>OZ BOT 2025 — ONLINE (v1.5.0)</b>\nСистема Trailing Stop: <b>{status}</b>.\nУправление через Telegram Webhook (/menu).")
    yield
    # 2. Очистка вебхука при завершении (опционально, но рекомендуется)
    try:
        await tg_bot.delete_webhook()
        print("Telegram Webhook очищен.")
    except Exception as e:
        print(f"Ошибка при очистке вебхука: {e}")
    await client.aclose() 

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE (v1.5.0)</h1>")

# НОВЫЙ ЭНДПОИНТ ДЛЯ ПРИЕМА ВСЕХ СООБЩЕНИЙ ОТ TELEGRAM
@app.post("/telegram_webhook/{token}")
async def handle_telegram(token: str, request: Request):
    if token != TELEGRAM_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid Telegram Token")
    
    try:
        update_data = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Передаем обработку асинхронной функции
    asyncio.create_task(handle_telegram_update(update_data))
    
    # Немедленно возвращаем 200 OK, чтобы Telegram не переотправлял запрос
    return {"ok": True}

@app.post("/webhook")
async def webhook(request: Request):
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

    # ================== ЛОГИКА ОБРАБОТКИ СИГНАЛОВ ==================
    if signal == "LONG":
        asyncio.create_task(open_long(symbol))
    elif signal == "CLOSE_LONG":
        asyncio.create_task(close_long(symbol))
    elif signal == "SHORT":
        asyncio.create_task(open_short(symbol))
    elif signal == "CLOSE_SHORT":
        asyncio.create_task(close_short(symbol))
    else:
        print(f"[WARNING] Получен неизвестный сигнал: {signal} для {symbol}")
        return {"ok": False, "message": f"Unknown signal: {signal}"}
    # ============================================================================

    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    # Важно: Host=0.0.0.0 для работы в контейнере (Fly.io)
    uvicorn.run(app, host="0.0.0.0", port=8000)
