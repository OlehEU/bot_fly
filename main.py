# =========================================================================================
# OZ TRADING BOT 2025 v1.5.4 | TS_START_RATE & TRAILING_RATE=1.0
# =========================================================================================
import os
import time
import hmac
import hashlib
import json
from typing import Dict, Set, Any
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, constants
from telegram.error import TelegramError
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
PUBLIC_HOST_URL = os.getenv("PUBLIC_HOST_URL").rstrip('/')
AMOUNT = float(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV = int(os.getenv("LEVERAGE", "10"))
TRAILING_RATE = float(os.getenv("TRAILING_RATE", "1.0")) # ИЗМЕНЕНО: По умолчанию 1.0%
TAKE_PROFIT_RATE = float(os.getenv("TAKE_PROFIT_RATE", "1.0")) 
TS_START_RATE = float(os.getenv("TS_START_RATE", "0.2")) # НОВАЯ ПЕРЕМЕННАЯ: Цена активации Trailing Stop в % прибыли

# Инициализация HTTP клиента
client = httpx.AsyncClient(timeout=30)
BASE = "https://fapi.binance.com"

# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
symbol_precision: Dict[str, int] = {}
price_precision: Dict[str, int] = {}
active_longs: Set[str] = set() 
active_shorts: Set[str] = set() 
active_trailing_enabled: bool = os.getenv("TRAILING_ENABLED", "true").lower() in ('true', '1', 't')
take_profit_enabled: bool = os.getenv("TAKE_PROFIT_ENABLED", "true").lower() in ('true', '1', 't')

# Инициализация Telegram Bot
tg_bot = Bot(token=TELEGRAM_TOKEN) 

# ================= TELEGRAM УВЕДОМЛЕНИЯ (Без изменений) =====================
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

def format_error_detail(error_result: Any) -> str:
    """Форматирует словарь ошибки Binance в читаемый код для Telegram."""
    if not error_result or not isinstance(error_result, dict):
        return str(error_result) if error_result else "Пустой или None ответ от Binance"
    
    code = error_result.get('code', 'N/A')
    msg = error_result.get('msg', 'N/A')
    
    if code != 'N/A' or msg != 'N/A':
        return f"Code: {code}\nMsg: {msg}"
    
    return json.dumps(error_result, indent=2)

# ================= BINANCE API ЗАПРОСЫ (Без изменений) ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    """Универсальная функция для запросов к API Binance Futures."""
    url = BASE + path
    p = params.copy() if params else {}
    final_params = p
    
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000

        def format_value(v):
            if isinstance(v, bool): return str(v).lower()
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
                error_info = {}
                try:
                    error_info = r.json()
                    err_msg = f"Code: {error_info.get('code', 'N/A')}. Msg: {error_info.get('msg', 'N/A')}"
                except Exception:
                    err_msg = err_text
                    
                await tg(f"<b>BINANCE ERROR {r.status_code}</b>\nPath: {path}\n<code>{err_msg}</code>")
            
            try: return r.json()
            except Exception: return None
        
        try: return r.json()
        except Exception: return r.text
            
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Без изменений) ================
def calculate_precision_from_stepsize(step_size: str) -> int:
    s = step_size.rstrip('0')
    if '.' not in s: return 0
    return len(s.split('.')[-1])

async def load_exchange_info():
    global symbol_precision, price_precision
    try:
        data = await binance("GET", "/fapi/v1/exchangeInfo", signed=False)
        
        if not data or not isinstance(data, dict) or 'symbols' not in data:
            await tg("<b>Ошибка:</b> Не удалось загрузить информацию о бинарных символах.")
            return

        for symbol_info in data['symbols']:
            sym = symbol_info['symbol']
            
            # Точность для количества (LOT_SIZE)
            lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size_filter:
                step_size = lot_size_filter['stepSize']
                symbol_precision[sym] = calculate_precision_from_stepsize(step_size)
            
            # Точность для цены (PRICE_FILTER)
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if price_filter:
                tick_size = price_filter['tickSize']
                price_precision[sym] = calculate_precision_from_stepsize(tick_size)

        await tg(f"<b>Загружена информация о бинарных символах:</b> Точность QTY для {len(symbol_precision)} пар, PRICE для {len(price_precision)} пар.")

    except Exception as e:
        await tg(f"<b>Критическая ошибка при загрузке exchangeInfo:</b> {e}")


