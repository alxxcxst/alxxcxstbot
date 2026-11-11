import os
import time
import threading
import requests
import pandas as pd
from flask import Flask, jsonify

# Bloqueo de procesos para evitar duplicidad de hilos en Gunicorn/Render (Linux/macOS)
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

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
GRANULARITY = 300  # 5 minutos en segundos

# Gestión de capital ($2.00 USDC x 4 = $8.00 USD nocional)
MARGIN_USDC = float(os.environ.get("MARGIN_USDC", "2.0"))
LEVERAGE = int(os.environ.get("LEVERAGE", "4"))

# Gestión de riesgo R:R 1:2 estricto
SL_PCT = 0.015          # 1.5% Stop Loss
TP_PCT = 0.030          # 3.0% Take Profit
BE_TRIGGER_PCT = 0.012  # Activar Break-Even al +1.2%

# Filtros Cuantitativos Optimizado (5M)
RSI_NEUTRAL_LINE = 50.0       # Nivel eje para rebotes en tendencia
RSI_MOMENTUM_LONG = 55.0      # Nivel de ruptura de impulso alcista
RSI_MOMENTUM_SHORT = 45.0     # Nivel de ruptura de impulso bajista
RSI_MAX_OVERBOUGHT = 68.0     # Techo para evitar comprar velas agotadas
RSI_MIN_OVERSOLD = 32.0       # Piso para evitar vender en pánico agotado
VOL_FACTOR_BASE = 0.85        # Volumen mínimo en rebotes (85% de la MA20)
VOL_FACTOR_BREAKOUT = 1.15    # Volumen exigido en rupturas de momentum (115% de la MA20)
MAX_WICK_RATIO = 0.45         # Máximo 45% de mecha de rechazo permitida en la vela

# Variables de estado
latest_market_data = {
    "price": None,
    "ema": None,
    "rsi_closed": None,
    "rsi_live": None,
    "vol_ratio": None
}
active_trade = None
candles_scanned_count = 0
last_bot_message_id = None
threads_started = False
lock_file_handle = None

# Sesión HTTP persistente para menor latencia
http_session = requests.Session()
http_session.headers.update({"User-Agent": "Mozilla/5.0 (QuantBot/2.0)"})

app = Flask(__name__)

# ==========================================
# 2. MOTOR DE COMUNICACIÓN TELEGRAM
# ==========================================
def delete_telegram_message(chat_id, message_id):
    if not TELEGRAM_BOT_TOKEN or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    try:
        http_session.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=3)
    except Exception:
        pass

def send_telegram_alert(message, target_chat_id=None, clean_previous=False):
    global last_bot_message_id
    if not TELEGRAM_BOT_TOKEN:
        return

    dest_id = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID

    if clean_previous and last_bot_message_id:
        delete_telegram_message(dest_id, last_bot_message_id)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": dest_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = http_session.post(url, json=payload, timeout=10)
        if r.status_code == 200 and clean_previous:
            res_json = r.json()
            last_bot_message_id = res_json.get("result", {}).get("message_id")
    except Exception as e:
        print(f"[Telegram] Error enviando alerta: {e}")

def send_permanent_welcome(message):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        http_session.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[Telegram] Error bienvenida: {e}")

# ==========================================
# 3. FEED DE MERCADO E INDICADORES EXACTOS
# ==========================================
def get_live_sol_price():
    try:
        url = "https://api.exchange.coinbase.com/products/SOL-USD/ticker"
        res = http_session.get(url, timeout=4).json()
        if "price" in res:
            return float(res["price"])
    except Exception:
        pass
    try:
        url = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
        res = http_session.get(url, timeout=4).json()
        return float(res["data"]["amount"])
    except Exception:
        return None

