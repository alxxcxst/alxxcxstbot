import os
import time
import threading
import requests
import pandas as pd
import ccxt
from flask import Flask

# ==========================================
# 1. PARÁMETROS Y CREDENCIALES
# ==========================================
TELEGRAM_BOT_TOKEN = "9177159589:AAEdG7oQpKfAQO6osw9AtO4HwgJJW43B4dE"
TELEGRAM_CHAT_ID = "8459722038"

SYMBOL = "SOL/USDT"
TIMEFRAME = "15m"
LIMIT_CANDLES = 250

SL_PCT = 0.015  # -1.5% Stop Loss
TP_PCT = 0.030  # +3.0% Take Profit
BE_PCT = 0.015  # +1.5% Activación Break-Even
LEVERAGE = "6x"

PHANTOM_URL = "https://phantom.app/ul/browse/https%3A%2F%2Fphantom.app"
TRADINGVIEW_URL = "https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT"

# Variables de seguimiento
active_trade = None
last_processed_candle = None
candles_scanned_count = 0

# ==========================================
# 2. SERVIDOR KEEP-ALIVE (FLASK)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "SOL Alpha Bot activo y en línea.", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# 3. INTERFAZ TELEGRAM (HTML & BOTONES)
# ==========================================
def send_telegram_alert(message, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error de red con Telegram: {e}")

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
# 4. MOTOR DE INDICADORES
# ==========================================
def calculate_indicators(df):
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    df['vol_ma20'] = df['volume'].rolling(20).mean()

    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))
    return df

# ==========================================
# 5. LISTENER DE COMANDOS
# ==========================================
def telegram_listener():
    offset = 0
    exchange = ccxt.binance({"enableRateLimit": True})
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(url, timeout=25).json()
            
            if "result" in res:
                for update in res["result"]:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "").strip().lower()

                    if chat_id != TELEGRAM_CHAT_ID:
                        continue

                    if text == "/precio":
                        candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=210)
                        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df = calculate_indicators(df)
                        c = df.iloc[-1]
                        
                        trend_badge = "🟢 LONG Bias (Sobre EMA 200)" if c['close'] > c['ema200'] else "🔴 SHORT Bias (Bajo EMA 200)"
                        
                        resp = (
                            "📊 <b>SOLANA · MONITOR DE MERCADO</b>\n"
                            "────────────────────────────\n"
                            f"▫️ <b>Precio Spot:</b> <code>${c['close']:.2f}</code>\n"
                            f"▫️ <b>EMA 200:</b>      <code>${c['ema200']:.2f}</code>\n"
                            f"▫️ <b>RSI (14):</b>     <code>{c['rsi']:.1f}</code>\n"
                            f"▫️ <b>Sesgo:</b>        {trend_badge}\n"
                            "────────────────────────────"
                        )
                        send_telegram_alert(resp, reply_markup=get_action_buttons())

                    elif text == "/status":
                        trade_badge = "Sin posiciones activas"
                        if active_trade:
                            trade_badge = f"{active_trade['type']} @ ${active_trade['entry']:.2f}"

                        resp = (
                            "⚡ <b>CENTRO DE ESTADO · NUBE</b>\n"
                            "────────────────────────────\n"
                            "▫️ <b>Servidor:</b>   Render · En línea (200 OK)\n"
                            "▫️ <b>Keep-Alive:</b> UptimeRobot activo\n"
                            f"▫️ <b>Ciclos:</b>       {candles_scanned_count} velas analizadas\n"
                            f"▫️ <b>Trade activo:</b> <code>{trade_badge}</code>\n"
                            "────────────────────────────"
                        )
                        send_telegram_alert(resp)

                    elif text in ["/start
