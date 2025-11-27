# main.py — Binance Futures LONG only, Fly.io-ready, async, with TP/SL fixes
import os
import time
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oz-bot")

# ===== CONFIG =====
required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEBHOOK_SECRET"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Нет переменной: {var}")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET")

FIXED_AMOUNT_USD = float(os.getenv("FIXED_AMOUNT_USD", "10"))
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))
TP_PERCENT       = float(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT       = float(os.getenv("SL_PERCENT", "1.0"))
RECV_WINDOW_MS   = int(os.getenv("RECV_WINDOW_MS", "5000"))

# Global httpx client (initialized on startup)
client: Optional[httpx.AsyncClient] = None

# Cache symbol info: { "BTCUSDT": {"precision": 3, "min_qty": 0.001} }
SYMBOL_INFO: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="oz-bot", version="1.0")


# ===== Telegram helper (async via httpx) =====
async def tg_send(text: str):
    global client
    if client is None:
        logger.error("HTTP client not initialized, cannot send TG message")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = await client.post(url, json=payload, timeout=20.0)
        if r.status_code >= 400:
            logger.warning("Telegram send failed: %s %s", r.status_code, await r.text())
    except Exception as e:
        logger.exception("TG send exception: %s", e)

def tg_send_bg(text: str):
    try:
        asyncio.create_task(tg_send(text))
    except Exception as e:
        logger.exception("Failed create_task for tg_send: %s", e)


# ===== Binance signature helper =====
def _create_signature(params: Dict[str, Any], secret: str) -> str:
    """
    Create HMAC SHA256 signature from sorted urlencoded params.
    """
    normalized = {}
    for k, v in params.items():
        if v is None:
            continue
        # booleans must be "true"/"false"
        if isinstance(v, bool):
            normalized[k] = str(v).lower()
        else:
            normalized[k] = str(v)
    query_string = urllib.parse.urlencode(sorted(normalized.items()))
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()


# ===== Binance request wrapper =====
async def binance_request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True):
    global client
    if client is None:
        raise RuntimeError("HTTP client not initialized")
    url = f"https://fapi.binance.com{endpoint}"
    params = params.copy() if params else {}

    if signed:
        # Ensure timestamp + recvWindow included BEFORE signature
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW_MS
        params["signature"] = _create_signature(params, BINANCE_API_SECRET)

    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if method.upper() == "GET":
            r = await client.get(url, params=params, headers=headers, timeout=30.0)
        else:
            # POST with params in querystring works for Binance signed endpoints
            r = await client.post(url, params=params, headers=headers, timeout=30.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        body = None
        try:
            body = r.json()
        except Exception:
            try:
                body = await r.text()
            except Exception:
                body = str(e)
        tg_send_bg(f"<b>BINANCE ERROR</b>\n<code>{body}</code>")
        logger.exception("Binance HTTPStatusError: %s", body)
        raise
    except Exception as e:
        tg_send_bg(f"<b>BINANCE EXCEPTION</b>\n<code>{str(e)[:500]}</code>")
        logger.exception("Binance exception: %s", e)
        raise


# ===== Symbol info loading & leverage set =====
async def load_symbol(symbol: str):
    if symbol in SYMBOL_INFO:
        return SYMBOL_INFO[symbol]
    info = await binance_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            prec = int(s.get("quantityPrecision", 8))
            min_qty = 0.0
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    min_qty = float(f.get("minQty", 0.0))
                    break
            SYMBOL_INFO[symbol] = {"precision": prec, "min_qty": min_qty}
            # attempt to set leverage (ignore failures)
            try:
                await binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
            except Exception:
                logger.info("Unable to set leverage for %s — maybe missing permissions or already set", symbol)
            return SYMBOL_INFO[symbol]
    raise Exception(f"Symbol not found: {symbol}")


# ===== Price & qty helpers =====
async def get_price(symbol: str) -> float:
    data = await binance_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])

async def calc_qty(symbol: str) -> str:
    info = await load_symbol(symbol)
    price = await get_price(symbol)
    raw = (FIXED_AMOUNT_USD * LEVERAGE) / price
    qty = round(raw, info["precision"])
    if qty < info["min_qty"]:
        qty = info["min_qty"]
    fmt = f"{{:.{info['precision']}f}}".format(qty)
    return fmt.rstrip("0").rstrip(".") if "." in fmt else fmt