def fetch_candles_5m():
    try:
        url = f"https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity={GRANULARITY}"
        res = http_session.get(url, timeout=8).json()
        if not isinstance(res, list) or len(res) < 210:
            return None
        df = pd.DataFrame(res, columns=["time", "low", "high", "open", "close", "volume"])
        for col in ["low", "high", "open", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["time"] = df["time"].astype(int)
        df = df.sort_values(by="time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[Mercado] Error descargando velas: {e}")
        return None

def calculate_technical_indicators(df):
    df = df.copy()
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
# 4. MONITOREO DE POSICIÓN ABIERTA (TP/SL/BE)
# ==========================================
def manage_open_position(current_price):
    global active_trade
    if not active_trade or not current_price:
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
                f"Precio actual: <code>${current_price:.2f}</code> (+{BE_TRIGGER_PCT*100:.1f}%)\n"
                f"Mueve tu Stop Loss en Phantom a entrada: <code>${active_trade['sl']:.2f}</code>"
            )
        elif current_price >= tp:
            send_telegram_alert(
                f"🎯 <b>TAKE PROFIT ALCANZADO (+3.0%)</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (LONG)\n"
                f"Entrada: <code>${entry:.2f}</code> | Salida: <code>${current_price:.2f}</code>"
            )
            active_trade = None
        elif current_price <= sl:
            tag = "BREAK-EVEN" if be_active else "STOP LOSS"
            send_telegram_alert(
                f"🛑 <b>{tag} ALCANZADO</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (LONG)\n"
                f"Entrada: <code>${entry:.2f}</code> | Salida: <code>${current_price:.2f}</code>"
            )
            active_trade = None

    elif direction == "SHORT":
        if not be_active and current_price <= entry * (1 - BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = round(entry, 2)
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN BLINDADO · SHORT</b>\n\n"
                f"Precio actual: <code>${current_price:.2f}</code> (+{BE_TRIGGER_PCT*100:.1f}%)\n"
                f"Mueve tu Stop Loss en Phantom a entrada: <code>${active_trade['sl']:.2f}</code>"
            )
        elif current_price <= tp:
            send_telegram_alert(
                f"🎯 <b>TAKE PROFIT ALCANZADO (+3.0%)</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (SHORT)\n"
                f"Entrada: <code>${entry:.2f}</code> | Salida: <code>${current_price:.2f}</code>"
            )
            active_trade = None
        elif current_price >= sl:
            tag = "BREAK-EVEN" if be_active else "STOP LOSS"
            send_telegram_alert(
                f"🛑 <b>{tag} ALCANZADO</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (SHORT)\n"
                f"Entrada: <code>${entry:.2f}</code> | Salida: <code>${current_price:.2f}</code>"
            )
            active_trade = None

