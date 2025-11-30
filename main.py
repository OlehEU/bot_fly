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
        raise ValueError(f"Нет переменной окружения: {v}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
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
active = set()

# ================= TELEGRAM =====================
async def tg(text: str):
    """Отправляет сообщение в Telegram, используя HTML форматирование."""
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass

# ================= BINANCE API ====================
async def binance(method: str, path: str, params: Dict | None = None, signed: bool = True):
    """
    Универсальная функция для запросов к API Binance Futures. 
    Использует ручное формирование URL для надежной подписи и форматирует булевы значения.
    """
    url = BASE + path
    p = params.copy() if params else {}
    
    final_params = p
    
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 60000

        # --- Форматирование: Преобразование булевых значений в нижний регистр ---
        def format_value(v):
            if isinstance(v, bool):
                # Принудительно используем lowercase 'true'/'false'
                return str(v).lower()
            return str(v)
        # ----------------------------------------------------------------------------

        # 1. Сортируем параметры по ключу и собираем строку запроса вручную
        query_parts = [f"{k}={format_value(v)}" for k, v in sorted(p.items())]
        query_string = "&".join(query_parts)

        # 2. Генерируем подпись на основе этой строки
        signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

        # 3. Добавляем строку прямо в URL
        url = f"{url}?{query_string}&signature={signature}"
        
        final_params = None
    
    headers = {"X-MBX-APIKEY": API_KEY}
    
    try:
        r = await client.request(method, url, params=final_params, headers=headers)
        
        if r.status_code != 200:
            err = r.text if len(r.text) < 3800 else r.text[:3800] + "..."
            await tg(f"<b>BINANCE ERROR {r.status_code}</b>\nURL: <code>{url}</code>\nPath: {path}\n<code>{err}</code>")
            return None
        return r.json()
    except Exception as e:
        await tg(f"<b>CRITICAL ERROR</b>\n{str(e)[:3800]}")
        return None

# ================ LOAD POSITIONS ====================
async def load_active_positions():
    """Загружает открытые LONG позиции с Binance в active set при старте."""
    global active
    try:
        # Используем v2, чтобы получить все данные
        data = await binance("GET", "/fapi/v2/positionRisk", signed=True)
        if data:
            # positionAmt (количество) должно быть > 0
            open_longs = {
                p["symbol"] for p in data 
                if float(p["positionAmt"]) > 0 and p["positionSide"] == "LONG"
            }
            active = open_longs
            await tg(f"<b>Начальная загрузка позиций:</b>\nНайдено {len(active)} открытых LONG-позиций.")
    except Exception as e:
        await tg(f"<b>Ошибка при загрузке активных позиций:</b> {e}")


# ================ QTY ROUND =======================
def fix_qty(symbol: str, qty: float) -> str:
    """Округляет количество в зависимости от символа."""
    high_prec = ["DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","BONKUSDT","FLOKIUSDT","1000SATSUSDT"]
    if symbol in high_prec:
        return str(int(qty))
    return f"{qty:.3f}".rstrip("0").rstrip(".")

# ================ OPEN LONG =======================
async def open_long(sym: str):
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"

    # ПРОВЕРКА НА АКТИВНОСТЬ: проверяет set, который заполняется при старте
    if symbol in active:
        await tg(f"<b>{symbol}</b> — уже открыта (пропуск сигнала)")
        return

    # 1. Cross Margin. Игнорируем ошибку, если она не критична (уже установлен CROSS).
    await binance("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
    
    # 2. Leverage. Игнорируем ошибку, если она не критична (уже установлен LEV).
    await binance("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})

    # 3. Price + Qty
    price_data = await binance("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if not price_data or 'price' not in price_data:
        return
        
    price = float(price_data["price"])
    qty = fix_qty(symbol, AMOUNT * LEV / price)

    # 4. Open LONG (Market)
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
    symbol = sym.upper().replace("/", "").replace("USDT", "") + "USDT"
    
    if symbol not in active:
        await tg(f"<b>{symbol}</b> — не найдена в active set")
        return

    # 1. Проверка текущей позиции на бирже
    pos_data = await binance("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data:
        return
    
    # Ищем LONG позицию с количеством > 0
    qty_str = next((p["positionAmt"] for p in pos_data if p["positionSide"] == "LONG" and float(p["positionAmt"]) > 0), None)
    
    if not qty_str or float(qty_str) <= 0:
        # Позиция уже закрыта или не найдена
        active.discard(symbol)
        await tg(f"<b>{symbol}</b> — позиция уже закрыта на бирже")
        return

    # 2. Закрытие LONG позиции (Market)
    close_order = await binance("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": qty_str,
        "reduceOnly": True # Теперь гарантированно преобразуется в 'true' в нижнем регистре
    })
    
    if close_order and close_order.get("orderId"):
        active.discard(symbol)
        await tg(f"<b>CLOSE {symbol} УСПЕШНО</b>\n{qty_str} шт")
    else:
        await tg(f"<b>CRITICAL ERROR: Не удалось закрыть {symbol}</b>")

# ================= FASTAPI =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ! ЗДЕСЬ ПРОИСХОДИТ ПРОВЕРКА ПРИ СТАРТЕ !
    await load_active_positions()
    
    await tg("<b>OZ BOT 2025 — ONLINE</b>\nCross Mode | Hedge Mode FIXED\nОшибки Binance → полные")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT 2025 — ONLINE</h1>")

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
    elif signal == "CLOSE":
        asyncio.create_task(close(symbol))

    return {"ok": True}
