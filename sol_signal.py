import os
import time
import threading
import requests
import ccxt
import pandas as pd
from flask import Flask

# ==========================================
# 1. SERVIDOR WEB LIGERO PARA RENDER
# ==========================================
app = Flask(__name__)

@app.route("/")
def home():
    return "🟢 Bot de Señales SOL activo y monitoreando el mercado 24/7."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# 2. CONFIGURACIÓN DEL BOT Y TELEGRAM
# ==========================================
TELEGRAM_TOKEN = "9177159589:AAEdG7oQpKfAQO6osw9AtO4HwgJJW43B4dE"
CHAT_ID = "8459722038"

SYMBOL = "SOL/USDT"
TIMEFRAME = "15m"

# Conexión a datos públicos de Binance
exchange = ccxt.binance({"enableRateLimit": True})

def send_telegram_alert(message: str):
    """Envía un mensaje formateado a Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=8)
        if response.status_code == 200:
            print("Alerta enviada correctamente a Telegram.")
        else:
            print(f"Error al enviar a Telegram: {response.text}")
    except Exception as e:
        print(f"Error de conexión con Telegram: {e}")

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calcula el RSI de 14 periodos."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

last_alerted_candle_time = None

def scan_market():
    global last_alerted_candle_time
    
    # Descargar últimas 250 velas de 15 minutos
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=250)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    
    # Indicadores
    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["rsi"] = calculate_rsi(df["close"], period=14)
    
    # Velas cerradas (-2 y -3)
    prev_closed = df.iloc[-3]
    last_closed = df.iloc[-2]
    candle_timestamp = last_closed["time"]
    
    if last_alerted_candle_time == candle_timestamp:
        return

    price = float(last_closed["close"])
    ema = float(last_closed["ema_200"])
    rsi_now = float(last_closed["rsi"])
    rsi_prev = float(prev_closed["rsi"])

    # Señal de COMPRA (LONG)
    if price > ema and rsi_prev <= 35 and rsi_now > 35:
        sl = round(price * 0.985, 2)
        tp = round(price * 1.030, 2)
        msg = (
            f"🚀 *SEÑAL LONG CONFIRMADA: {SYMBOL}*\n\n"
            f"• *Temporalidad:* {TIMEFRAME}\n"
            f"• *Precio Entrada:* ${price:.2f}\n"
            f"• *Stop-Loss (SL):* ${sl:.2f} (-1.5%)\n"
            f"• *Take-Profit (TP):* ${tp:.2f} (+3.0%)\n"
            f"• *Ratio Riesgo/Beneficio:* 1:2\n\n"
            f"📊 *Condición:* Precio sobre EMA 200 y RSI ({rsi_now:.1f}) saliendo de sobreventa."
        )
        send_telegram_alert(msg)
        last_alerted_candle_time = candle_timestamp

    # Señal de VENTA (SHORT)
    elif price < ema and rsi_prev >= 65 and rsi_now < 65:
        sl = round(price * 1.015, 2)
        tp = round(price * 0.970, 2)
        msg = (
            f"🔻 *SEÑAL SHORT CONFIRMADA: {SYMBOL}*\n\n"
            f"• *Temporalidad:* {TIMEFRAME}\n"
            f"• *Precio Entrada:* ${price:.2f}\n"
            f"• *Stop-Loss (SL):* ${sl:.2f} (-1.5%)\n"
            f"• *Take-Profit (TP):* ${tp:.2f} (+3.0%)\n"
            f"• *Ratio Riesgo/Beneficio:* 1:2\n\n"
            f"📊 *Condición:* Precio bajo EMA 200 y RSI ({rsi_now:.1f}) perdiendo sobrecompra."
        )
        send_telegram_alert(msg)
        last_alerted_candle_time = candle_timestamp

def bot_loop():
    """Bucle continuo de trading en segundo plano."""
    print("📡 Monitoreo de Binance iniciado en segundo plano...")
    send_telegram_alert("🟢 *Bot de Señales SOL activado en la nube.*")
    
    while True:
        try:
            scan_market()
        except Exception as e:
            print(f"Error en consulta de velas: {e}")
        time.sleep(60)

# ==========================================
# 3. ARRANQUE SIMULTÁNEO (FLASK + BOT)
# ==========================================
if __name__ == "__main__":
    # Inicia el escaneo en un hilo paralelo
    threading.Thread(target=bot_loop, daemon=True).start()
    # Inicia el servidor web principal
    run_web()