# ==========================================
# 5. LISTENER TELEGRAM (COMANDOS INTERACTIVOS)
# ==========================================
def telegram_listener():
    global active_trade
    offset = None
    print("[Telegram] Listener activo.")

    while True:
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(5)
            continue

        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 10}
            if offset is not None:
                params["offset"] = offset

            resp = http_session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    msg_id = msg.get("message_id")
                    text = msg.get("text", "").strip().lower()

                    valid_cmds = ["price", "status", "restart", "close", "free"]
                    if msg_id and (text.startswith("/") or text in valid_cmds):
                        delete_telegram_message(chat_id, msg_id)

                    if text.startswith("/price") or text == "price":
                        p = get_live_sol_price() or latest_market_data.get("price")
                        ema = latest_market_data.get("ema")
                        rsi_c = latest_market_data.get("rsi_closed")
                        rsi_l = latest_market_data.get("rsi_live")
                        vol_r = latest_market_data.get("vol_ratio")

                        if p and ema and rsi_c is not None:
                            trend = "🟢 Tendencia Alcista (LONG Bias)" if p > ema else "🔴 Tendencia Bajista (SHORT Bias)"
                            rsi_live_str = f"{rsi_l:.1f}" if rsi_l is not None else f"{rsi_c:.1f}"
                            vol_str = f"{vol_r:.2f}x MA20" if vol_r is not None else "N/A"
                            resp_text = (
                                f"📊 <b>TELEMETRÍA SOL-PERP (5M)</b>\n\n"
                                f"💵 <b>Precio Spot:</b> <code>${p:.2f}</code>\n"
                                f"📈 <b>EMA 200:</b> <code>${ema:.2f}</code>\n"
                                f"📉 <b>RSI (14) Vivo / Cierre:</b> <code>{rsi_live_str}</code> / <code>{rsi_c:.1f}</code>\n"
                                f"📊 <b>Volumen Últ. Vela:</b> <code>{vol_str}</code>\n"
                                f"⚡ <b>Sesgo:</b> <code>{trend}</code>"
                            )
                        else:
                            resp_text = "⏳ Sincronizando telemetría de mercado..."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/status") or text == "status":
                        if active_trade:
                            pos = (
                                f"ACTIVA ({active_trade['type']} @ ${active_trade['entry']:.2f} | "
                                f"TP: ${active_trade['tp']:.2f} | SL: ${active_trade['sl']:.2f})"
                            )
                        else:
                            pos = "LIBRE (Escaneando mercado)"
                        resp_text = (
                            f"🟢 <b>ESTADO DEL SISTEMA</b>\n\n"
                            f"• <b>Nodo Render:</b> En línea ⚡\n"
                            f"• <b>Posición:</b> <code>{pos}</code>\n"
                            f"• <b>Velas 5M cerradas evaluadas:</b> <code>{candles_scanned_count}</code>"
                        )
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/close") or text.startswith("/free") or text in ["close", "free"]:
                        active_trade = None
                        resp_text = "✅ <b>POSICIÓN LIBERADA</b>\nEl bot volvió a modo escaneo para detectar la próxima entrada."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/restart") or text == "restart":
                        active_trade = None
                        resp_text = "🔄 <b>SISTEMA REINICIADO</b>\nEstado de trade reseteado. Escaneo activo al 100%."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

        except Exception:
            time.sleep(5)
        time.sleep(1)

# ==========================================
# 6. MOTOR CUANTITATIVO DE MERCADO (5M)
# ==========================================
def evaluate_signal(closed_candle, prev_candle, live_price):
    """Evalúa condiciones de entrada sobre la vela recién cerrada con filtro anti-mecha."""
    open_p = float(closed_candle["open"])
    high_p = float(closed_candle["high"])
    low_p = float(closed_candle["low"])
    close_p = float(closed_candle["close"])
    ema_val = float(closed_candle["ema_200"])
    rsi_val = float(closed_candle["rsi_14"])
    prev_rsi = float(prev_candle["rsi_14"])
    vol_val = float(closed_candle["volume"])
    vol_avg = float(closed_candle["vol_ma20"]) if pd.notna(closed_candle["vol_ma20"]) else vol_val

    candle_range = max(high_p - low_p, 0.0001)
    upper_wick_ratio = (high_p - close_p) / candle_range
    lower_wick_ratio = (close_p - low_p) / candle_range

    # ---------------- EVALUACIÓN LONG ----------------
    if close_p > ema_val and close_p > open_p:
        # Filtro anti-mechazo superior (evita comprar rechazos como el de $85.30)
        if upper_wick_ratio <= MAX_WICK_RATIO and rsi_val < RSI_MAX_OVERBOUGHT:
            # Condición A: Rebote limpio cruzando RSI 50 con volumen base
            cond_pullback = (prev_rsi < RSI_NEUTRAL_LINE and rsi_val >= RSI_NEUTRAL_LINE) and (vol_val >= vol_avg * VOL_FACTOR_BASE)
            # Condición B: Continuación de momentum cruzando RSI 55 con volumen de expansión
            cond_breakout = (prev_rsi < RSI_MOMENTUM_LONG and rsi_val >= RSI_MOMENTUM_LONG) and (vol_val >= vol_avg * VOL_FACTOR_BREAKOUT)

            if cond_pullback or cond_breakout:
                trigger_reason = "Rebote EMA200 + Cruce RSI 50" if cond_pullback else "Ruptura Momentum RSI 55 + Volumen"
                return "LONG", trigger_reason, rsi_val, (vol_val / vol_avg if vol_avg > 0 else 1.0)

    # ---------------- EVALUACIÓN SHORT ----------------
    elif close_p < ema_val and close_p < open_p:
        # Filtro anti-mechazo inferior (evita vender en absorción compradora)
        if lower_wick_ratio <= MAX_WICK_RATIO and rsi_val > RSI_MIN_OVERSOLD:
            # Condición A: Rechazo bajista cruzando RSI 50 a la baja
            cond_pullback = (prev_rsi > RSI_NEUTRAL_LINE and rsi_val <= RSI_NEUTRAL_LINE) and (vol_val >= vol_avg * VOL_FACTOR_BASE)
            # Condición B: Ruptura bajista cruzando RSI 45 con volumen de expansión
            cond_breakout = (prev_rsi > RSI_MOMENTUM_SHORT and rsi_val <= RSI_MOMENTUM_SHORT) and (vol_val >= vol_avg * VOL_FACTOR_BREAKOUT)

            if cond_pullback or cond_breakout:
                trigger_reason = "Rechazo EMA200 + Pérdida RSI 50" if cond_pullback else "Quiebre Bajista RSI 45 + Volumen"
                return "SHORT", trigger_reason, rsi_val, (vol_val / vol_avg if vol_avg > 0 else 1.0)

    return None, None, rsi_val, (vol_val / vol_avg if vol_avg > 0 else 1.0)

