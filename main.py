# =========================================================================================
# OZ TRADING BOT 2025 v1.6.0 | ДОБАВЛЕНИЕ: Take Profit (1%) и кнопка управления
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
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", "1.0")) # Процент для Take Profit (1%)

# Инициализация HTTP клиента
client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"

# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
symbol_precision: Dict[str, int] = {} # Для количества (QTY)
price_precision: Dict[str, int] = {} # Для цены (TP/SL)
active_longs: Set[str] = set() 
active_shorts: Set[str] = set() 
active_trailing_enabled: bool = os.getenv("TRAILING_ENABLED", "true").lower() in ('true', '1', 't')
# НОВАЯ ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ ДЛЯ TAKE PROFIT
take_profit_enabled: bool = os.getenv("TAKE_PROFIT_ENABLED", "true").lower() in ('true', '1', 't')

# Инициализация Telegram Bot
tg_bot = Bot(token=TELEGRAM_TOKEN) 

# ================= TELEGRAM УВЕДОМЛЕНИЯ =====================
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

# ================ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ: ТОЧНОСТЬ ====================
def calculate_precision_from_stepsize(step_size: str) -> int:
    """Вычисляет необходимое количество знаков после запятой из stepSize (или tickSize)."""
    s = step_size.rstrip('0')
    if '.' not in s:
        return 0
    return len(s.split('.')[-1])

async def load_exchange_info():
    """Загружает точность (precision) для количества и цены с Binance."""
    global symbol_precision, price_precision
    try:
        data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
        
        if not data or not isinstance(data, dict) or 'symbols' not in data:
            await tg("<b>Ошибка:</b> Не удалось загрузить информацию о бинарных символах.")
            return

        for symbol_info in data['symbols']:
            sym = symbol_info['symbol']
            
            # Точность для КОЛИЧЕСТВА (LOT_SIZE)
            lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size_filter:
                step_size = lot_size_filter['stepSize']
                symbol_precision[sym] = calculate_precision_from_stepsize(step_size)
            
            # Точность для ЦЕНЫ (PRICE_FILTER) - используется для TP/SL
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if price_filter:
                tick_size = price_filter['tickSize']
                price_precision[sym] = calculate_precision_from_stepsize(tick_size)
        
        await tg(f"<b>Загружена информация о бинарных символах:</b> Точность QTY для {len(symbol_precision)} пар, PRICE для {len(price_precision)} пар.")

    except Exception as e:
        await tg(f"<b>Критическая ошибка при загрузке exchangeInfo:</b> {e}")


def fix_qty(symbol: str, qty: float) -> str:
    """Округляет количество в зависимости от динамически загруженной точности Binance."""
    precision = symbol_precision.get(symbol.upper(), 3)
    if precision == 0: 
        return str(int(qty)) 
    return f"{qty:.{precision}f}".rstrip("0").rstrip(".")

def fix_price(symbol: str, price: float) -> str:
    """Округляет цену в зависимости от динамически загруженной точности Binance (PRICE_FILTER)."""
    # Используем 8 знаков по умолчанию, если PRICE_FILTER не был найден
    precision = price_precision.get(symbol.upper(), 8) 
    return f"{price:.{precision}f}".rstrip("0").rstrip(".")

# ================ ЗАГРУЗКА АКТИВНЫХ ПОЗИЦИЙ И ЦЕНЫ ====================
async def load_active_positions():
    """Загружает открытые LONG и SHORT позиции с Binance в соответствующие множества при старте."""
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


async def get_symbol_and_qty(sym: str) -> tuple[str, str, float] | None:
    """Вспомогательная функция для получения символа, цены и рассчитанного количества."""
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

# ================= ФУНКЦИИ ОТКРЫТИЯ =======================

async def open_long(sym: str):
    global active_trailing_enabled, take_profit_enabled
    
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    
    # ... (Проверка на уже открытую позицию - для краткости опущена)
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
    # =================================================================

    # 3. Открытие LONG позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": qty_str
    })

    if order and order.get("orderId"):
        active_longs.add(symbol)
        
        rate_str = f"{TRAILING_RATE:.2f}" 
        activation_price_str = fix_price(symbol, price) 
        
        await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f}\n@ {fix_price(symbol, price)}\n")
        
        # 4. Размещение TRAILING_STOP_MARKET (Существующая логика)
        if active_trailing_enabled:
            trailing_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": "TRAILING_STOP_MARKET", "quantity": qty_str, "callbackRate": rate_str, "activationPrice": activation_price_str, 
            })

            if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("algoId")):
                await tg(f"<b>LONG {symbol}</b>\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
            else:
                await tg(f"<b>LONG {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP")
        else:
             await tg(f"<b>LONG {symbol}</b>\n🚫 TRAILING STOP ОТКЛЮЧЕН")

        # 5. Размещение TAKE_PROFIT_MARKET (НОВАЯ ЛОГИКА)
        if take_profit_enabled:
            # Цена TP = Цена открытия * (1 + Процент / 100)
            tp_price_f = price * (1 + TAKE_PROFIT_RATE / 100)
            tp_price_str = fix_price(symbol, tp_price_f) 

            tp_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": "TAKE_PROFIT_MARKET", "quantity": qty_str, "stopPrice": tp_price_str, 
            })

            if tp_order and (isinstance(tp_order, dict) and tp_order.get("algoId")):
                await tg(f"<b>LONG {symbol}</b>\n✅ TAKE PROFIT ({TAKE_PROFIT_RATE}%) УСТАНОВЛЕН @ {tp_price_str}")
            else:
                await tg(f"<b>LONG {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TAKE PROFIT")
        else:
            await tg(f"<b>LONG {symbol}</b>\n🚫 TAKE PROFIT ОТКЛЮЧЕН")

    else:
        await tg(f"<b>Ошибка открытия LONG {symbol}</b>")

