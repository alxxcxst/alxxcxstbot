import os
import time
import threading
import requests
import pandas as pd
import ccxt
from flask import Flask

# ==========================================
# 1. PARAMETROS Y CREDENCIALES
# ==========================================
TELEGRAM_BOT_TOKEN = "9177159589:AAEdG7oQpKfAQO6osw9AtO4HwgJJW43B4dE"
TELEGRAM_CHAT_ID = "8459722038"

SYMBOL = "SOL/USDT"
TIMEFRAME = "15m"
LIMIT_CANDLES = 250

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

def get_live_sol_price():
    # Intenta obtener el precio de Binance, OKX o CoinGecko
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", timeout=5).json()
        if "price" in r:
            return float(r["price"])
    except Exception:
        pass

    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=5).json()
        if "solana" in r and "usd" in r["solana"]:
            return float(r["solana"]["usd"])
    except Exception:
        pass

    return None

# ==========================================
# 5. LISTENER DE COMANDOS
# ==========================================
def telegram_listener():
    offset = None

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 15}
            if offset is not None:
                params["offset"] = offset

            res = requests.get(url, params=params, timeout=20).json()

            if "result" in res and res["result"]:
                for update in res["result"]:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    raw_text = msg.get("text", "").strip()
                    text = raw_text.lower()

                    if text.startswith("/precio") or text == "precio":
                        p = latest_market_data.get("price") or get_live_sol_price()
                        ema = latest_market_data.get("ema")
                        rsi = latest_market_data.get("rsi")

                        if p:
                            ema_str = f"${ema:.2f}" if ema else "Sincronizando..."
                            rsi_str = f"{rsi:.1f}" if rsi else "Sincronizando..."
                            trend = ""
                            if ema:
                                trend = "\nSesgo: 🟢 LONG Bias" if p > ema else "\nSesgo: 🔴 SHORT Bias"

                            resp = (
                                "📊 <b>SOL/USDT · EN VIVO</b>\n\n"
                                f"Precio: <code>${p:.2f}</code>\n"
                                f"EMA 200: <code>{ema_str}</code>\n"
                                f"RSI (14): <code>{rsi_str}</code>"
                                f"{trend}"
                            )
                        else:
                            resp = "⚠️ No se pudo conectar a los feeds de mercado. Reintentando..."

                        send_telegram_alert(resp, target_chat_id=chat_id, reply_markup=get_action_buttons())

                    elif text.startswith("/status") or text == "status":
                        trade_badge = "Ninguna"
                        if active_trade:
                            trade_badge = f"{active_trade['type']} @ ${active_trade['entry']:.2f}"

                        resp = (
                            "⚡ <b>ESTADO DEL BOT</b>\n\n"
                            "Servidor: Render (Activo)\n"
                            f"Velas escaneadas: {candles_scanned_count}\n"
                            f"Posicion: <code>{trade_badge}</code>"
                        )
                        send_telegram_alert(resp, target_chat_id=chat_id)

                    elif text.startswith("/start") or text.startswith("/ayuda") or text.startswith("/help"):
                        resp = (
                            "⚙️ <b>COMANDOS</b>\n\n"
                            "• <code>/precio</code> — Ver precio en tiempo real\n"
                            "• <code>/status</code> — Diagnostico del bot\n"
                            "• <code>/ayuda</code> — Lista de comandos"
                        )
                        send_telegram_alert(resp, target_chat_id=chat_id)

        except Exception as e:
            time.sleep(3)

        time.sleep(1)

# ==========================================
# 6. BUCLE PRINCIPAL DE MERCADO (OKX / BINANCE)
# ==========================================
def bot_loop():
    global active_trade, last_processed_candle, candles_scanned_count, latest_market_data
    time.sleep(3)
    
    boot_msg = (
        "🎯 <b>ALPHA BOT CONECTADO</b>\n\n"
        f"Par: <code>{SYMBOL}</code>\n"
        f"Velas: <code>{TIMEFRAME}</code>\n"
        "Filtros: EMA 200 + RSI + Vol MA20\n\n"
        "<i>Escribe /precio o /status cuando quieras.</i>"
    )
    send_telegram_alert(boot_msg, reply_markup=get_action_buttons())

    # Probar conexión con OKX primero (no bloquea servidores de Render) o Binance de fallback
    try:
        exchange = ccxt.okx({"enableRateLimit": True})
        exchange.load_markets()
    except Exception:
        exchange = ccxt.binance({"enableRateLimit": True})

    while True:
        try:
            # Obtener velas
            try:
                candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            except Exception:
                # Si falla OKX, intenta con Binance
                fallback_ex = ccxt.binance({"enableRateLimit": True})
                candles = fallback_ex.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)

            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_indicators(df)

            live_candle = df.iloc