def market_engine():
    global candles_scanned_count, latest_market_data, active_trade
    last_evaluated_time = None
    time.sleep(2)

    tradingview_url = "https://www.tradingview.com/chart/?symbol=COINBASE%3ASOLUSDC"
    welcome_card = (
        f"⚡ <b>QUANTITATIVE BOT v2.0 ACTIVE</b> ⚡\n\n"
        f"Par: <code>SOL-PERP (5M)</code> | Sincronización UNIX Exacta\n"
        f"Modos activos: <code>Rebote RSI 50 + Ruptura RSI 55 + Filtro Anti-Mecha</code>\n"
        f"Comandos: <code>/price</code> | <code>/status</code> | <code>/close</code> | <code>/restart</code>\n\n"
        f"🔗 <a href='{tradingview_url}'>Abrir TradingView (5M)</a>"
    )
    send_permanent_welcome(welcome_card)

    while True:
        try:
            now_ts = int(time.time())
            current_bucket = now_ts - (now_ts % GRANULARITY)

            live_price = get_live_sol_price()
            if live_price:
                latest_market_data["price"] = live_price
                manage_open_position(live_price)

            df = fetch_candles_5m()
            if df is not None and len(df) >= 205:
                # 1. Calcular RSI en vivo incluyendo la vela en formación
                df_live = df.copy()
                if live_price and df_live.iloc[-1]["time"] == current_bucket:
                    df_live.loc[df_live.index[-1], "close"] = live_price
                df_live = calculate_technical_indicators(df_live)
                latest_market_data["rsi_live"] = float(df_live.iloc[-1]["rsi_14"])

                # 2. Filtrar estrictamente las velas 100% cerradas (time < current_bucket)
                closed_df = df[df["time"] < current_bucket].copy()
                if len(closed_df) >= 202:
                    closed_df = calculate_technical_indicators(closed_df)
                    closed_candle = closed_df.iloc[-1]
                    prev_candle = closed_df.iloc[-2]
                    candle_time = int(closed_candle["time"])

                    close_p = float(closed_candle["close"])
                    ema_val = float(closed_candle["ema_200"])
                    rsi_val = float(closed_candle["rsi_14"])
                    vol_val = float(closed_candle["volume"])
                    vol_avg = float(closed_candle["vol_ma20"]) if pd.notna(closed_candle["vol_ma20"]) else vol_val

                    latest_market_data["ema"] = ema_val
                    latest_market_data["rsi_closed"] = rsi_val
                    latest_market_data["vol_ratio"] = (vol_val / vol_avg) if vol_avg > 0 else 1.0

                    # Evaluar únicamente una vez por cada vela de 5M recién cerrada
                    if candle_time != last_evaluated_time:
                        # En el primer arranque del bot, registra la vela actual sin disparar señales viejas
                        if last_evaluated_time is None:
                            last_evaluated_time = candle_time
                            print(f"[Engine] Sincronizado en vela base {candle_time}. Esperando próximo cierre...")
                        else:
                            last_evaluated_time = candle_time
                            candles_scanned_count += 1

                            if not active_trade:
                                sig_type, reason, sig_rsi, sig_vol_ratio = evaluate_signal(
                                    closed_candle, prev_candle, live_price
                                )

                                if sig_type == "LONG":
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
                                        f"🚨 <b>SEÑAL LONG · CONFIRMADA AL CIERRE (5M)</b>\n\n"
                                        f"🪙 <b>Activo:</b> <code>{SYMBOL}</code>\n"
                                        f"⚡ <b>Gatillo:</b> <code>{reason}</code>\n"
                                        f"📈 <b>Entrada (Phantom):</b> <code>${entry:.2f}</code>\n"
                                        f"📊 <b>RSI Cierre:</b> <code>{sig_rsi:.1f}</code> | <b>Vol:</b> <code>{sig_vol_ratio:.2f}x</code>\n"
                                        f"💰 <b>Margen:</b> <code>${MARGIN_USDC} USDC ({LEVERAGE}x | Nocional: ${notional:.2f})</code>\n"
                                        f"🎯 <b>Take Profit (+3.0%):</b> <code>${tp:.2f}</code>\n"
                                        f"🛑 <b>Stop Loss (-1.5%):</b> <code>${sl:.2f}</code>\n"
                                        f"🛡️ <b>Break-Even:</b> Alerta automática al +{BE_TRIGGER_PCT*100:.1f}%"
                                    )
                                    send_telegram_alert(card)

                                elif sig_type == "SHORT":
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
                                        f"🚨 <b>SEÑAL SHORT · CONFIRMADA AL CIERRE (5M)</b>\n\n"
                                        f"🪙 <b>Activo:</b> <code>{SYMBOL}</code>\n"
                                        f"⚡ <b>Gatillo:</b> <code>{reason}</code>\n"
                                        f"📉 <b>Entrada (Phantom):</b> <code>${entry:.2f}</code>\n"
                                        f"📊 <b>RSI Cierre:</b> <code>{sig_rsi:.1f}</code> | <b>Vol:</b> <code>{sig_vol_ratio:.2f}x</code>\n"
                                        f"💰 <b>Margen:</b> <code>${MARGIN_USDC} USDC ({LEVERAGE}x | Nocional: ${notional:.2f})</code>\n"
                                        f"🎯 <b>Take Profit (+3.0%):</b> <code>${tp:.2f}</code>\n"
                                        f"🛑 <b>Stop Loss (-1.5%):</b> <code>${sl:.2f}</code>\n"
                                        f"🛡️ <b>Break-Even:</b> Alerta automática al +{BE_TRIGGER_PCT*100:.1f}%"
                                    )
                                    send_telegram_alert(card)

        except Exception as e:
            print(f"[Engine] Error en ciclo: {e}")

        time.sleep(5)

# ==========================================
# 7. ARRANQUE CON LOCK DE PROCESO EN RENDER
# ==========================================
def start_background_workers():
    global threads_started, lock_file_handle
    if threads_started:
        return

    # Evita que múltiples workers de Gunicorn dupliquen el bot
    if HAS_FCNTL:
        try:
            lock_file_handle = open("/tmp/sol_quant_bot.lock", "w")
            fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            print("[System] Otro worker ya tiene el lock activo. Omitiendo hilos duplicados.")
            return

    threads_started = True
    t_market = threading.Thread(target=market_engine, daemon=True)
    t_market.start()
    t_telegram = threading.Thread(target=telegram_listener, daemon=True)
    t_telegram.start()

start_background_workers()

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "SOL-PERP Quant Signal v2.0",
        "candles_scanned": candles_scanned_count,
        "active_trade": active_trade is not None
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