async def load_active_positions():
    global active_longs, active_shorts
    try:
        data = await binance("GET", "/fapi/v2/positionRisk", signed=True)
        if isinstance(data, dict) and data.get("code"): 
             await tg(f"<b>Ошибка:</b> Не удалось загрузить активные позиции. {data.get('msg', '')}")
             return

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

def fix_price(symbol: str, price: float) -> str:
    precision = price_precision.get(symbol.upper(), 8) 
    return f"{price:.{precision}f}".rstrip("0").rstrip(".")


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


# ================= ФУНКЦИИ ОТКРЫТИЯ (ОБНОВЛЕНО РАСЧЕТ TS_START_RATE) =======================

async def open_long(sym: str):
    global active_trailing_enabled, take_profit_enabled
    
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

    # 3. Открытие LONG позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": qty_str
    })

    if isinstance(order, dict) and order.get("orderId"):
        active_longs.add(symbol)
        
        rate_str = f"{TRAILING_RATE:.2f}" 
        
        # РАСЧЕТ TS АКТИВАЦИИ: Используем TS_START_RATE
        ts_activation_price_f = price * (1 + TS_START_RATE / 100)
        ts_activation_price_str = fix_price(symbol, ts_activation_price_f) 

        # НОВЫЙ БЛОК TELEGRAM: ОТЧЕТ ОБ ОТКРЫТИИ (Вариант 1)
        usd_amount = float(qty_str) * price 
        
        main_message = (
            f"<b>🚀 LONG | {symbol.replace('USDT', '/USDT')} (x{LEV})</b>\n"
            f"---"
        )
        await tg(main_message)
        
        detail_message = (
            f"📈 Цена входа: <code>{fix_price(symbol, price)}</code>\n"
            f"💵 Объем: {qty_str} шт (~${usd_amount:.0f})"
        )
        await tg(detail_message)
        # КОНЕЦ НОВОГО БЛОКА

        # Задержка 1.5 сек, чтобы цена успела отойти от триггера TP/TS
        await asyncio.sleep(1.5) 
        
        tp_ok, ts_ok = False, False # Флаги для проверки установки ордеров
        tp_price_str = "N/A" # Инициализация для финального сообщения
        
        # 5. Размещение TRAILING_STOP_MARKET
        if active_trailing_enabled:
            trailing_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": "TRAILING_STOP_MARKET", "quantity": qty_str, 
                "callbackRate": rate_str, 
                "activationPrice": ts_activation_price_str,
            })

            if isinstance(trailing_order, dict) and trailing_order.get("algoId"):
                ts_ok = True
            else:
                error_log = format_error_detail(trailing_order)
                await tg(f"<b>LONG {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP\n<code>{error_log}</code>")
        
        # 6. Размещение TAKE_PROFIT_MARKET
        if take_profit_enabled:
            tp_price_f = price * (1 + TAKE_PROFIT_RATE / 100)
            tp_price_str = fix_price(symbol, tp_price_f) 

            tp_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "positionSide": "LONG",
                "type": "TAKE_PROFIT_MARKET", "quantity": qty_str, "triggerPrice": tp_price_str, 
            })

            if isinstance(tp_order, dict) and tp_order.get("algoId"):
                tp_ok = True
            else:
                error_log = format_error_detail(tp_order)
                await tg(f"<b>LONG {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TAKE PROFIT\n<code>{error_log}</code>")
        
        # НОВЫЙ БЛОК: Единое сообщение о результатах установки (Вариант 1)
        if tp_ok or ts_ok or (not take_profit_enabled and active_trailing_enabled):
             
             tp_line = ""
             if take_profit_enabled:
                tp_line = f"🎯 TP ({TAKE_PROFIT_RATE}%): <code>{tp_price_str}</code> {'✅' if tp_ok else '❌'}\n"
             elif not take_profit_enabled:
                # Если TP отключен, но мы показываем результат, указываем это.
                tp_line = f"🎯 TP: {'Отключен'}\n"

             ts_line = f"🛡️ TS ({TRAILING_RATE}%, Активация {TS_START_RATE}%): <code>{ts_activation_price_str}</code> {'✅' if ts_ok else '❌'}\n"

             status_message = (
                f"{tp_line}{ts_line}"
                f"\n✅ **Ордера установлены.**"
             )
             await tg(status_message)
        # КОНЕЦ НОВОГО БЛОКА О РЕЗУЛЬТАТАХ

    else:
        error_log = format_error_detail(order)
        await tg(f"<b>Ошибка открытия LONG {symbol}</b>\n<code>{error_log}</code>")