# ===== Trading actions: LONG only =====
async def open_long(symbol: str):
    try:
        await load_symbol(symbol)
        await asyncio.sleep(0.12)  # small safety delay
        qty = await calc_qty(symbol)
        entry = await get_price(symbol)
        oid = f"oz_{int(time.time() * 1000)}"

        tp_price = round(entry * (1 + TP_PERCENT / 100), 6)
        sl_price = round(entry * (1 - SL_PERCENT / 100), 6)

        # MARKET BUY to open LONG
        order = await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": oid,
            "positionSide": "LONG"
        })

        if not order.get("orderId"):
            tg_send_bg(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}</b>\n{order}")
            return {"ok": False, "detail": order}

        # Place TP and SL as reduceOnly MARKET orders (if they fail, we log and continue)
        for price, typ in [(tp_price, "TAKE_PROFIT_MARKET"), (sl_price, "STOP_MARKET")]:
            try:
                params = {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": typ,
                    "quantity": qty,
                    "stopPrice": str(price),
                    "reduceOnly": "true",
                    "positionSide": "LONG",
                    "workingType": "CONTRACT_PRICE",   # use contract price
                    "priceProtect": "true",           # protect parameter
                    "newClientOrderId": f"{typ[:2].lower()}_{oid}"
                }
                # signature + recvWindow handled in binance_request
                await binance_request("POST", "/fapi/v1/order", params)
            except Exception as e:
                logger.exception("Failed to place %s for %s: %s", typ, symbol, e)

        text = (f"<b>LONG {symbol} ОТКРЫТ</b>\n"
                f"${FIXED_AMOUNT_USD:.2f} × {LEVERAGE}x\n"
                f"Entry: <code>{entry:.6f}</code>\n"
                f"TP: <code>{tp_price:.6f}</code> (+{TP_PERCENT}%)\n"
                f"SL: <code>{sl_price:.6f}</code> (-{SL_PERCENT}%)\n"
                f"Qty: <code>{qty}</code>")
        tg_send_bg(text)
        return {"ok": True, "entry": entry, "tp": tp_price, "sl": sl_price, "qty": qty}
    except Exception as e:
        logger.exception("open_long exception: %s", e)
        tg_send_bg(f"<b>ОШИБКА ОТКРЫТИЯ {symbol}</b>\n<code>{str(e)}</code>")
        return {"ok": False, "error": str(e)}

async def close_long(symbol: str):
    try:
        pos = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = 0.0
        if isinstance(pos, list):
            for p in pos:
                if p.get("positionSide") == "LONG" and p.get("symbol") == symbol:
                    amt = float(p.get("positionAmt", 0))
                    break
        else:
            if pos.get("positionSide") == "LONG" and pos.get("symbol") == symbol:
                amt = float(pos.get("positionAmt", 0))

        if abs(amt) < 1e-8:
            tg_send_bg(f"{symbol} уже закрыт")
            return {"ok": True, "detail": "already closed"}

        qty = f"{abs(amt):.8f}".rstrip("0").rstrip(".")
        await binance_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
            "positionSide": "LONG"
        })
        tg_send_bg(f"<b>{symbol} ЗАКРЫТ</b>")
        return {"ok": True}
    except Exception as e:
        logger.exception("close_long exception: %s", e)
        tg_send_bg(f"<b>ОШИБКА ЗАКРЫТИЯ {symbol}</b>\n<code>{str(e)}</code>")
        return {"ok": False, "error": str(e)}


# ===== Webhook verification =====
def verify_webhook_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header:
        return False
    try:
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)
    except Exception:
        return False


# ===== API models & endpoints =====
class WebhookModel(BaseModel):
    action: str
    symbol: str
    secret: Optional[str] = None

@app.on_event("startup")
async def startup():
    global client
    client = httpx.AsyncClient(timeout=60.0)
    logger.info("HTTP client initialized")
    try:
        tg_send_bg("<b>BOT STARTED</b>")
    except Exception:
        pass

@app.on_event("shutdown")
async def shutdown():
    global client
    if client:
        await client.aclose()
        logger.info("HTTP client closed")
    try:
        tg_send_bg("<b>BOT STOPPED</b>")
    except Exception:
        pass

# Root for Fly health checks
@app.get("/")
async def root():
    return {"status": "ok", "service": "oz-bot"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    """
    JSON:
      { "action": "open_long" | "close_long", "symbol": "BTCUSDT", "secret": "optional" }
    Header:
      X-SIGNATURE: hex(HMAC_SHA256(body, WEBHOOK_SECRET))
    """
    body = await request.body()
    sig = request.headers.get("X-SIGNATURE") or request.headers.get("X-Signature")
    ok = verify_webhook_signature(WEBHOOK_SECRET, body, sig)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # fallback if header missing: allow secret in JSON (less secure)
    if not ok:
        if data.get("secret") and data.get("secret") == WEBHOOK_SECRET:
            ok = True

    if not ok:
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = WebhookModel(**data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    action = payload.action.lower()
    symbol = payload.symbol.upper()

    if action == "open_long":
        background.add_task(open_long, symbol)
        return {"ok": True, "action": "open_long", "symbol": symbol}
    elif action == "close_long":
        background.add_task(close_long, symbol)
        return {"ok": True, "action": "close_long", "symbol": symbol}
    else:
        raise HTTPException(status_code=400, detail="Unknown action")


# Manual endpoints for quick testing
@app.post("/open/{symbol}")
async def open_manual(symbol: str):
    return await open_long(symbol.upper())

@app.post("/close/{symbol}")
async def close_manual(symbol: str):
    return await close_long(symbol.upper())
