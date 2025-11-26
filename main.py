# main.py — TERMINATOR 2026 | FULLY FIXED (signature, time sync, qty rounding)
import os
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional, List, Tuple
from decimal import Decimal, ROUND_DOWN
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oz-bot")

# ====================== КОНФИГ ======================
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Отсутствует переменная: {var}")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))

# retry / timings
MAX_RETRIES = 3
RETRY_BACKOFF = 0.5  # seconds

bot = Bot(token=TELEGRAM_TOKEN)
binance_client = httpx.AsyncClient(timeout=30.0)

async def tg_send(text: str):
    try:
        # bot.send_message may be coroutine depending on lib version; try await, fallback to sync
        coro = bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        if asyncio.iscoroutine(coro):
            await coro
    except Exception as e:
        logger.exception("Telegram error: %s", e)

# ====================== ВРЕМЯ СЕРВЕРА ======================
BASE = "https://fapi.binance.com"
SERVER_TIME_OFFSET_MS = 0  # server - local

async def sync_time():
    """Sync local time with Binance server time (sets SERVER_TIME_OFFSET_MS)."""
    global SERVER_TIME_OFFSET_MS
    try:
        r = await binance_client.get(f"{BASE}/fapi/v1/time", timeout=10.0)
        r.raise_for_status()
        server_time = int(r.json().get("serverTime", int(time.time() * 1000)))
        local_time = int(time.time() * 1000)
        SERVER_TIME_OFFSET_MS = server_time - local_time
        logger.info("Time synced: offset_ms=%s", SERVER_TIME_OFFSET_MS)
    except Exception as e:
        logger.warning("Time sync failed: %s", e)

def _now_ts() -> int:
    return int(time.time() * 1000 + SERVER_TIME_OFFSET_MS)

# ====================== ПОДПИСЬ (order-preserving) ======================
def _normalize_value(v: Any) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if v is None:
        return ""
    return str(v)

def _create_signature_from_ordered(pairs: List[Tuple[str, Any]], secret: str) -> str:
    """
    Create signature from ordered list of (k, v).
    We preserve insertion order (no sorting).
    """
    normalized = []
    for k, v in pairs:
        if v is None:
            continue
        normalized.append((k, _normalize_value(v)))
    qs = urllib.parse.urlencode(normalized)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return sig