async def open_short(sym: str):
    global active_trailing_enabled, take_profit_enabled
    
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

    # 3. Открытие SHORT позиции (Market)
    order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": "SELL", "positionSide": "SHORT", "type": "MARKET", "quantity": qty_str
    })

    if isinstance(order, dict) and order.get("orderId"):
        active_shorts.add(symbol)
        
        rate_str = f"{TRAILING_RATE:.2f}"
        
        # РАСЧЕТ TS АКТИВАЦИИ: Используем TS_START_RATE
        ts_activation_price_f = price * (1 - TS_START_RATE / 100)
        ts_activation_price_str = fix_price(symbol, ts_activation_price_f) 

        # НОВЫЙ БЛОК TELEGRAM: ОТЧЕТ ОБ ОТКРЫТИИ (Вариант 1)
        usd_amount = float(qty_str) * price
        
        main_message = (
            f"<b>⬇️ SHORT | {symbol.replace('USDT', '/USDT')} (x{LEV})</b>\n"
            f"---"
        )
        await tg(main_message)
        
        detail_message = (
            f"📉 Цена входа: <code>{fix_price(symbol, price)}</code>\n"
            f"💵 Объем: {qty_str} шт (~${usd_amount:.0f})"
        )
        await tg(detail_message)
        # КОНЕЦ НОВОГО БЛОКА

        # Задержка 1.5 сек, чтобы цена успела отойти от триггера TP/TS
        await asyncio.sleep(1.5) 

        tp_ok, ts_ok = False, False # Флаги для проверки установки ордеров
        tp_price_str = "N/A" # Инициализация для финального сообщения

        # 5. Размещение TRAILING_STOP_MARKET
        if active_trailing_enabled:
            trailing_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
                "type": "TRAILING_STOP_MARKET", "quantity": qty_str, 
                "callbackRate": rate_str, 
                "activationPrice": ts_activation_price_str, 
            })

            if isinstance(trailing_order, dict) and trailing_order.get("algoId"):
                ts_ok = True
            else:
                error_log = format_error_detail(trailing_order)
                await tg(f"<b>SHORT {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP\n<code>{error_log}</code>")
        
        # 6. Размещение TAKE_PROFIT_MARKET
        if take_profit_enabled:
            tp_price_f = price * (1 - TAKE_PROFIT_RATE / 100)
            tp_price_str = fix_price(symbol, tp_price_f) 

            tp_order = await binance("POST", "/fapi/v1/algoOrder", { 
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY", "positionSide": "SHORT",
                "type": "TAKE_PROFIT_MARKET", "quantity": qty_str, "triggerPrice": tp_price_str, 
            })

            if isinstance(tp_order, dict) and tp_order.get("algoId"):
                tp_ok = True
            else:
                error_log = format_error_detail(tp_order)
                await tg(f"<b>SHORT {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TAKE PROFIT\n<code>{error_log}</code>")

        # НОВЫЙ БЛОК: Единое сообщение о результатах установки (Вариант 1)
        if tp_ok or ts_ok or (not take_profit_enabled and active_trailing_enabled):
             
             tp_line = ""
             if take_profit_enabled:
                tp_line = f"🎯 TP ({TAKE_PROFIT_RATE}%): <code>{tp_price_str}</code> {'✅' if tp_ok else '❌'}\n"
             elif not take_profit_enabled:
                tp_line = f"🎯 TP: {'Отключен'}\n"

             ts_line = f"🛡️ TS ({TRAILING_RATE}%, Активация {TS_START_RATE}%): <code>{ts_activation_price_str}</code> {'✅' if ts_ok else '❌'}\n"

             status_message = (
                f"{tp_line}{ts_line}"
                f"\n✅ **Ордера установлены.**"
             )
             await tg(status_message)
        # КОНЕЦ НОВОГО БЛОКА О РЕЗУЛЬТАТАХ

    else:
        error_log = format_error_detail(order)
        await tg(f"<b>Ошибка открытия SHORT {symbol}</b>\n<code>{error_log}</code>")


