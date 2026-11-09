import os
import time
import threading
import requests
import pandas as pd
import ccxt
from flask import Flask

# ==========================================
# 1. CONFIGURACIÓN Y CREDENCIALES
# ==========================================
TELEGRAM_BOT_TOKEN = "9177159589:AAEdG7oQpKfAQO6osw9AtO4HwgJJW43B4dE"
TELEGRAM_CHAT_ID = "8459722038"

SYMBOL = "SOL/USDT"
TIMEFRAME = "15m"
LIMIT_CANDLES = 250

SL_PCT = 0.015  # -1.5% Stop Loss
TP_PCT = 0.030  # +3.0% Take Profit
LEVERAGE = "6x"

# ==========================================
# 2. SERVIDOR WEB FLASK (KEEP-ALIVE)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de Señales SOL activo y monitoreando el mercado 24/7.", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# 3. ENVÍO DE ALERTAS CON HTML A TELEGRAM
# ==========================================
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

# ==========================================
# 4. CÁLCULO DE INDICADORES MATEMÁTICOS
# ==========================================
def calculate_indicators(df):
    # Media Móvil Exponencial (EMA 200)
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    # Índice de Fuerza Relativa (RSI 14)
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))
    return df

# ==========================================
# 5. BUCLE PRINCIPAL DE ANÁLISIS
# ==========================================
def bot_loop():
    time.sleep(5)  # Breve pausa para asegurar el arranque del servidor web
    
    startup_msg = (
        "┏━━━━━━━━━━━━━━━━━━━━━┓\n"
        "⚡ <b>SISTEMA DE SEÑALES ACTIVO</b>\n"
        "┗━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"💎 <b>Activo:</b> <code>{SYMBOL}</code>\n"
        f"⏱ <b>Temporalidad:</b> <code>{TIMEFRAME}</code>\n"
        f"⚙️ <b>Apalancamiento:</b> <code>{LEVERAGE}</code>\n"
        "📡 <i>Escaneando el mercado en segundo plano...</i>"
    )
    send_telegram_alert(startup_msg)

    exchange = ccxt.binance({"enableRateLimit": True})
    last_signal_timestamp = None

    while True:
        try:
            # Descarga de datos OHLCV
            candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_indicators(df)

            # Tomar las últimas velas cerradas para confirmación
            current_candle = df.iloc[-2]   # Última vela cerrada completamente
            prev_candle = df.iloc[-3]      # Penúltima vela cerrada

            candle_time = current_candle['timestamp']
            price = current_candle['close']
            ema = current_candle['ema200']
            rsi = current_candle['rsi']
            prev_rsi = prev_candle['rsi']

            if candle_time != last_signal_timestamp:
                # Condición 1: LONG (Precio > EMA 200 y rebote alcista de RSI saliendo de 35)
                if price > ema and prev_rsi < 35 and rsi >= 35:
                    sl = price * (1 - SL_PCT)
                    tp = price * (1 + TP_PCT)
                    
                    alert_long = (
                        "┏━━━━━━━━━━━━━━━━━━━━━┓\n"
                        "🟢 <b>NUEVA SEÑAL · SOL/USDT</b>\n"
                        "<i>(Largo / Compra)</i>\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                        f"🎯 <b>Precio Entrada:</b> <code>{price:.2f}</code>\n"
                        f"🛡️ <b>Stop-Loss (-1.5%):</b> <code>{sl:.2f}</code>\n"
                        f"💰 <b>Take-Profit (+3.0%):</b> <code>{tp:.2f}</code>\n\n"
                        f"⚙️ <b>Apalancamiento:</b> <code>{LEVERAGE}</code>\n"
                        "📊 <b>Ratio Riesgo/Beneficio:</b> <code>1:2</code>\n"
                        f"📈 <b>Filtro:</b> Precio > EMA200 | RSI ({rsi:.1f}) saliendo de sobreventa."
                    )
                    send_telegram_alert(alert_long)
                    last_signal_timestamp = candle_time

                # Condición 2: SHORT (Precio < EMA 200 y quiebre bajista de RSI cayendo de 65)
                elif price < ema and prev_rsi > 65 and rsi <= 65:
                    sl = price * (1 + SL_PCT)
                    tp = price * (1 - TP_PCT)
                    
                    alert_short = (
                        "┏━━━━━━━━━━━━━━━━━━━━━┓\n"
                        "🔴 <b>NUEVA SEÑAL · SOL/USDT</b>\n"
                        "<i>(Corto / Venta)</i>\n"
                        "┗━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                        f"🎯 <b>Precio Entrada:</b> <code>{price:.2f}</code>\n"
                        f"🛡️ <b>Stop-Loss (-1.5%):</b> <code>{sl:.2f}</code>\n"
                        f"💰 <b>Take-Profit (+3.0%):</b> <code>{tp:.2f}</code>\n\n"
                        f"⚙️ <b>Apalancamiento:</b> <code>{LEVERAGE}</code>\n"
                        "📊 <b>Ratio Riesgo/Beneficio:</b> <code>1:2</code>\n"
                        f"📉 <b>Filtro:</b> Precio < EMA200 | RSI ({rsi:.1f}) saliendo de sobrecompra."
                    )
                    send_telegram_alert(alert_short)
                    last_signal_timestamp = candle_time

        except Exception as e:
            print(f"Error al analizar el mercado: {e}")

        # Pausa de 60 segundos antes de volver a consultar
        time.sleep(60)

# ==========================================
# 6. ARRANQUE
# ==========================================
if __name__ == '__main__':
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    run_flask()
