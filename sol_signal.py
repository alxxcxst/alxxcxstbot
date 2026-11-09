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
LEVERAGE = "6x"

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
        print(f"Error enviando mensaje: {e}")

def get_action_buttons():
    return {
        "inline_keyboard": [
            [
                {"text": "Abrir Phantom", "url": PHANTOM_URL},
                {"text": "TradingView", "url": TRADINGVIEW_URL}
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
                        
                        trend_badge = "LONG Bias (Sobre EMA 200)" if c['close'] > c['ema200'] else "SHORT Bias (Bajo EMA 200)"
                        
                        resp = (
                            "<b>SOLANA · MONITOR DE MERCADO</b>\n"
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
                            "<b>CENTRO DE ESTADO · NUBE</b>\n"
                            "────────────────────────────\n"
                            "▫️ <b>Servidor:</b>   Render · En linea (200 OK)\n"
                            "▫️ <b>Keep-Alive:</b> UptimeRobot activo\n"
                            f"▫️ <b>Ciclos:</b>       {candles_scanned_count} velas analizadas\n"
                            f"▫️ <b>Trade activo:</b> <code>{trade_badge}</code>\n"
                            "────────────────────────────"
                        )
                        send_telegram_alert(resp)

                    elif text in ["/start", "/ayuda", "/help"]:
                        resp = (
                            "<b>PANEL DE COMANDOS DISPONIBLES</b>\n"
                            "────────────────────────────\n"
                            "• <code>/precio</code> — Cotizacion en vivo, RSI y EMA 200\n"
                            "• <code>/status</code> — Diagnostico del bot en la nube\n"
                            "• <code>/ayuda</code>  — Lista de comandos del bot\n"
                            "────────────────────────────"
                        )
                        send_telegram_alert(resp)

        except Exception:
            time.sleep(5)
        time.sleep(1)

# ==========================================
# 6. BUCLE PRINCIPAL DE MERCADO
# ==========================================
def bot_loop():
    global active_trade, last_processed_candle, candles_scanned_count
    time.sleep(4)
    
    boot_msg = (
        "<b>SISTEMA ALPHA SOL · CONECTADO</b>\n"
        "────────────────────────────\n"
        f"▫️ <b>Par:</b>          <code>{SYMBOL}</code>\n"
        f"▫️ <b>Intervalo:</b>    <code>{TIMEFRAME}</code>\n"
        f"▫️ <b>Apalancamiento:</b> <code>{LEVERAGE}</code>\n"
        "▫️ <b>Filtros:</b>     EMA 200 + RSI + Vol MA20\n"
        "────────────────────────────\n"
        "<i>Escribe /precio o /status en cualquier momento.</i>"
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

            # Vigilancia Break-Even
            if active_trade and not active_trade.get('be_triggered'):
                entry = active_trade['entry']
                t_type = active_trade['type']

                if (t_type == "LONG" and live_price >= entry * (1 + BE_PCT)) or \
                   (t_type == "SHORT" and live_price <= entry * (1 - BE_PCT)):
                    
                    be_card = (
                        f"🛡️ <b>AJUSTE BREAK-EVEN · {t_type}</b>\n"
                        "────────────────────────────\n"
                        f"La posicion avanza con <b>+1.5%</b> a favor (<code>${live_price:.2f}</code>).\n\n"
                        f"👉 <b>Mueve tu Stop-Loss en Phantom a tu entrada:</b>\n"
                        f"<code>{entry:.2f}</code>\n\n"
                        "<i>Operacion blindada a riesgo cero.</i>\n"
                        "────────────────────────────"
                    )
                    send_telegram_alert(be_card, reply_markup=get_action_buttons())
                    active_trade['be_triggered'] = True

            # Analisis vela cerrada
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
                        "🟢 <b>SEÑAL DE ENTRADA · LONG</b>\n"
                        "────────────────────────────\n"
                        f"▫️ <b>Entrada:</b>    <code>{price:.2f}</code>\n"
                        f"▫️ <b>Take-Profit:</b> <code>{tp:.2f}</code> (+3.0%)\n"
                        f"▫️ <b>Stop-Loss:</b>   <code>{sl:.2f}</code> (-1.5%)\n"
                        "────────────────────────────\n"
                        f"▫️ <b>Margen:</b>     <code>$2.00 USDC</code> (6x aislado)\n"
                        f"▫️ <b>Ratio R/B:</b>   <code>1:2</code>\n"
                        f"▫️ <b>Setup:</b>       RSI {rsi:.1f} rebote alcista + Vol\n"
                        "────────────────────────────"
                    )
                    send_telegram_alert(card_long, reply_markup=get_action_buttons())
                    active_trade = {'type': 'LONG', 'entry': price, 'be_triggered': False}

                # Señal SHORT
                elif price < ema and prev_rsi > 65 and rsi <= 65 and has_volume:
                    sl = price * (1 + SL_PCT)
                    tp = price * (1 - TP_PCT)
                    
                    card_short = (
                        "🔴 <b>SEÑAL DE ENTRADA · SHORT</b>\n"
                        "────────────────────────────\n"
                        f"▫️ <b>Entrada:</b>    <code>{price:.2f}</code>\n"
                        f"▫️ <b>Take-Profit:</b> <code>{tp:.2f}</code> (+3.0%)\n"
                        f"▫️ <b>Stop-Loss:</b>   <code>{sl:.2f}</code> (-1.5%)\n"
                        "────────────────────────────\n"
                        f"▫️ <b>Margen:</b>     <code>$2.00 USDC</code> (6x aislado)\n"
                        f"▫️ <b>Ratio R/B:</b>   <code>1:2</code>\n"
                        f"▫️ <b>Setup:</b>       RSI {rsi:.1f} rechazo bajista + Vol\n"
                        "────────────────────────────"
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
