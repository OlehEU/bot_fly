# main.py — ПРОДУКЦИОННЫЙ (30.11.2025) — Автоматический stepSize
import os
import time
import hmac
import hashlib
import urllib.parse
import asyncio
from typing import Dict, Optional, Tuple
from decimal import Decimal, ROUND_DOWN, getcontext
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from contextlib import asynccontextmanager

# Увеличим точность decimal
getcontext().prec = 28

# ==================== КОНФИГ ====================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for v in required:
    if not os.getenv(v):
        raise EnvironmentError(f"Нет переменной {v}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID          = int(os.getenv("TELEGRAM_CHAT_ID"))
API_KEY          = os.getenv("BINANCE_API_KEY")
API_SECRET       = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")
AMOUNT           = Decimal(os.getenv("FIXED_AMOUNT_USD", "30"))
LEV              = int(os.getenv("LEVERAGE", "10"))
BASE             = "https://fapi.binance.com"
TIME_SYNC_INTERVAL = 60  # seconds, как часто синхронизируем время
EXCHANGEINFO_CACHE_TTL = 300  # seconds

bot = Bot(token=TELEGRAM_TOKEN)
client = httpx.AsyncClient(timeout=20)
active = set()
locks = defaultdict(asyncio.Lock)  # per-symbol lock to avoid races

# time offset: server_time_ms - local_time_ms
_time_offset_ms: int = 0
_last_time_sync = 0

# cache exchangeInfo symbol filters
_exchange_info_cache: Dict[str, Tuple[dict, float]] = {}  # symbol -> (filters_dict, timestamp)


# ----------------- utilities -----------------
async def tg(text: str):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    except Exception:
        # не ломаем приложение, если телеграм временно недоступен
        pass

def _now_ms() -> int:
    return int(time.time() * 1000)

def server_now_ms() -> int:
    return int(time.time() * 1000) + _time_offset_ms

def make_signature(params: Dict) -> str:
    """
    Подпись: ПАРАМЕТРЫ НЕ СОРТИРУЮТСЯ — порядок тот, в котором мы их положили в dict.
    Создаём querystring в порядке insertion (Python 3.7+ dict сохраняет порядок).
    """
    # Note: urllib.parse.quote_plus для корректного кодирования значений
    query = "&".join(f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in params.items())
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def sync_time_offset():
    """
    Синхронизируем локальное время с серверным временем Binance.
    """
    global _time_offset_ms, _last_time_sync
    try:
        r = await client.get(BASE + "/fapi/v1/time", timeout=10)
        if r.status_code == 200:
            server_time = r.json().get("serverTime")
            if server_time:
                local = int(time.time() * 1000)
                _time_offset_ms = int(server_time) - local
                _last_time_sync = time.time()
                return True
    except Exception:
        pass
    return False

async def ensure_time_synced():
    global _last_time_sync
    if _last_time_sync == 0 or time.time() - _last_time_sync > TIME_SYNC_INTERVAL:
        await sync_time_offset()

def quantize_qty(qty: Decimal, step_size: Decimal) -> Decimal:
    """
    Округляем вниз qty до ближайшего multiple of step_size.
    """
    if step_size == 0:
        return Decimal("0")
    # compute number of steps (floor)
    steps = (qty // step_size)
    return (steps * step_size).quantize(step_size, rounding=ROUND_DOWN)

def step_size_to_places(step: Decimal) -> int:
    # step like 0.001 -> 3 places, 1 -> 0 places
    s = step.normalize()
    exp = -s.as_tuple().exponent
    return max(0, exp)

# ----------------- Binance helpers -----------------
async def _fetch_exchange_info_symbol(symbol: str) -> Optional[dict]:
    """
    Возвращает dict с фильтрами (LOT_SIZE и др.) для symbol.
    Кешируем на EXCHANGEINFO_CACHE_TTL секунд.
    """
    now = time.time()
    cached = _exchange_info_cache.get(symbol)
    if cached and now - cached[1] < EXCHANGEINFO_CACHE_TTL:
        return cached[0]

    try:
        r = await client.get(BASE + "/fapi/v1/exchangeInfo", timeout=15)
        if r.status_code != 200:
            await tg(f"<b>BINANCE exchangeInfo error</b>\n<code>{r.text}</code>")
            return None
        data = r.json()
        for s in data.get("symbols", []):
            if s.get("symbol") == symbol:
                filters = {f["filterType"]: f for f in s.get("filters", [])}
                _exchange_info_cache[symbol] = (filters, now)
                return filters
    except Exception as e:
        await tg(f"<b>CRITICAL: exchangeInfo fetch failed</b>\n{str(e)[:300]}")
    return None

async def binance_request(method: str, path: str, params: Dict | None = None, signed: bool = True, retries: int = 2):
    """
    Универсальный вызов к Binance — НЕ бросает исключения по статусу,
    возвращает dict или None, и шлёт подробную ошибку в Telegram.
    """
    await ensure_time_synced()
    url = BASE + path
    p = params.copy() if params else {}
    if signed:
        # NB: порядок параметров важен (мы используем insertion order)
        p["timestamp"] = server_now_ms()
        p["recvWindow"] = 5000
        # signature добавляем последним
        p["signature"] = make_signature(p)
    headers = {"X-MBX-APIKEY": API_KEY}

    for attempt in range(retries + 1):
        try:
            r = await client.request(method, url, params=p, headers=headers)
            if r.status_code == 200:
                # безопасно возвращаем JSON
                try:
                    return r.json()
                except Exception:
                    await tg(f"<b>BINANCE PARSE ERROR</b>\n<code>{r.text}</code>")
                    return None
            else:
                # попробуем разобрать тело ответа
                body = r.text
                try:
                    js = r.json()
                    # Binance usually returns {"code":..., "msg":"..."}
                    msg = js.get("msg") or js
                except Exception:
                    msg = body
                # логируем ошибку
                await tg(f"<b>BINANCE ERROR {r.status_code} {path}</b>\n<code>{msg}</code>")
                # Если 5xx — можно ретраить
                if 500 <= r.status_code < 600 and attempt < retries:
                    await asyncio.sleep(0.5 + attempt)
                    continue
                return None
        except httpx.RequestError as e:
            await tg(f"<b>HTTP ERROR</b>\n<code>{str(e)[:300]}</code>")
            if attempt < retries:
                await asyncio.sleep(0.5 + attempt)
                continue
            return None
    return None

# ----------------- Business logic -----------------
async def calc_quantity_for_symbol(symbol: str, price: Decimal) -> Optional[Decimal]:
    """
    Вычисляет корректное quantity с учётом stepSize и minQty.
    """
    filters = await _fetch_exchange_info_symbol(symbol)
    if not filters:
        return None

    lot = filters.get("LOT_SIZE") or {}
    step_size = Decimal(lot.get("stepSize", "1"))
    min_qty = Decimal(lot.get("minQty", "0"))
    max_qty = Decimal(lot.get("maxQty", "0"))

    raw_qty = (AMOUNT * Decimal(LEV)) / price
    q = quantize_qty(raw_qty, step_size)

    # ensure >= min_qty
    if q < min_qty:
        # если меньше минимума — вернуть None и лог
        await tg(f"<b>Quantity too small for {symbol}</b>\nminQty={min_qty} raw={raw_qty} stepSize={step_size}")
        return None
    # cap by max_qty if provided (0 means no limit)
    if max_qty != 0 and q > max_qty:
        q = quantize_qty(max_qty, step_size)

    return q

async def open_long(sym: str):
    symbol = sym.upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    async with locks[symbol]:
        if symbol in active:
            await tg(f"<b>{symbol} уже открыт</b>")
            return

        # sync time before any signed requests
        await ensure_time_synced()

        # Set marginType and leverage (best-effort; we don't require response)
        await binance_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSS"})
        await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEV})

        # current price (unsigned)
        price_resp = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        if not price_resp:
            await tg(f"<b>Не могу получить цену для {symbol}</b>")
            return
        try:
            price = Decimal(price_resp["price"])
        except Exception:
            await tg(f"<b>Ошибка парсинга price для {symbol}</b>\n<code>{price_resp}</code>")
            return

        qty = await calc_quantity_for_symbol(symbol, price)
        if not qty or qty == 0:
            return

        # format quantity according to stepSize decimal places
        filters = await _fetch_exchange_info_symbol(symbol)
        step_size = Decimal(filters.get("LOT_SIZE", {}).get("stepSize", "1"))
        places = step_size_to_places(step_size)
        if places == 0:
            quantity_param = int(qty)
        else:
            # string with required decimal places (no extra zeros beyond places)
            quantity_param = format(qty.quantize(Decimal(10) ** -places), 'f')

        # place market buy
        order = await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": quantity_param
        })

        if order:
            active.add(symbol)
            await tg(f"<b>LONG ОТКРЫТ ×{LEV} (Cross)</b>\n"
                     f"<code>{symbol.replace('USDT','/USDT')}</code>\n"
                     f"{quantity_param} шт ≈ {price:.8f}\n<code>orderId: {order.get('orderId')}</code>")

