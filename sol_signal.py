import os
import time
import threading
import requests
import pandas as pd
from flask import Flask

# ==========================================
# 1. PARÁMETROS Y VARIABLES DE ENTORNO
# ==========================================
TELEGRAM_BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGRAM_TOKEN")
    or os.environ.get("BOT_TOKEN")
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "8459722038"

SYMBOL = "SOL-PERP"
TIMEFRAME = "5m"

# Gestión de capital ($2.00 USDC x 4 = $8.00 USD nocional)
MARGIN_USDC = float(os.environ.get("MARGIN_USDC", "2.0"))
LEVERAGE = int(os.environ.get("LEVERAGE", "4"))

# Gestión de riesgo R:R 1:2 estricto
SL_PCT = 0.015          # 1.5% Stop Loss
TP_PCT = 0.030          # 3.0% Take Profit
BE_TRIGGER_PCT = 0.012  # Activar Break-Even al +1.2%

# Filtros Anti-Ruido
RSI_SHORT_THRESHOLD = 60  # Cruce a la baja desde sobrecompra
RSI_LONG_THRESHOLD = 40   # Cruce al alza desde sobreventa
VOL_FACTOR = 0.8          # Volumen mínimo: vol > vol_avg * 0.8

# Variables de estado
latest_market_data = {"price": None, "ema": None, "rsi": None}
active_trade = None
candles_scanned_count = 0
last_bot_message_id = None
threads_started = False

app = Flask(__name__)

# ==========================================
# 2. MOTOR DE COMUNICACIÓN TELEGRAM (ANTI-SPAM)
# ==========================================
def delete_telegram_message(chat_id, message_id):
    if not TELEGRAM_BOT_TOKEN or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=3)
    except Exception:
        pass

def send_telegram_alert(message, target_chat_id=None, clean_previous=False):
    global last_bot_message_id
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return

    dest_id = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID

    if clean_previous and last_bot_message_id:
        delete_telegram_message(dest_id, last_bot_message_id)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": dest_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            res_json = r.json()
            if clean_previous:
                last_bot_message_id = res_json.get("result", {}).get("message_id")
    except Exception as e:
        print(f"[Telegram] Error: {e}")

def send_permanent_welcome(message):
    """Envía un mensaje de bienvenida permanente que NUNCA se borra (clean_previous=False)."""
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False  # Permitir vista previa para enlaces limpios si aplica
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[Telegram] Error bienvenida: {e}")

# ==========================================
# 3. FEED DE MERCADO (COINBASE 5M) Y TÉCNICO
# ==========================================
def get_live_sol_price():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
        res = requests.get(url, headers=headers, timeout=5).json()
        return float(res["data"]["amount"])
    except Exception:
        return None

def fetch_candles_5m():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = "https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=300"
        res = requests.get(url, headers=headers, timeout=10).json()
        if not isinstance(res, list) or len(res) < 210:
            return None
        df = pd.DataFrame(res, columns=["time", "low", "high", "open", "close", "volume"])
        df = df.sort_values(by="time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[Mercado] Error velas: {e}")
        return None

def calculate_technical_indicators(df):
    close = df["close"]
    df["ema_200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi_14"] = 100 - (100 / (1 + rs))

    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    return df

# ==========================================
# 4. GESTIÓN AUTÓNOMA DE SEÑALES Y BREAK-EVEN
# ==========================================
def manage_open_position(current_price):
    global active_trade
    if not active_trade:
        return

    direction = active_trade["type"]
    entry = active_trade["entry"]
    tp = active_trade["tp"]
    sl = active_trade["sl"]
    be_active = active_trade["be_activated"]

    if direction == "LONG":
        if not be_active and current_price >= entry * (1 + BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = round(entry, 2)
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN BLINDADO · LONG</b>\n\n"
                f"Precio: <code>${current_price:.2f}</code> (+1.2%)\n"
                f"Stop Loss movido a entrada: <code>${active_trade['sl']:.2f}</code>"
            )
        elif current_price >= tp:
            send_telegram_alert(
                f"🎯 <b>TAKE PROFIT ALCANZADO (+3.0%)</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (LONG)\n"
                f"Entrada: <code>${entry:.2f}</code>\n"
                f"Salida (TP): <code>${current_price:.2f}</code>"
            )
            active_trade = None
        elif current_price <= sl:
            send_telegram_alert(
                f"🛑 <b>STOP LOSS ALCANZADO</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (LONG)\n"
                f"Entrada: <code>${entry:.2f}</code>\n"
                f"Salida (SL): <code>${current_price:.2f}</code>"
            )
            active_trade = None

    elif direction == "SHORT":
        if not be_active and current_price <= entry * (1 - BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = round(entry, 2)
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN BLINDADO · SHORT</b>\n\n"
                f"Precio: <code>${current_price:.2f}</code> (+1.2%)\n"
                f"Stop Loss movido a entrada: <code>${active_trade['sl']:.2f}</code>"
            )
        elif current_price <= tp:
            send_telegram_alert(
                f"🎯 <b>TAKE PROFIT ALCANZADO (+3.0%)</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (SHORT)\n"
                f"Entrada: <code>${entry:.2f}</code>\n"
                f"Salida (TP): <code>${current_price:.2f}</code>"
            )
            active_trade = None
        elif current_price >= sl:
            send_telegram_alert(
                f"🛑 <b>STOP LOSS ALCANZADO</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (SHORT)\n"
                f"Entrada: <code>${entry:.2f}</code>\n"
                f"Salida (SL): <code>${current_price:.2f}</code>"
            )
            active_trade = None

