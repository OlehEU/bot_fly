import os
import json
import asyncio
import logging
import hmac
import hashlib
import time
import aiohttp
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mexc-bot")

# === Проверка секретов ===
REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MEXC_API_KEY", "MEXC_API_SECRET", "WEBHOOK_SECRET"]
for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        raise EnvironmentError(f"ОШИБКА: {secret} не задан! Установи: fly secrets set {secret}=...")

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
RISK_PERCENT = float(os.getenv("RISK_PERCENT", 25))
SYMBOL = os.getenv("SYMBOL", "XRP/USDT:USDT")  # Стандартный формат ccxt
LEVERAGE = int(os.getenv("LEVERAGE", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logger.info("=== ИНИЦИАЛИЗАЦИЯ MEXC БОТА ===")

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)

# === MEXC Exchange ===
exchange = ccxt.mexc({
    'apiKey': MEXC_API_KEY,
    'secret': MEXC_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

# === FastAPI ===
app = FastAPI()
last_trade_info = None
active_position = False

# === Вспомогательные функции ===
async def get_current_price(symbol: str = SYMBOL) -> float:
    """Получить текущую цену символа"""
    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = float(ticker['last'])
        logger.info(f"Текущая цена {symbol}: {price:.6f}")
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return 0.0

async def check_balance() -> float:
    """Проверить баланс USDT через ccxt (рабочий метод)"""
    logger.info("Проверка баланса USDT...")
    try:
        balance_data = await exchange.fetch_balance()
        usdt = balance_data['total'].get('USDT', 0)
        logger.info(f"Баланс USDT: {usdt:.4f}")
        return float(usdt)
    except Exception as e:
        logger.error(f"Ошибка баланса: {e}")
        return 0.0

async def set_leverage_for_mexc(symbol: str, leverage: int, side: str):
    """Установить плечо для MEXC с правильными параметрами"""
    try:
        # Параметры для MEXC futures
        params = {
            'openType': 1,  # 1 = isolated, 2 = cross
            'positionType': 1 if side == 'buy' else 2  # 1 = long, 2 = short
        }
        
        await exchange.set_leverage(leverage, symbol, params)
        logger.info(f"✅ Плечо {leverage}x установлено для {side}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка установки плеча: {e}")
        # Пробуем установить без параметров
        try:
            await exchange.set_leverage(leverage, symbol)
            logger.info(f"✅ Плечо {leverage}x установлено (упрощенный метод)")
            return True
        except Exception as e2:
            logger.error(f"❌ Ошибка упрощенной установки плеча: {e2}")
            return False

async def calculate_qty(usd_amount: float) -> float:
    """Рассчитать количество для ордера"""
    try:
        # Загружаем markets
        await exchange.load_markets()
        
        # Получаем информацию о символе
        market = exchange.markets[SYMBOL]
        min_qty = market['limits']['amount']['min']
        
        # Получаем цену
        price = await get_current_price()
        if price <= 0:
            raise ValueError("Не удалось получить цену")
        
        # Рассчитываем количество
        raw_qty = usd_amount / price
        qty = exchange.amount_to_precision(SYMBOL, raw_qty)
        qty = float(qty)
        
        # Проверяем минимальное количество
        if qty < min_qty:
            qty = min_qty
            logger.info(f"Используем минимальное количество: {qty}")
            
        logger.info(f"Рассчитано количество: {qty} {SYMBOL} за {usd_amount} USDT")
        return qty
        
    except Exception as e:
        logger.error(f"Ошибка расчета количества: {e}")
        # Упрощенный расчет как запасной вариант
        try:
            price = await get_current_price()
            if price > 0:
                simple_qty = usd_amount / price
                simple_qty = round(simple_qty, 1)
                if simple_qty < 1:
                    simple_qty = 1.0
                logger.info(f"Используем упрощенный расчет: {simple_qty}")
                return simple_qty
        except:
            pass
        return 0.0

async def close_existing_positions():
    """Закрыть все существующие позиции"""
    try:
        positions = await exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos['contracts'] and float(pos['contracts']) > 0:
                logger.info(f"Закрываем существующую позицию: {pos['side']} {pos['contracts']}")
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                await exchange.create_market_order(SYMBOL, close_side, abs(float(pos['contracts'])))
                await asyncio.sleep(1)
                return True
        return False
    except Exception as e:
        logger.error(f"Ошибка при закрытии позиций: {e}")
        return False

async def check_all_balances():
    """Проверить все доступные балансы"""
    try:
        logger.info("🔍 ПРОВЕРКА ВСЕХ БАЛАНСОВ...")
        
        # Основной баланс через ccxt
        balance = await check_balance()
        
        # Детальная информация о балансе
        try:
            balance_data = await exchange.fetch_balance()
            total_balance = balance_data['total']
            
            # Формируем отчет по всем валютам с балансом > 0
            balances_report = []
            for currency, total in total_balance.items():
                if total > 0:
                    balances_report.append(f"  • {currency}: {total:.4f}")
            
            balances_text = "\n".join(balances_report) if balances_report else "  • Нет средств"
            
        except Exception as e:
            logger.error(f"Ошибка детального баланса: {e}")
            balances_text = "  • Ошибка загрузки"
        
        # Цена
        price = await get_current_price()
        
        diagnostics = f"""
🔍 ДИАГНОСТИКА БАЛАНСОВ:

💰 ОСНОВНОЙ БАЛАНС:
• USDT: {balance:.2f}

📊 ВСЕ ВАЛЮТЫ:
{balances_text}

📈 ТОРГОВЛЯ:
• Символ: {SYMBOL}
• Цена: ${price:.4f}
• Плечо: {LEVERAGE}x
• Риск: {RISK_PERCENT}%

💡 СТАТУС:
{f"✅ ГОТОВ К ТОРГОВЛЕ" if balance > 5 else "⚠️ МАЛО СРЕДСТВ"}
"""
        
        logger.info(diagnostics)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=diagnostics)
        
        return balance > 5
        
    except Exception as e:
        error_msg = f"❌ Ошибка проверки балансов: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        return False

async def open_position(signal: str, amount_usd=None):
    """Открыть позицию с исправленной установкой плеча"""
    global last_trade_info, active_position
    
    try:
        logger.info(f"=== ПОПЫТКА ОТКРЫТИЯ ПОЗИЦИИ {signal.upper()} ===")
        
        # Проверяем баланс
        if not await check_all_balances():
            raise ValueError("Проблемы с балансом или недостаточно средств")
        
        # Закрываем существующие позиции
        had_position = await close_existing_positions()
        if had_position:
            await asyncio.sleep(2)

        balance = await check_balance()
        if balance <= 0:
            raise ValueError(f"Баланс = {balance:.2f} USDT")

        usd = amount_usd or (balance * RISK_PERCENT / 100)
        logger.info(f"Риск: {RISK_PERCENT}% → {usd:.2f} USDT из {balance:.2f}")

        if usd < 5:
            raise ValueError(f"Слишком маленький лот: {usd:.2f} USDT (мин. 5 USDT)")

        # Рассчитываем количество
        qty = await calculate_qty(usd)
        if qty <= 0:
            raise ValueError(f"Неверный qty: {qty}")

        side = "buy" if signal.lower() == "buy" else "sell"
        logger.info(f"Открываем {side.upper()} {qty} {SYMBOL}")

        # Устанавливаем плечо с правильными параметрами для MEXC
        leverage_success = await set_leverage_for_mexc(SYMBOL, LEVERAGE, side)
        if not leverage_success:
            logger.warning("Не удалось установить плечо, пробуем продолжить")

        # Создаем рыночный ордер
        order = await exchange.create_market_order(SYMBOL, side, qty)
        logger.info(f"Ордер создан: {order}")

        # Получаем цену входа
        entry = await get_current_price()
        if order.get('filled', 0) > 0 and order.get('average'):
            entry = order['average']

        # Рассчитываем TP/SL
        if side == "buy":
            tp = round(entry * 1.015, 6)  # +1.5%
            sl = round(entry * 0.99, 6)   # -1%
        else:
            tp = round(entry * 0.985, 6)  # -1.5%
            sl = round(entry * 1.01, 6)   # +1%

        # Создаем TP/SL ордера (лимитные)
        try:
            tp_order = await exchange.create_order(
                SYMBOL, 'limit', 
                'sell' if side == "buy" else 'buy', 
                qty, tp, 
                {'reduceOnly': True}
            )
            logger.info(f"TP ордер создан: {tp}")
        except Exception as e:
            logger.warning(f"Не удалось создать TP: {e}")

        try:
            sl_order = await exchange.create_order(
                SYMBOL, 'limit', 
                'sell' if side == "buy" else 'buy', 
                qty, sl, 
                {'reduceOnly': True}
            )
            logger.info(f"SL ордер создан: {sl}")
        except Exception as e:
            logger.warning(f"Не удалось создать SL: {e}")

        active_position = True
        last_trade_info = {
            "signal": signal, 
            "side": side,
            "qty": qty, 
            "entry": entry, 
            "tp": tp, 
            "sl": sl,
            "order_id": order.get('id', 'N/A'),
            "timestamp": time.time()
        }

        msg = (f"✅ {side.upper()} ОТКРЫТА\n"
               f"Символ: {SYMBOL}\n"
               f"Количество: {qty}\n"
               f"Вход: ${entry:.4f}\n"
               f"TP: ${tp:.4f} | SL: ${sl:.4f}\n"
               f"Баланс: {balance:.2f} USDT")
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🎉 ПОЗИЦИЯ УСПЕШНО ОТКРЫТА!")

    except Exception as e:
        err_msg = f"❌ Ошибка открытия {signal}: {str(e)}"
        logger.error(err_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=err_msg)
        except:
            pass
        active_position = False

# === FastAPI Routes ===
@app.on_event("startup")
async def startup_event():
    """Запуск при старте приложения"""
    try:
        logger.info("🚀 ЗАПУСК БОТА")
        await asyncio.sleep(2)
        
        # Проверяем все балансы
        await check_all_balances()
        
        balance = await check_balance()
        price = await get_current_price()
        
        msg = f"""✅ MEXC Futures Bot ЗАПУЩЕН!

📊 СТАТУС:
• Символ: {SYMBOL}
• Цена: ${price:.4f}
• Баланс: {balance:.2f} USDT
• Плечо: {LEVERAGE}x
• Риск: {RISK_PERCENT}%

💡 Отправьте webhook сигнал для торговли."""
        
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info("🤖 БОТ УСПЕШНО ЗАПУЩЕН")
        
    except Exception as e:
        error_msg = f"❌ Ошибка при старте: {e}"
        logger.error(error_msg)
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=error_msg)
        except:
            pass

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook для получения торговых сигналов"""
    logger.info("📨 ПОЛУЧЕН WEBHOOK ЗАПРОС")
    
    if WEBHOOK_SECRET and request.headers.get("Authorization") != f"Bearer {WEBHOOK_SECRET}":
        raise HTTPException(401, detail="Unauthorized")

    try:
        data = await request.json()
        signal = data.get("signal")
        amount = data.get("amount")
        
        logger.info(f"Webhook данные: signal={signal}, amount={amount}")
        
        if signal not in ["buy", "sell"]:
            return {"status": "error", "message": "signal must be 'buy' or 'sell'"}
        
        # Запускаем открытие позиции в фоне
        asyncio.create_task(open_position(signal, amount))
        
        logger.info(f"✅ Сигнал {signal} принят в обработку")
        return {"status": "ok", "message": f"{signal} signal received"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def home():
    """Главная страница"""
    global last_trade_info, active_position
    
    balance = await check_balance()
    price = await get_current_price()
    
    status = "АКТИВНА" if active_position else "НЕТ"
    
    html = f"""
    <html>
        <head>
            <title>MEXC Futures Bot</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial; background: #1e1e1e; color: white; padding: 20px; }}
                .card {{ background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px; }}
                .success {{ color: #00b894; }}
                .warning {{ color: #fdcb6e; }}
                .error {{ color: #e74c3c; }}
            </style>
        </head>
        <body>
            <h1 class="success">🤖 MEXC Futures Trading Bot</h1>
            
            <div class="card">
                <h3>💰 БАЛАНС</h3>
                <p><b>USDT:</b> <span class="{'success' if balance > 0 else 'error'}">{balance:.2f} USDT</span></p>
                <p><a href="/balances">📊 Подробный отчет по балансам</a></p>
            </div>
            
            <div class="card">
                <h3>📊 СТАТУС ТОРГОВЛИ</h3>
                <p><b>Символ:</b> {SYMBOL}</p>
                <p><b>Цена:</b> ${price:.4f}</p>
                <p><b>Позиция:</b> <span class="{'success' if active_position else 'warning'}">{status}</span></p>
                <p><b>Риск:</b> {RISK_PERCENT}%</p>
                <p><b>Плечо:</b> {LEVERAGE}x</p>
            </div>
            
            <div class="card">
                <h3>📈 Последняя сделка</h3>
                <pre>{json.dumps(last_trade_info, indent=2, ensure_ascii=False) if last_trade_info else "Нет данных"}</pre>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(html)

@app.get("/balances")
async def balances_page():
    """Страница с детальными балансами"""
    try:
        balance_data = await exchange.fetch_balance()
        total_balance = balance_data['total']
        
        balances_html = ""
        for currency, total in total_balance.items():
            if total > 0:
                balances_html += f'<p><b>{currency}:</b> {total:.4f}</p>'
        
        if not balances_html:
            balances_html = "<p>Нет средств на счете</p>"
            
    except Exception as e:
        balances_html = f"<p>Ошибка загрузки: {str(e)}</p>"
    
    html = f"""
    <html>
        <head><title>Балансы</title></head>
        <body style="font-family: Arial; background: #1e1e1e; color: white; padding: 20px;">
            <h1>💰 ДЕТАЛЬНЫЕ БАЛАНСЫ</h1>
            
            <div style="background: #2d2d2d; padding: 20px; margin: 10px 0; border-radius: 10px;">
                <h3>🎯 ВСЕ ВАЛЮТЫ</h3>
                {balances_html}
            </div>
            
            <br>
            <a href="/">← Назад</a>
        </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