async def close_position(sym: str, position_side: str, active_set: Set[str]):
    # ... (код для закрытия позиции без изменений)
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    await binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}) 
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    
    if isinstance(pos_data, dict) and pos_data.get("code"): 
        await tg(f"<b>{symbol}</b> — Не удалось получить данные о позиции. {pos_data.get('msg', '')}"); return
    
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
        await tg(f"<b>✅ ЗАКРЫТИЕ {position_side} {symbol} УСПЕШНО</b>\n{qty_to_close} шт")
    else:
        error_log = format_error_detail(close_order)
        await tg(f"<b>CRITICAL ERROR: Не удалось закрыть {position_side} {symbol}</b>\n<code>{error_log}</code>")

async def close_long(sym: str):
    await close_position(sym, "LONG", active_longs)

async def close_short(sym: str):
    await close_position(sym, "SHORT", active_shorts)
# ================= КОНЕЦ ФУНКЦИЙ ОТКРЫТИЯ/ЗАКРЫТИЯ =======================


# ==================== TELEGRAM WEBHOOK HANDLER (Обновлено меню) =====================

def create_trailing_menu(trailing_status: bool, tp_status: bool):
    """Создает клавиатуру для меню Trailing Stop и Take Profit."""
    trailing_text = "ВКЛЮЧЕН" if trailing_status else "ОТКЛЮЧЕН"
    tp_text = "ВКЛЮЧЕН" if tp_status else "ОТКЛЮЧЕН"
    
    text = (
        "<b>⚙️ Управление ботом</b>\n\n"
        f"Трейлинг Стоп (Откат <b>{TRAILING_RATE}%</b> / Активация <b>{TS_START_RATE}%</b>): <b>{trailing_text}</b>\n"
        f"Take Profit (Фикс. <b>{TAKE_PROFIT_RATE}%</b>): <b>{tp_text}</b>"
    )

    keyboard = [
        [
            InlineKeyboardButton(f"Трейлинг: {'✅ ВКЛ' if trailing_status else '❌ ВЫКЛ'}", 
                                 callback_data='set_trailing_false' if trailing_status else 'set_trailing_true'),
        ],
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
    """
    Основная функция для обработки всех входящих сообщений и callback-запросов от Telegram.
    """
    global active_trailing_enabled, take_profit_enabled
    
    update = Update.de_json(update_json, tg_bot)
    
    # 1. Обработка команд (/start, /menu)
    if update.message and update.message.text:
        message = update.message
        if message.chat.id != CHAT_ID: await tg_bot.send_message(message.chat.id, "Доступ запрещен."); return
        text_lower = message.text.lower()
        if text_lower == '/start' or text_lower == '/menu':
            text, reply_markup = create_trailing_menu(active_trailing_enabled, take_profit_enabled)
            await message.reply_html(text, reply_markup=reply_markup); return

    # 2. Обработка нажатий на кнопки (CallbackQuery)
    elif update.callback_query:
        query = update.callback_query
        if query.message.chat.id != CHAT_ID: await query.answer("Доступ запрещен.", show_alert=True); return
        data = query.data
        state_changed = False
        
        # Обработка Trailing Stop
        if data == 'set_trailing_true' and not active_trailing_enabled: active_trailing_enabled = True; state_changed = True
        elif data == 'set_trailing_false' and active_trailing_enabled: active_trailing_enabled = False; state_changed = True
        
        # Обработка Take Profit
        elif data == 'set_tp_true' and not take_profit_enabled: take_profit_enabled = True; state_changed = True
        elif data == 'set_tp_false' and take_profit_enabled: take_profit_enabled = False; state_changed = True
        
        await query.answer() 
        
        if state_changed:
            status_t = "ВКЛЮЧЕН" if active_trailing_enabled else "ОТКЛЮЧЕН"
            status_tp = "ВКЛЮЧЕН" if take_profit_enabled else "ОТКЛЮЧЕН"
            await tg(f"<b>⚙️ Настройки бота изменены через Telegram</b>\nТрейлинг: <b>{status_t}</b>\nTP: <b>{status_tp}</b>")
            
        # Обновление меню
        text, reply_markup = create_trailing_menu(active_trailing_enabled, take_profit_enabled)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=constants.ParseMode.HTML)