async def open_short(sym: str):
    global active_trailing_enabled, take_profit_enabled
    
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    
    # ... (Проверка на уже открытую позицию - для краткости опущена)
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
    # =================================================================

    # 3. Открытие SHORT позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": qty_str
    })

    if order and order.get("orderId"):
        active_shorts.add(symbol)
        
        rate_str = f"{TRAILING_RATE:.2f}"
        activation_price_str = fix_price(symbol, price) 

        await tg(f"<b>SHORT ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f}\n@ {fix_price(symbol, price)}\n")

        # 4. Размещение TRAILING_STOP_MARKET (Существующая логика)
        if active_trailing_enabled:
            trailing_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
                "type": "TRAILING_STOP_MARKET", "quantity": qty_str, "callbackRate": rate_str, "activationPrice": activation_price_str, 
            })

            if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("algoId")):
                await tg(f"<b>SHORT {symbol}</b>\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
            else:
                await tg(f"<b>SHORT {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP")
        else:
            await tg(f"<b>SHORT {symbol}</b>\n🚫 TRAILING STOP ОТКЛЮЧЕН")

        # 5. Размещение TAKE_PROFIT_MARKET (НОВАЯ ЛОГИКА)
        if take_profit_enabled:
            # Цена TP = Цена открытия * (1 - Процент / 100)
            tp_price_f = price * (1 - TAKE_PROFIT_RATE / 100)
            tp_price_str = fix_price(symbol, tp_price_f) 

            tp_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
                "type": "TAKE_PROFIT_MARKET", "quantity": qty_str, "stopPrice": tp_price_str, 
            })

            if tp_order and (isinstance(tp_order, dict) and tp_order.get("algoId")):
                await tg(f"<b>SHORT {symbol}</b>\n✅ TAKE PROFIT ({TAKE_PROFIT_RATE}%) УСТАНОВЛЕН @ {tp_price_str}")
            else:
                await tg(f"<b>SHORT {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TAKE PROFIT")
        else:
            await tg(f"<b>SHORT {symbol}</b>\n🚫 TAKE PROFIT ОТКЛЮЧЕН")

    else:
        await tg(f"<b>Ошибка открытия SHORT {symbol}</b>")

# ... (close_position, close_long, close_short - без изменений)

async def close_position(sym: str, position_side: str, active_set: Set[str]):
    """Универсальная функция для закрытия LONG или SHORT позиции."""
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

async def close_long(sym: str): await close_position(sym, "LONG", active_longs)
async def close_short(sym: str): await close_position(sym, "SHORT", active_shorts)

# ==================== TELEGRAM WEBHOOK HANDLER =====================