# ====================== UNIVERSAL BINANCE REQUEST ======================
async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    """
    method: "GET" or "POST"
    endpoint: e.g. "/fapi/v1/order"
    params: dict (insertion order preserved)
    signed: add timestamp & signature
    """
    if signed and SERVER_TIME_OFFSET_MS == 0:
        # try to sync at first signed call
        await sync_time()

    params = params or {}
    # preserve insertion order - convert to list of pairs
    items: List[Tuple[str, Any]] = list(params.items())

    if signed:
        # append timestamp at the end to keep deterministic ordering
        items.append(("timestamp", _now_ts()))
        # optional recvWindow - helps with small time drifts
        items.append(("recvWindow", 5000))

        signature = _create_signature_from_ordered(items, BINANCE_API_SECRET)
        items.append(("signature", signature))

    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    # content-type for POST form
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    url = BASE + endpoint
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if method.upper() == "GET":
                resp = await binance_client.get(url, params=items, headers=headers, timeout=30.0)
            else:
                # IMPORTANT: send in data (form-encoded) AND keep order by passing list of tuples
                resp = await binance_client.post(url, data=items, headers=headers, timeout=30.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code
            try:
                body = e.response.json()
                code = body.get("code")
                msg = body.get("msg")
                await tg_send(f"<b>BINANCE ERROR</b>\n<code>{code}: {msg}</code>")
            except Exception:
                await tg_send(f"<b>BINANCE HTTP ERROR {status}</b>\n<code>{str(e)}</code>")
            # retry on server errors / rate limits
            if status in (429, 418) or 500 <= status < 600:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            raise
        except (httpx.RequestError, Exception) as e:
            last_exc = e
            logger.warning("Request failed attempt %s: %s", attempt, e)
            await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue
    raise last_exc if last_exc else Exception("Unknown error in binance_request")

# ====================== EXCHANGE INFO CACHE & SYMBOL DATA ======================
SYMBOL_CACHE: Dict[str, Dict[str, Any]] = {}
EXCHANGE_INFO_TS = 0.0
EXCHANGE_INFO_TTL = 60 * 30  # 30 minutes

async def _refresh_exchange_info(force: bool = False):
    global SYMBOL_CACHE, EXCHANGE_INFO_TS
    now = time.time()
    if SYMBOL_CACHE and not force and (now - EXCHANGE_INFO_TS) < EXCHANGE_INFO_TTL:
        return
    try:
        info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
        SYMBOL_CACHE = {}
        for s in info.get("symbols", []):
            sym = s["symbol"]
            # defaults
            min_qty = Decimal("0")
            step_size = Decimal("1")
            qty_prec = s.get("quantityPrecision", None)
            tick_size = None
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step_size = Decimal(str(f.get("stepSize", "1")))
                    min_qty = Decimal(str(f.get("minQty", "0")))
                if f.get("filterType") == "PRICE_FILTER":
                    tick_size = Decimal(str(f.get("tickSize", "0.00000001")))
            SYMBOL_CACHE[sym] = {
                "min_qty": min_qty,
                "step_size": step_size,
                "qty_precision": qty_prec,
                "tick_size": tick_size,
                "raw": s
            }
        EXCHANGE_INFO_TS = now
        logger.info("Loaded exchangeInfo for %s symbols", len(SYMBOL_CACHE))
    except Exception as e:
        logger.exception("Failed to refresh exchangeInfo: %s", e)

async def get_symbol_data(symbol: str) -> Dict[str, Any]:
    symbol = symbol.upper()
    await _refresh_exchange_info()
    if symbol in SYMBOL_CACHE:
        # ensure leverage set once (best-effort)
        try:
            await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": str(LEVERAGE)}, signed=True)
        except Exception:
            pass
        return SYMBOL_CACHE[symbol]
    # force reload and try again
    await _refresh_exchange_info(force=True)
    if symbol in SYMBOL_CACHE:
        return SYMBOL_CACHE[symbol]
    raise Exception(f"Symbol not found in exchangeInfo: {symbol}")

# ====================== PRICE & QTY CALC ======================
async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    # floor to multiple of step
    multiples = (value // step)
    return (multiples * step).quantize(step, rounding=ROUND_DOWN)

async def calc_qty(symbol: str, fixed_amount_usd: Optional[float] = None, leverage: Optional[int] = None) -> str:
    fa = FIXED_AMOUNT_USD if fixed_amount_usd is None else fixed_amount_usd
    lv = LEVERAGE if leverage is None else leverage

    sym = await get_symbol_data(symbol)
    price = Decimal(str(await get_price(symbol)))
    if price == 0:
        raise Exception("Price is zero")
    raw_usd = Decimal(str(fa)) * Decimal(str(lv))
    raw_qty = raw_usd / price

    step = sym.get("step_size", Decimal("1"))
    min_qty = sym.get("min_qty", Decimal("0"))

    qty_dec = _floor_to_step(raw_qty, step)
    if qty_dec < min_qty:
        qty_dec = min_qty

    qp = sym.get("qty_precision")
    if qp is not None:
        # format to qty_precision, strip trailing zeros
        s = f"{qty_dec:.{qp}f}".rstrip("0").rstrip(".")
        return s
    else:
        # fallback
        return format(qty_dec.normalize(), "f")

# ====================== OPEN / CLOSE LONG ======================
async def open_long(symbol: str):
    try:
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"

        qty = await calc_qty(symbol)
        oid = f"oz-{int(time.time()*1000)}"  # use dash to be safe
        # small delay to get stable price
        await asyncio.sleep(0.05)
        entry = await get_price(symbol)

        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": oid,
            # keep positionSide if account uses HEDGE, otherwise Binance accepts but may ignore in OWM
            "positionSide": "LONG",
        }

        start = time.time()
        resp = await binance_request("POST", "/fapi/v1/order", params=params, signed=True)
        took = round(time.time() - start, 2)

        if not resp.get("orderId"):
            # sometimes Binance returns fills without orderId in weird scenarios
            raise Exception(f"Invalid order response: {resp}")

        await tg_send(
            f"<b>LONG {symbol} ОТКРЫТ</b> за {took}s\n"
            f"${FIXED_AMOUNT_USD} × {LEVERAGE}x\n"
            f"Entry: <code>{entry:.8f}</code>\n"
            f"Кол-во: {qty}\n"
            f"OrderId: {resp.get('orderId')}"
        )
        return resp
    except Exception as e:
        logger.exception("open_long failed: %s", e)
        await tg_send(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}</b>\n<code>{str(e)}</code>")

async def close_long(symbol: str):
    try:
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"

        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        amt = Decimal("0")
        for p in pos:
            if p.get("symbol") == symbol and p.get("positionSide") == "LONG":
                amt = Decimal(str(p.get("positionAmt", "0")))
                break

        if abs(float(amt)) < 1e-8:
            await tg_send(f"{symbol} LONG уже закрыт")
            return

        sym = await get_symbol_data(symbol)
        step = sym.get("step_size", Decimal("1"))
        dqty = _floor_to_step(abs(amt), step)
        qp = sym.get("qty_precision")
        if qp is not None:
            qty_str = f"{dqty:.{qp}f}".rstrip("0").rstrip(".")
        else:
            qty_str = format(dqty.normalize(), "f")

        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_str,
            "reduceOnly": "true",
            "positionSide": "LONG",
        }

        await binance_request("POST", "/fapi/v1/order", params=params, signed=True)
        await tg_send(f"<b>{symbol} LONG ЗАКРЫТ</b>")
    except Exception as e:
        logger.exception("close_long failed: %s", e)
        await tg_send(f"<b>ОШИБКА ЗАКРЫТИЯ {symbol}</b>\n<code>{str(e)}</code>")

# ====================== FASTAPI / WEBHOOK ======================
app = FastAPI()

@app.on_event("startup")
async def startup():
    # sync time and warm cache
    try:
        await sync_time()
        await _refresh_exchange_info()
    except Exception as e:
        logger.warning("Startup sync failed: %s", e)
    await tg_send("<b>TERMINATOR 2026 ЗАПУЩЕН</b>\nИсправлен модуль Binance (signature/time/qty)")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1 style='color:#0f0;background:#000;text-align:center;padding:100px;font-family:monospace'>TERMINATOR 2026<br>ТВОЙ РАБОЧИЙ КОД<br>ONLINE</h1>"

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400)

    raw_symbol = data.get("symbol", "").upper()
    action = data.get("direction", "").upper()  # LONG / CLOSE

    symbol = raw_symbol if raw_symbol.endswith("USDT") else raw_symbol + "USDT"

    if action == "LONG":
        asyncio.create_task(open_long(symbol))
    elif action == "CLOSE":
        asyncio.create_task(close_long(symbol))
    else:
        logger.warning("Unknown action: %s", action)

    return {"status": "ok"}