# ==========================================
# 5. LISTENER TELEGRAM (COMANDOS SIMPLIFICADOS)
# ==========================================
def telegram_listener():
    global active_trade
    offset = None
    print("[Telegram] Listener activo.")

    while True:
        token = TELEGRAM_BOT_TOKEN
        if not token:
            time.sleep(5)
            continue

        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"timeout": 10}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    msg_id = msg.get("message_id")
                    text = msg.get("text", "").strip().lower()

                    if msg_id and (text.startswith("/") or text in ["price", "status", "restart"]):
                        delete_telegram_message(chat_id, msg_id)

                    if text.startswith("/price") or text == "price":
                        p = latest_market_data.get("price") or get_live_sol_price()
                        ema = latest_market_data.get("ema")
                        rsi = latest_market_data.get("rsi")
                        if p and ema:
                            trend = "🟢 Tendencia Alcista (LONG Bias)" if p > ema else "🔴 Tendencia Bajista (SHORT Bias)"
                            resp_text = (
                                f"📊 <b>TELEMETRÍA SOL-PERP (5M)</b>\n\n"
                                f"💵 <b>Precio Spot:</b> <code>${p:.2f}</code>\n"
                                f"📈 <b>EMA 200:</b> <code>${ema:.2f}</code>\n"
                                f"📉 <b>RSI (14):</b> <code>{rsi:.1f}</code>\n"
                                f"⚡ <b>Sesgo:</b> <code>{trend}</code>"
                            )
                        else:
                            resp_text = "⏳ Sincronizando telemetría de mercado..."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/status") or text == "status":
                        pos = f"SÍ ({active_trade['type']} @ ${active_trade['entry']:.2f})" if active_trade else "NO (Escaneando mercado)"
                        resp_text = (
                            f"🟢 <b>ESTADO DEL SISTEMA</b>\n\n"
                            f"• <b>Nodo Render:</b> En línea ⚡\n"
                            f"• <b>Posición Activa:</b> <code>{pos}</code>\n"
                            f"• <b>Velas 5M analizadas:</b> {candles_scanned_count}"
                        )
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/restart") or text == "restart":
                        active_trade = None
                        resp_text = "🔄 <b>SISTEMA REINICIADO</b>\nEstado reseteado a cero. El bot continúa operando."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

        except Exception:
            time.sleep(5)
        time.sleep(1)

