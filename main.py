# =========================================================================================
# OZ TRADING BOT 2025 v1.1.8 | ДЕТАЛЬНЫЙ ЛОГ КОЛИЧЕСТВА ДЛЯ TRAILING STOP
# =========================================================================================
import os
# ... (Остальной импорт)

# ... (Конфигурация, Telegram, Binance API остаются прежними)

# ... (load_exchange_info и load_active_positions остаются прежними)

# ================ ОКРУГЛЕНИЕ КОЛИЧЕСТВА =======================
def fix_qty(symbol: str, qty: float) -> str:
    """
    Округляет количество в зависимости от динамически загруженной точности Binance.
    """
    # Получаем точность из глобального словаря. Если нет (новая или необычная пара), используем 3 по умолчанию.
    precision = symbol_precision.get(symbol.upper(), 3)

    if precision == 0:
        # Для целых чисел гарантируем, что передаем чистое целое число без .0
        return str(int(qty)) 
    
    # Форматирование: f"{qty:.{precision}f}"
    return f"{qty:.{precision}f}".rstrip("0").rstrip(".")

# ================ ФУНКЦИИ ОТКРЫТИЯ =======================

# ... (get_symbol_and_qty остается прежним)

async def open_long(sym: str):
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    
    # === СИНХРОНИЗАЦИЯ С БИРЖЕЙ ПЕРЕД ОТКРЫТИЕМ ===
    # ... (Логика синхронизации остается прежней)
    
    if is_open_on_exchange:
        # ... (Обработка пропуска остается прежней)
        return

    active_longs.discard(symbol) 
    # =================================================================

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
        
        # --- НОВЫЙ ДЕТАЛЬНЫЙ ЛОГ ---
        await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\nПопытка установить Trailing Stop. QTY: <code>{qty_str}</code>")
        # --- КОНЕЦ НОВОГО ЛОГА ---

        # 4. Размещение TRAILING_STOP_MARKET ордера (SELL для закрытия LONG)
        trailing_order = await binance("POST", "/fapi/v1/order/algo", { 
            "symbol": symbol, 
            "side": "SELL",
            "positionSide": "LONG",
            "type": "TRAILING_STOP_MARKET",
            "quantity": qty_str, # Используем то же самое, что и для Market-ордера
            "callbackRate": TRAILING_RATE,
        })

        if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("orderId")):
            # Обновленное сообщение об успехе:
            await tg(f"<b>LONG ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
        else:
            # Безопасное логирование ответа Binance
            log_detail = str(trailing_order) if trailing_order else "Пустой или None ответ от Binance"
            
            if isinstance(log_detail, str) and log_detail.strip().startswith("<"):
                 log_text = f"ОТВЕТ В ФОРМАТЕ HTML. Обрезан лог: {log_detail[:100]}..."
            else:
                 log_text = log_detail
            
            # Обновленное сообщение об ошибке (теперь без дублирования инфо о позиции)
            await tg(f"<b>LONG ×{LEV} (Cross+Hedge) {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP (СМОТРИТЕ ЛОГ)\n<pre>{log_text}</pre>")
    else:
        await tg(f"<b>Ошибка открытия LONG {symbol}</b>")

# НОВАЯ ФУНКЦИЯ ДЛЯ ОТКРЫТИЯ SHORT
async def open_short(sym: str):
    result = await get_symbol_and_qty(sym)
    if not result: return

    symbol, qty_str, price = result
    
    # === СИНХРОНИЗАЦИЯ С БИРЖЕЙ ПЕРЕД ОТКРЫТИЕМ ===
    # ... (Логика синхронизации остается прежней)

    if is_open_on_exchange:
        # ... (Обработка пропуска остается прежней)
        return

    active_shorts.discard(symbol) 
    # =================================================================

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
        
        # --- НОВЫЙ ДЕТАЛЬНЫЙ ЛОГ ---
        await tg(f"<b>SHORT ×{LEV} (Cross+Hedge)</b>\n<code>{symbol}</code>\n{qty_str} шт ≈ ${AMOUNT*LEV:.2f} (Объем) / ${AMOUNT:.2f} (Обеспечение)\n@ {price:.8f}\n\nПопытка установить Trailing Stop. QTY: <code>{qty_str}</code>")
        # --- КОНЕЦ НОВОГО ЛОГА ---

        # 4. Размещение TRAILING_STOP_MARKET ордера (BUY для закрытия SHORT)
        trailing_order = await binance("POST", "/fapi/v1/order/algo", { 
            "symbol": symbol, 
            "side": "BUY", # Покупаем (закрываем SHORT позицию)
            "positionSide": "SHORT",
            "type": "TRAILING_STOP_MARKET",
            "quantity": qty_str,
            "callbackRate": TRAILING_RATE,
        })

        if trailing_order and (isinstance(trailing_order, dict) and trailing_order.get("orderId")):
            # Обновленное сообщение об успехе:
            await tg(f"<b>SHORT ×{LEV} (Cross+Hedge) {symbol}</b>\n✅ TRAILING STOP ({TRAILING_RATE}%) УСТАНОВЛЕН")
        else:
            # Безопасное логирование ответа Binance
            log_detail = str(trailing_order) if trailing_order else "Пустой или None ответ от Binance"
            
            if isinstance(log_detail, str) and log_detail.strip().startswith("<"):
                 log_text = f"ОТВЕТ В ФОРМАТЕ HTML. Обрезан лог: {log_detail[:100]}..."
            else:
                 log_text = log_detail

            # Обновленное сообщение об ошибке (теперь без дублирования инфо о позиции)
            await tg(f"<b>SHORT ×{LEV} (Cross+Hedge) {symbol}</b>\n⚠️ ОШИБКА УСТАНОВКИ TRAILING STOP (СМОТРИТЕ ЛОГ)\n<pre>{log_text}</pre>")

    else:
        await tg(f"<b>Ошибка открытия SHORT {symbol}</b>")

# ... (Функции закрытия и FastAPI остаются прежними)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_exchange_info()
    await load_active_positions()
    
    await tg("<b>OZ BOT 2025 — ONLINE (v1.1.8)</b>\nВнедрено детальное логирование Trailing Stop (QTY).")
    yield
    await client.aclose()

# ... (Остальной код FastAPI)