def create_trailing_menu(trailing_status: bool, tp_status: bool):
    """Создает клавиатуру для меню Trailing Stop и Take Profit."""
    
    trailing_text = "ВКЛЮЧЕН" if trailing_status else "ОТКЛЮЧЕН"
    tp_text = "ВКЛЮЧЕН" if tp_status else "ОТКЛЮЧЕН"
    
    text = (
        "<b>⚙️ Управление ботом</b>\n\n"
        f"Трейлинг Стоп ({TRAILING_RATE}%): <b>{trailing_text}</b>\n"
        f"Take Profit ({TAKE_PROFIT_RATE}%): <b>{tp_text}</b>"
    )

    keyboard = [
        # Кнопка для Trailing Stop (теперь показывает текущее состояние и меняет его на противоположное)
        [
            InlineKeyboardButton(f"Трейлинг: {'✅ ВКЛ' if trailing_status else '❌ ВЫКЛ'}", 
                                 callback_data='set_trailing_false' if trailing_status else 'set_trailing_true'),
        ],
        # Кнопка для Take Profit (НОВАЯ)
        [
            InlineKeyboardButton(f"Take Profit: {'✅ ВКЛ' if tp_status else '❌ ВЫКЛ'}", 
                                 callback_data='set_tp_false' if tp_status else 'set_tp_true'),
        ],
        [
            InlineKeyboardButton("🔄 Обновить статус", callback_data='refresh_status'),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    return text, reply_markup

async def handle_telegram_update(update_json: Dict):
    """Обработка всех входящих сообщений и callback-запросов от Telegram."""
    global active_trailing_enabled, take_profit_enabled
    
    update = Update.de_json(update_json, tg_bot)
    
    # 1. Обработка команд (/start, /menu)
    if update.message and update.message.text:
        message = update.message
        
        if message.chat.id != CHAT_ID:
            await tg_bot.send_message(message.chat.id, "Доступ запрещен.")
            return

        text_lower = message.text.lower()
        if text_lower == '/start' or text_lower == '/menu':
            text, reply_markup = create_trailing_menu(active_trailing_enabled, take_profit_enabled)
            await message.reply_html(text, reply_markup=reply_markup)
            return

    # 2. Обработка нажатий на кнопки (CallbackQuery)
    elif update.callback_query:
        query = update.callback_query
        
        if query.message.chat.id != CHAT_ID:
            await query.answer("Доступ запрещен.", show_alert=True)
            return

        data = query.data
        state_changed = False
        
        # Логика Trailing Stop
        if data == 'set_trailing_true' and not active_trailing_enabled:
            active_trailing_enabled = True
            state_changed = True
        elif data == 'set_trailing_false' and active_trailing_enabled:
            active_trailing_enabled = False
            state_changed = True

        # Логика Take Profit (НОВАЯ)
        elif data == 'set_tp_true' and not take_profit_enabled:
            take_profit_enabled = True
            state_changed = True
        elif data == 'set_tp_false' and take_profit_enabled:
            take_profit_enabled = False
            state_changed = True
        
        await query.answer() 
        
        # Отправка уведомления об изменении состояния
        if state_changed:
            status_t = "ВКЛЮЧЕН" if active_trailing_enabled else "ОТКЛЮЧЕН"
            status_tp = "ВКЛЮЧЕН" if take_profit_enabled else "ОТКЛЮЧЕН"
            await tg(f"<b>⚙️ Настройки бота изменены через Telegram</b>\nТрейлинг: <b>{status_t}</b>\nTP: <b>{status_tp}</b>")
            
        # Обновляем меню (независимо от того, изменилось состояние или нет)
        text, reply_markup = create_trailing_menu(active_trailing_enabled, take_profit_enabled)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=constants.ParseMode.HTML)


async def set_telegram_webhook(url: str):
    """Регистрирует Webhook URL в Telegram при старте приложения."""
    try:
        response = await tg_bot.set_webhook(url=url)
        if response:
            print(f"✅ Telegram Webhook установлен на: {url}")
            # Отправка уведомления в tg() отключена, чтобы не дублировать стартовое сообщение
        else:
            print(f"❌ Ошибка установки Webhook. URL: {url}")
            await tg(f"<b>Ошибка!</b> Не удалось установить Telegram Webhook на: <code>{url}</code>")
    except TelegramError as e:
        print(f"❌ Критическая ошибка Telegram API при установке Webhook: {e}")
        await tg(f"<b>Критическая ошибка Telegram API</b>\nНе удалось установить Webhook: <code>{e}</code>")


# ================= FASTAPI ПРИЛОЖЕНИЕ =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Асинхронная загрузка данных о точности и позициях
    await load_exchange_info()
    await load_active_positions()
    
    # Установка вебхука при старте
    webhook_url = f"{PUBLIC_HOST_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    await set_telegram_webhook(webhook_url)
    
    status_t = "ВКЛЮЧЕН" if active_trailing_enabled else "ОТКЛЮЧЕН"
    status_tp = "ВКЛЮЧЕН" if take_profit_enabled else "ОТКЛЮЧЕН"
    await tg(
        f"<b>OZ BOT 2025 — ONLINE (v1.6.0)</b>\n"
        f"Трейлинг Стоп: <b>{status_t}</b> ({TRAILING_RATE}%)\n"
        f"Take Profit: <b>{status_tp}</b> ({TAKE_PROFIT_RATE}%)\n"
        f"Управление через Telegram Webhook (/menu)."
    )
    yield
    
    # Очистка вебхука при завершении
    try:
        await tg_bot.delete_webhook()
        print("Telegram Webhook очищен.")
    except Exception as e:
        print(f"Ошибка при очистке вебхука: {e}")
    await client.aclose() 

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE (v1.6.0)</h1>")

@app.post("/telegram_webhook/{token}")
async def handle_telegram(token: str, request: Request):
    if token != TELEGRAM_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid Telegram Token")
    
    try:
        update_data = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    asyncio.create_task(handle_telegram_update(update_data))
    
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

    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