# ==========================================
# 6. MOTOR PRINCIPAL DE MERCADO (5M)
# ==========================================
def market_engine():
    global candles_scanned_count, latest_market_data, active_trade
    last_evaluated_time = None
    time.sleep(3)

    # Mensaje de bienvenida permanente con acceso directo a TradingView en temporalidad de 5 minutos (intervalo '5')
    tradingview_url = "https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT.P"
    welcome_card = (
        f"⚡ <b>QUANTITATIVE BOT ACTIVE</b> ⚡\n\n"
        f"Par: <code>SOLUSDT (Perp)</code> | Temporalidad: <code>5M</code>\n"
        f"Filtros anti-ruido y alta frecuencia operando al 100%.\n\n"
        f"🔗 <a href='{tradingview_url}'>Abrir TradingView (SOLUSDT - 5M)</a>"
    )
    send_permanent_welcome(welcome_card)

    while True:
        try:
            live_price = get_live_sol_price()
            if live_price:
                latest_market_data["price"] = live_price
                manage_open_position(live_price)

            df = fetch_candles_5m()
            if df is not None and len(df) >= 200:
                df = calculate_technical_indicators(df)

                closed_candle = df.iloc[-2]
                candle_time = closed_candle["time"]

                latest_market_data["ema"] = float(closed_candle["ema_200"])
                latest_market_data["rsi"] = float(closed_candle["rsi_14"])

                if candle_time != last_evaluated_time:
                    last_evaluated_time = candle_time
                    candles_scanned_count += 1

                    close_p = float(closed_candle["close"])
                    ema_val = float(closed_candle["ema_200"])
                    rsi_val = float(closed_candle["rsi_14"])
                    vol_val = float(closed_candle["volume"])
                    vol_avg = float(closed_candle["vol_ma20"])
                    prev_rsi = float(df.iloc[-3]["rsi_14"])

                    if not active_trade:
                        if vol_val >= (vol_avg * VOL_FACTOR):
                            # Señal LONG
                            if (close_p > ema_val) and (prev_rsi <= RSI_LONG_THRESHOLD and rsi_val > RSI_LONG_THRESHOLD):
                                entry = live_price if live_price else close_p
                                tp = round(entry * (1 + TP_PCT), 2)
                                sl = round(entry * (1 - SL_PCT), 2)
                                notional = MARGIN_USDC * LEVERAGE

                                active_trade = {
                                    "type": "LONG",
                                    "entry": entry,
                                    "tp": tp,
                                    "sl": sl,
                                    "be_activated": False
                                }

                                card = (
                                    f"🚨 <b>SEÑAL LONG · ALTA FRECUENCIA (5M)</b>\n\n"
                                    f"🪙 <b>Activo:</b> <code>{SYMBOL}</code>\n"
                                    f"📈 <b>Entrada:</b> <code>${entry:.2f}</code>\n"
                                    f"💰 <b>Margen:</b> <code>${MARGIN_USDC} USDC ({LEVERAGE}x | Nocional: ${notional:.2f})</code>\n"
                                    f"🎯 <b>Take Profit (+3.0%):</b> <code>${tp:.2f}</code>\n"
                                    f"🛑 <b>Stop Loss (-1.5%):</b> <code>${sl:.2f}</code>\n"
                                    f"🛡️ <b>Break-Even:</b> Automático al +{BE_TRIGGER_PCT*100}%"
                                )
                                send_telegram_alert(card)

                            # Señal SHORT
                            elif (close_p < ema_val) and (prev_rsi >= RSI_SHORT_THRESHOLD and rsi_val < RSI_SHORT_THRESHOLD):
                                entry = live_price if live_price else close_p
                                tp = round(entry * (1 - TP_PCT), 2)
                                sl = round(entry * (1 + SL_PCT), 2)
                                notional = MARGIN_USDC * LEVERAGE

                                active_trade = {
                                    "type": "SHORT",
                                    "entry": entry,
                                    "tp": tp,
                                    "sl": sl,
                                    "be_activated": False
                                }

                                card = (
                                    f"🚨 <b>SEÑAL SHORT · ALTA FRECUENCIA (5M)</b>\n\n"
                                    f"🪙 <b>Activo:</b> <code>{SYMBOL}</code>\n"
                                    f"📉 <b>Entrada:</b> <code>${entry:.2f}</code>\n"
                                    f"💰 <b>Margen:</b> <code>${MARGIN_USDC} USDC ({LEVERAGE}x | Nocional: ${notional:.2f})</code>\n"
                                    f"🎯 <b>Take Profit (+3.0%):</b> <code>${tp:.2f}</code>\n"
                                    f"🛑 <b>Stop Loss (-1.5%):</b> <code>${sl:.2f}</code>\n"
                                    f"🛡️ <b>Break-Even:</b> Automático al +{BE_TRIGGER_PCT*100}%"
                                )
                                send_telegram_alert(card)
        except Exception as e:
            print(f"[Engine] Error en ciclo: {e}")

        time.sleep(20)

# ==========================================
# 7. ARRANQUE SEGURO EN RENDER (FLASK)
# ==========================================
def start_background_workers():
    global threads_started
    if not threads_started:
        threads_started = True
        t_market = threading.Thread(target=market_engine, daemon=True)
        t_market.start()
        t_telegram = threading.Thread(target=telegram_listener, daemon=True)
        t_telegram.start()

start_background_workers()

@app.route("/")
def home():
    return "SOL/USDT Signal Bot Activo 24/7", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
