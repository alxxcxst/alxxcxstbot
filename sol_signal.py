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

# ==========================================
# 5. LISTENER DE COMANDOS
# ==========================================
def telegram_listener():
    offset = None
    exchange = ccxt.binance({"enableRateLimit": True})

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
                        candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=210)
                        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df = calculate_indicators(df)
                        c = df.iloc[-1]
                        
                        trend_badge = "🟢 LONG Bias" if c['close'] > c['ema200'] else "🔴 SHORT Bias"
                        
                        resp = (
                            "📊 <b>SOL/USDT · EN VIVO</b>\n\n"
                            f"Precio: <code>${c['close']:.2f}</code>\n"
                            f"EMA 200: <code>${c['ema200']:.2f}</code>\n"
                            f"RSI (14): <code>{c['rsi']:.1f}</code>\n"
                            f"Sesgo: {trend_badge}"
                        )
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
                            "• <code>/precio</code> — Ver datos en vivo\n"
                            "• <code>/status</code> — Diagnostico del bot\n"
                            "• <code>/ayuda</code> — Lista de comandos"
                        )
                        send_telegram_alert(resp, target_chat_id=chat_id)

        except Exception:
            time.sleep(3)

        time.sleep(1)

# ==========================================
# 6. BUCLE PRINCIPAL DE MERCADO
# ==========================================
def bot_loop():
    global active_trade, last_processed_candle, candles_scanned_count
    time.sleep(4)
    
    boot_msg = (
        "🎯 <b>ALPHA BOT CONECTADO</b>\n\n"
        f"Par: <code>{SYMBOL}</code>\n"
        f"Velas: <code>{TIMEFRAME}</code>\n"
        "Filtros: EMA 200 + RSI + Vol MA20\n\n"
        "<i>Escribe /precio o /status cuando quieras.</i>"
    )
    send_telegram_alert(boot_msg, reply_markup=get_action_buttons())

    exchange = ccxt.binance({"enableRateLimit": True})

    while True:
        try:
            candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_indicators(df)

            live_candle = df.iloc[-1]
            live_price = live_candle['close']

            # --- VIGILANCIA BREAK-EVEN (+1.5%) ---
            if active_trade and not active_trade.get('be_triggered'):
                entry = active_trade['entry']
                t_type = active_trade['type']

                if (t_type == "LONG" and live_price >= entry * (1 + BE_PCT)) or \
                   (t_type == "SHORT" and live_price <= entry * (1 - BE_PCT)):
                    
                    be_badge = "🟢 LONG" if t_type == "LONG" else "🔴 SHORT"
                    be_card = (
                        f"🛡️ <b>BREAK-EVEN · {be_badge}</b>\n\n"
                        f"Ganancia actual: <b>+1.5%</b> (<code>${live_price:.2f}</code>)\n\n"
                        f"👉 Mueve el SL en Phantom a tu entrada:\n"
                        f"<code>{entry:.2f}</code>\n\n"
                        "<i>Operacion asegurada sin riesgo.</i>"
                    )
                    send_telegram_alert(be_card, reply_markup=get_action_buttons())
                    active_trade['be_triggered'] = True

            # --- ANÁLISIS DE VELA CERRADA ---
            current_candle = df.iloc[-2]
            prev_candle = df.iloc[-3]
            candle_time = current_candle['timestamp']

            if candle_time != last_processed_candle:
                candles_scanned_count += 1
                last_processed_candle = candle_time
                
                price = current_candle['close']
                ema = current_candle['ema200']
                rsi = current_candle['rsi']
                prev_rsi = prev_candle['rsi']
                vol = current_candle['volume']
                vol_avg = current_candle['vol_ma20']

                has_volume = vol > vol_avg

                # Señal LONG
                if price > ema and prev_rsi < 35 and rsi >= 35 and has_volume:
                    sl = price * (1 - SL_PCT)
                    tp = price * (1 + TP_PCT)
                    
                    card_long = (
                        "🟢 <b>LONG · SOL/USDT</b> 🔼\n\n"
                        f"Entrada: <code>{price:.2f}</code>\n"
                        f"TP (+3.0%): <code>{tp:.2f}</code>\n"
                        f"SL (-1.5%): <code>{sl:.2f}</code>\n\n"
                        f"Ratio: 1:2\n"
                        f"Filtro: RSI {rsi:.1f} + Volumen confirmado"
                    )
                    send_telegram_alert(card_long, reply_markup=get_action_buttons())
                    active_trade = {'type': 'LONG', 'entry': price, 'be_triggered': False}

                # Señal SHORT
                elif price < ema and prev_rsi > 65 and rsi <= 65 and has_volume:
                    sl = price * (1 + SL_PCT)
                    tp = price * (1 - TP_PCT)
                    
                    card_short = (
                        "🔴 <b>SHORT · SOL/USDT</b> 🔽\n\n"
                        f"Entrada: <code>{price:.2f}</code>\n"
                        f"TP (+3.0%): <code>{tp:.2f}</code>\n"
                        f"SL (-1.5%): <code>{sl:.2f}</code>\n\n"
                        f"Ratio: 1:2\n"
                        f"Filtro: RSI {rsi:.1f} + Volumen confirmado"
                    )
                    send_telegram_alert(card_short, reply_markup=get_action_buttons())
                    active_trade = {'type': 'SHORT', 'entry': price, 'be_triggered': False}

        except Exception as e:
            print(f"Error en escaneo de mercado: {e}")

        time.sleep(60)

# ==========================================
# 7. EJECUCION MULTIHILO
# ==========================================
if __name__ == '__main__':
    t_market = threading.Thread(target=bot_loop)
    t_market.daemon = True
    t_market.start()

    t_tg = threading.Thread(target=telegram_listener)
    t_tg.daemon = True
    t_tg.start()

    run_flask()
