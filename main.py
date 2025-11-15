# main.py
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import hmac
import hashlib
import time
import logging
from pydantic import BaseModel
import asyncio

# === НАСТРОЙКИ ===
API_KEY = "ВАШ_API_KEY"          # ← Замени
API_SECRET = "ВАШ_API_SECRET"    # ← Замени
BASE_URL = "https://contract.mexc.com"
SYMBOL = "XRP_USDT"
VOLUME = 4.0
LEVERAGE = 1

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

app = FastAPI()

# === Модель ордера ===
class OrderRequest(BaseModel):
    action: str = "open"  # open / close

# === Подпись запроса ===
def sign_request(params: dict, timestamp: str) -> str:
    param_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    sign_str = f"{param_str}&timestamp={timestamp}"
    return hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

# === Отправка ордера ===
async def place_mexc_order():
    url = f"{BASE_URL}/api/v1/private/order/submit"
    timestamp = str(int(time.time() * 1000))

    order = {
        "symbol": SYMBOL,
        "volume": VOLUME,
        "leverage": LEVERAGE,
        "side": "OPEN_LONG",
        "type": "MARKET",
        "openType": "ISOLATED",
        "positionMode": "ONE_WAY_MODE",
        "externalOid": f"bot_open_{int(time.time())}_buy"
    }

    signature = sign_request(order, timestamp)

    headers = {
        "X-MEXC-APIKEY": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json"
    }

    logger.info(f"Ордер MEXC: {order}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(1, 4):
            logger.info(f"Попытка {attempt}/3 (async HTTPS)")
            try:
                response = await client.post(url, json=order, headers=headers)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("success"):
                        logger.info(f"УСПЕХ! Ордер ID: {result['data']}")
                        return result
                    else:
                        logger.warning(f"MEXC ошибка: {result}")
                        return result
                else:
                    text = response.text
                    logger.error(f"HTTP {response.status_code}: {text}")
                    if attempt == 3:
                        raise HTTPException(status_code=500, detail=f"MEXC API error: {text}")
            except httpx.TimeoutException:
                logger.warning(f"ТАЙМАУТ на попытке {attempt}")
                if attempt < 3:
                    await asyncio.sleep(2)
                continue
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                if attempt == 3:
                    raise

    raise HTTPException(status_code=500, detail="Не удалось отправить ордер")

# === Получение баланса ===
async def get_balance():
    url = f"{BASE_URL}/api/v1/private/account/assets"
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(API_SECRET.encode(), f"timestamp={timestamp}".encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-MEXC-APIKEY": API_KEY,
        "timestamp": timestamp,
        "signature": signature
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                usdt = next((x for x in data["data"] if x["currency"] == "USDT"), None)
                if usdt:
                    total = float(usdt["positionMargin"]) + float(usdt["availableBalance"])
                    free = float(usdt["availableBalance"])
                    frozen = float(usdt["frozenBalance"])
                    logger.info(f"Баланс: Всего {total:.4f}, Свободно {free:.4f}, Занято {frozen:.4f}")
                    return {"total": total, "free": free, "frozen": frozen}
        except Exception as e:
            logger.error(f"Ошибка баланса: {e}")
    return {"total": 0, "free": 0, "frozen": 0}

# === Получение цены ===
async def get_price():
    url = f"https://contract.mexc.com/api/v1/contract/ticker?symbol={SYMBOL}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                price = data["data"]["fairPrice"]
                logger.info(f"Цена {SYMBOL}: {price}")
                return price
        except Exception as e:
            logger.error(f"Ошибка цены: {e}")
    return None

# === Веб-страница ===
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    balance = await get_balance()
    price = await get_price()
    return f"""
    <h1>MEXC Futures Bot (XRP/USDT)</h1>
    <p><strong>Баланс:</strong> Всего {balance['total']:.4f} USDT | Свободно {balance['free']:.4f}</p>
    <p><strong>Цена:</strong> {price or '—'} USDT</p>
    <form method="post" action="/order">
        <button type="submit">Открыть ордер (4 USDT, Long)</button>
    </form>
    <pre id="log"></pre>
    <script>
        setInterval(() => location.reload(), 10000);
    </script>
    """

# === API: Открыть ордер ===
@app.post("/order")
async def create_order():
    await get_balance()
    await get_price()
    result = await place_mexc_order()
    return result

# === Запуск ===
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