async def set_telegram_webhook(url: str):
    """
    Регистрирует Webhook URL в Telegram при старте приложения.
    """
    try:
        response = await tg_bot.set_webhook(url=url)
        if response:
            print(f"✅ Telegram Webhook установлен на: {url}")
        else:
            print(f"❌ Ошибка установки Webhook. URL: {url}")
            await tg(f"<b>Ошибка!</b> Не удалось установить Telegram Webhook на: <code>{url}</code>")
    except TelegramError as e:
        print(f"❌ Критическая ошибка Telegram API при установке Webhook: {e}")
        await tg(f"<b>Критическая ошибка Telegram API</b>\nНе удалось установить Webhook: <code>{e}</code>")


# ================= FASTAPI ПРИЛОЖЕНИЕ (Изменена метка версии) =========================

async def get_binance_server_time():
    """Получает и возвращает текущее время сервера Binance."""
    try:
        data = await binance("GET", "/fapi/v1/time", signed=False) 
        if isinstance(data, dict) and data.get('serverTime'):
            return int(data['serverTime'])
    except Exception as e:
        print(f"Ошибка получения времени Binance: {e}")
    return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_exchange_info()
    
    # 1. Диагностика времени
    server_time = await get_binance_server_time()
    bot_time_ms = int(time.time() * 1000)
    time_info = ""
    if server_time:
        time_diff = abs(server_time - bot_time_ms)
        diff_sec = time_diff / 1000
        
        server_time_str = datetime.fromtimestamp(server_time / 1000, timezone.utc).strftime("%H:%M:%S UTC")
        bot_time_str = datetime.fromtimestamp(bot_time_ms / 1000, timezone.utc).strftime("%H:%M:%S UTC")

        time_info = (
            f"🕒 Время бота: <b>{bot_time_str}</b>\n"
            f"🕒 Время Binance: <b>{server_time_str}</b>\n"
            f"Разница: <b>{diff_sec:.3f} сек</b>"
        )
        if diff_sec > 5: 
             time_info += " ⚠️ **ВНИМАНИЕ!** Отклонение времени значительное."
    else:
        time_info = "🕒 Не удалось получить время Binance."

    await load_active_positions()
    
    webhook_url = f"{PUBLIC_HOST_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    await set_telegram_webhook(webhook_url)
    
    status_t = "ВКЛЮЧЕН" if active_trailing_enabled else "ОТКЛЮЧЕН"
    status_tp = "ВКЛЮЧЕН" if take_profit_enabled else "ОТКЛЮЧЕН"
    await tg(
        f"<b>OZ BOT 2025 — ONLINE (v1.5.4)</b>\n" 
        f"Трейлинг Стоп: <b>{status_t}</b> (Откат {TRAILING_RATE}%, Активация {TS_START_RATE}%)\n"
        f"Take Profit: <b>{status_tp}</b> ({TAKE_PROFIT_RATE}%)\n"
        f"---"
        f"\n{time_info}\n"
        f"---"
        f"\nУправление через Telegram Webhook (/menu)."
    )
    yield
    
    try:
        await tg_bot.delete_webhook()
        print("Telegram Webhook очищен.")
    except Exception as e:
        print(f"Ошибка при очистке вебхука: {e}")
    await client.aclose() 

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE (v1.5.4)</h1>")

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
    uvicorn.run(app, host="0.0.0.0", port=8000)