async def close(sym: str):
    symbol = sym.upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    async with locks[symbol]:
        if symbol not in active:
            await tg(f"<b>{symbol} не в active — пропускаем CLOSE</b>")
            return

        # get positions
        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if not pos:
            await tg(f"<b>Не удалось получить позиции для {symbol}</b>")
            return

        # find positive position amount
        position_amt = None
        for p in pos:
            if p.get("symbol") == symbol:
                amt = Decimal(p.get("positionAmt", "0"))
                if amt > 0:
                    position_amt = amt
                break

        if not position_amt or position_amt == 0:
            await tg(f"<b>Позиция для {symbol} не найдена или нулевая — удаляю из active</b>")
            active.discard(symbol)
            return

        # format qty to match stepSize
        filters = await _fetch_exchange_info_symbol(symbol)
        step_size = Decimal(filters.get("LOT_SIZE", {}).get("stepSize", "1"))
        places = step_size_to_places(step_size)
        if places == 0:
            quantity_param = int(quantize_qty(position_amt, step_size))
        else:
            quantity_param = format(quantize_qty(position_amt, step_size), 'f')

        # send reduceOnly sell market
        order = await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": quantity_param,
            "reduceOnly": "true"
        })

        if order:
            active.discard(symbol)
            await tg(f"<b>CLOSE</b> {symbol.replace('USDT','/USDT')}\nqty: {quantity_param}\norderId: {order.get('orderId')}")

# ----------------- FastAPI app -----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # sync time at startup
    await sync_time_offset()
    await tg("<b>OZ BOT 2025 — ОНЛАЙН ×10</b>\nCross | Авто stepSize | Готов к работе")
    yield
    try:
        await client.aclose()
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return HTMLResponse("<h1>OZ BOT — РАБОТАЕТ</h1>")

def _task_exception_handler(task: asyncio.Task):
    try:
        exc = task.exception()
        if exc:
            # логируем в TG
            asyncio.create_task(tg(f"<b>Background task exception:</b>\n<code>{str(exc)[:300]}</code>"))
    except asyncio.CancelledError:
        pass

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    data = await request.json()
    s = data.get("symbol", "").replace("/", "").upper()
    sig = data.get("signal", "").upper()
    if not s or not sig:
        raise HTTPException(400, "missing symbol or signal")

    if sig == "LONG":
        t = asyncio.create_task(open_long(s))
        t.add_done_callback(_task_exception_handler)
    elif sig == "CLOSE":
        t = asyncio.create_task(close(s))
        t.add_done_callback(_task_exception_handler)
    else:
        # неизвестный сигнал
        await tg(f"<b>Unknown signal:</b> {sig} for {s}")
    return {"ok": True}
