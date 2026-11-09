import os
import time
import threading
import requests
import pandas as pd
from flask import Flask

# ==========================================
# 1. PARAMETROS Y CREDENCIALES
# ==========================================
TELEGRAM_BOT_TOKEN = "9177159589:AAEdG7oQpKfAQO6osw9AtO4HwgJJW43B4dE"
TELEGRAM_CHAT_ID = "8459722038"

SYMBOL = "SOL/USDT"
TIMEFRAME = "15m"

SL_PCT = 0.015  # -1.5% Stop Loss
TP_PCT = 0.030  # +3.0% Take Profit
BE_PCT = 0.015  # +1.5% Break-Even

PHANTOM_URL = "https://phantom.app/ul/browse/https%3A%2F%2Fphantom.app"
TRADINGVIEW_URL = "https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT"

active_trade = None
last_processed_candle = None
candles_scanned_count = 0
latest_market_data = {}

# ==========================================
# 2. SERVIDOR KEEP-ALIVE FLASK
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "SOL Alpha Bot activo 24/7.", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# 3. INTERFAZ TELEGRAM
# ==========================================
def send_telegram_alert(message, target_chat_id=None, reply_markup=None):
    cid = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Error enviando mensaje: {e}")
        return None

def get_action_buttons():
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ Abrir Phantom", "url": PHANTOM_URL},
                {"text": "📈 TradingView", "url": TRADINGVIEW_URL}
            ]
        ]
    }

# ==========================================
# 4. DATOS DE MERCADO E INDICADORES
# ==========================================
def fetch_candles_direct():
    # Descarga directa de velas 15m (Coinbase no bloquea Render)
    # Granularity 900 = 15 minutos
    url = "https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=900"
    headers = {"User-Agent
