import os
import time
import threading
import requests
import pandas as pd
from flask import Flask

# ==========================================
# 1. PARÁMETROS Y CONFIGURACIÓN
# ==========================================
TELEGRAM_BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN") 
    or os.environ.get("TELEGRAM_TOKEN") 
    or os.environ.get("BOT_TOKEN")
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "8459722038"

SYMBOL = "SOL/USDT"
TIMEFRAME = "15m"

# Gestión de riesgo R:R 1:2
SL_PCT = 0.015          # 1.5%
TP_PCT = 0.030          # 3.0%
BE_TRIGGER_PCT = 0.015  # 1.5%

# URLs de acceso rápido
PHANTOM_URL = "https://phantom.app/ul/browse/https%3A%2F%2Fjup.ag%2Fperps"
TRADINGVIEW_URL = "https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT"

# Variables de estado en memoria
latest_market_data = {"price": None, "ema": None, "rsi": None}
active_trade = None
candles_scanned_count = 0
last_bot_message_id = None  # Almacena el ID para auto-limpieza de chat

app = Flask(__name__)

# ==========================================
# 2. INTERFAZ Y COMUNICACIÓN TELEGRAM
# ==========================================
def get_action_buttons(include_reset=False):
    keyboard = [
        [
            {"text": "⚡ Abrir Phantom", "url": PHANTOM_URL},
            {"text": "📈 TradingView", "url": TRADINGVIEW_URL}
        ]
    ]
    return {"inline_keyboard": keyboard}

def delete_telegram_message(chat_id, message_id):
    """Borra un mensaje específico de Telegram."""
    if not TELEGRAM_BOT_TOKEN or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
    except Exception:
        pass

def send_telegram_alert(message, target_chat_id=None, reply_markup=None, clean_previous=False):
    """Envía mensaje y opcionalmente borra el mensaje previo del bot para mantener limpio el chat."""
    global last_bot_message_id
    token = TELEGRAM_BOT_TOKEN
    if not token:
        print("[Telegram] ERROR: Token no definido.")
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
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            res_data = r.json()
            if clean_previous:
                last_bot_message_id = res_data.get("result", {}).get("message_id")
        else:
            print(f"[Telegram] Error al enviar mensaje: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[Telegram] Excepción de conexión: {e}")

# ==========================================
# 3. FEED DE MERCADO Y CÁLCULO TÉCNICO
# ==========================================
def get_live_sol_price():
    headers = {"User-Agent": "Mozilla/5.0"}
    # Intento 1: Jupiter Price API (nativo de Solana)
    try:
        url_jup = "https://price.jup.ag/v6/price?ids=SOL"
        res = requests.get(url_jup, headers=headers, timeout=4).json()
        return float(res["data"]["SOL"]["price"])
    except Exception:
        pass

    # Intento 2: Coinbase API pública
    try:
        url = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
        res = requests.get(url, headers=headers, timeout=5).json()
        return float(res["data"]["amount"])
    except Exception:
        pass

    # Intento 3: CoinGecko API
    try:
        url_cg = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        res_cg = requests.get(url_cg, headers=headers, timeout=5).json()
        return float(res_cg["solana"]["usd"])
    except Exception:
        return None

def fetch_candles_15m():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = "https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=900"
        res = requests.get(url, headers=headers, timeout=10).json()
        if not isinstance(res, list) or len(res) < 200:
            return None

        # Formato Coinbase: [time, low, high, open, close, volume]
        df = pd.DataFrame(res, columns=["time", "low", "high", "open", "close", "volume"])
        df = df.sort_values(by="time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[Mercado] Error al descargar velas: {e}")
        return None

def calculate_technical_indicators(df):
    close = df["close"]
    
    # EMA 200
    df["ema_200"] = close.ewm(span=200, adjust=False).mean()

    # RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # Media Móvil de Volumen 20
    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    return df

# ==========================================
# 4. GESTIÓN DE ENTRADAS Y BREAK-EVEN
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
        # Disparo de Break-Even
        if not be_active and current_price >= entry * (1 + BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = entry
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN ACTIVADO · LONG</b>\n\n"
                f"Precio actual: <code>${current_price:.2f}</code> (+1.5%)\n"
                f"Mueve tu Stop-Loss en Phantom a precio de entrada: <code>${entry:.2f}</code>\n"
                f"Riesgo de la operación: <b>$0.00</b>",
                reply_markup=get_action_buttons()
            )
        # Salida por TP
        elif current_price >= tp:
            send_telegram_alert(
                f"🎯 <b>TAKE PROFIT ALCANZADO (+3.0%)</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (LONG)\n"
                f"Salida: <code>${current_price:.2f}</code>\n"
                f"Operación finalizada en ganancia.",
                reply_markup=get_action_buttons()
            )
            active_trade = None
        # Salida por SL
        elif current_price <= sl:
            send_telegram_alert(
                f"🛑 <b>STOP LOSS EJECUTADO</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (LONG)\n"
                f"Salida: <code>${current_price:.2f}</code>\n"
                f"Posición cerrada.",
                reply_markup=get_action_buttons()
            )
            active_trade = None

    elif direction == "SHORT":
        # Disparo de Break-Even
        if not be_active and current_price <= entry * (1 - BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = entry
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN ACTIVADO · SHORT</b>\n\n"
                f"Precio actual: <code>${current_price:.2f}</code> (+1.5%)\n"
                f"Mueve tu Stop-Loss en Phantom a precio de entrada: <code>${entry:.2f}</code>\n"
                f"Riesgo de la operación: <b>$0.00</b>",
                reply_markup=get_action_buttons()
            )
        # Salida por TP
        elif current_price <= tp:
            send_telegram_alert(
                f"🎯 <b>TAKE PROFIT ALCANZADO (+3.0%)</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (SHORT)\n"
                f"Salida: <code>${current_price:.2f}</code>\n"
                f"Operación finalizada en ganancia.",
                reply_markup=get_action_buttons()
            )
            active_trade = None
        # Salida por SL
        elif current_price >= sl:
            send_telegram_alert(
                f"🛑 <b>STOP LOSS EJECUTADO</b>\n\n"
                f"Par: <b>{SYMBOL}</b> (SHORT)\n"
                f"Salida: <code>${current_price:.2f}</code>\n"
                f"Posición cerrada.",
                reply_markup=get_action_buttons()
            )
            active_trade = None

# ==========================================
# 5. HILO DEL LISTENER DE TELEGRAM (COMANDOS)
# ==========================================
def telegram_listener():
    global active_trade
    offset = None
    print("[Telegram] Listener de comandos iniciado.")

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
                    raw_text = msg.get("text", "").strip()
                    text = raw_text.lower()

                    # Eliminar el comando enviado por el usuario
                    if msg_id and (text.startswith("/") or text in ["precio", "status", "reset"]):
                        delete_telegram_message(chat_id, msg_id)

                    if text.startswith("/precio") or text == "precio":
                        p = latest_market_data.get("price") or get_live_sol_price()
                        ema = latest_market_data.get("ema")
                        rsi = latest_market_data.get("rsi")
                        if p:
                            ema_str = f"${ema:.2f}" if ema else "Sincronizando..."
                            rsi_str = f"{rsi:.1f}" if rsi else "Sincronizando..."
                            trend = "\nSesgo: 🟢 LONG Bias" if (ema and p > ema) else "\nSesgo: 🔴 SHORT Bias"
                            resp_text = (
                                "📊 <b>SOL/USDT · EN VIVO</b>\n\n"
                                f"Precio: <code>${p:.2f}</code>\n"
                                f"EMA 200: <code>{ema_str}</code>\n"
                                f"RSI (14): <code>{rsi_str}</code>"
                                f"{trend}"
                            )
                        else:
                            resp_text = "⏳ Sincronizando precio..."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, reply_markup=get_action_buttons(), clean_previous=True)

                    elif text.startswith("/status") or text == "status":
                        pos = f"{active_trade['type']} @ ${active_trade['entry']:.2f}" if active_trade else "Ninguna (Escaneando velas libremente)"
                        resp_text = (
                            "⚡ <b>ESTADO DEL BOT</b>\n\n"
                            "Servidor: Render (Activo)\n"
                            f"Velas escaneadas: {candles_scanned_count}\n"
                            f"Posicion: <code>{pos}</code>"
                        )
                        send_telegram_alert(resp_text, target_chat_id=chat_id, reply_markup=get_action_buttons(), clean_previous=True)

                    elif text.startswith("/reset") or text == "reset":
                        active_trade = None
                        resp_text = (
                            "🔄 <b>POSICIÓN RESETEADA</b>\n\n"
                            "Velas: <code>15m</code> (Modo escaneo activo)\n"
                            "Posición: <code>Ninguna (Buscando siguiente trade)</code>\n\n"
                            "Filtro: Memoria liberada con éxito"
                        )
                        send_telegram_alert(resp_text, target_chat_id=chat_id, reply_markup=get_action_buttons(), clean_previous=True)

                    elif text.startswith("/start") or text == "start":
                        resp_text = (
                            "🚀 <b>ALPHA BOT CONECTADO</b>\n\n"
                            "Par: SOL/USDT\n"
                            "Velas: 15m\n"
                            "Filtros: EMA 200 + RSI + Vol MA20\n\n"
                            "Escribe /precio o /status cuando quieras."
                        )
                        send_telegram_alert(resp_text, target_chat_id=chat_id, reply_markup=get_action_buttons(), clean_previous=True)

            elif resp.status_code == 401:
                print("[Telegram] Error 401: Token inválido. Revisa las variables en Render.")
                time.sleep(10)
        except Exception:
            time.sleep(5)

        time.sleep(1)

# ==========================================
# 6. HILO PRINCIPAL DE MERCADO
# ==========================================
def market_engine():
    global candles_scanned_count, latest_market_data, active_trade
    last_evaluated_time = None

    time.sleep(3)
    send_telegram_alert(
        "🚀 <b>ALPHA BOT CONECTADO</b>\n\n"
        "Par: SOL/USDT\n"
        "Velas: 15m\n"
        "Filtros: EMA 200 + RSI + Vol MA20\n\n"
        "Escribe /precio o /status cuando quieras.",
        reply_markup=get_action_buttons(),
        clean_previous=True
    )

    while True:
        try:
            live_price = get_live_sol_price()
            if live_price:
                latest_market_data["price"] = live_price
                manage_open_position(live_price)

            df = fetch_candles_15m()
            if df is not None and len(df) >= 200:
                df = calculate_technical_indicators(df)
                
                # La penúltima vela es la última cerrada definitivamente
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

                    # Evaluación de señales solo si no hay operación abierta
                    if not active_trade:
                        # 🟢 CONDICIÓN LONG
                        if (close_p > ema_val) and (prev_rsi < 35 and rsi_val >= 35) and (vol_val > vol_avg):
                            entry = live_price if live_price else close_p
                            tp = round(entry * (1 + TP_PCT), 2)
                            sl = round(entry * (1 - SL_PCT), 2)

                            active_trade = {
                                "type": "LONG",
                                "entry": entry,
                                "tp": tp,
                                "sl": sl,
                                "be_activated": False
                            }

                            msg = (
                                "🟢 <b>LONG · SOL/USDT</b> 🔼\n\n"
                                f"Entrada: <code>{entry:.2f}</code>\n"
                                f"TP (+3.0%): <code>{tp:.2f}</code>\n"
                                f"SL (-1.5%): <code>{sl:.2f}</code>\n\n"
                                "Ratio: 1:2\n"
                                f"Filtro: RSI {rsi_val:.1f} + Volumen confirmado"
                            )
                            send_telegram_alert(msg, reply_markup=get_action_buttons(), clean_previous=False)

                        # 🔴 CONDICIÓN SHORT
                        elif (close_p < ema_val) and (prev_rsi > 65 and rsi_val <= 65) and (vol_val > vol_avg):
                            entry = live_price if live_price else close_p
                            tp = round(entry * (1 - TP_PCT), 2)
                            sl = round(entry * (1 + SL_PCT), 2)

                            active_trade = {
                                "type": "SHORT",
                                "entry": entry,
                                "tp": tp,
                                "sl": sl,
                                "be_activated": False
                            }

                            msg = (
                                "🔴 <b>SHORT · SOL/USDT</b> 🔽\n\n"
                                f"Entrada: <code>{entry:.2f}</code>\n"
                                f"TP (+3.0%): <code>{tp:.2f}</code>\n"
                                f"SL (-1.5%): <code>{sl:.2f}</code>\n\n"
                                "Ratio: 1:2\n"
                                f"Filtro: RSI {rsi_val:.1f} + Volumen confirmado"
                            )
                            send_telegram_alert(msg, reply_markup=get_action_buttons(), clean_previous=False)

        except Exception as e:
            print(f"[Engine] Error en el ciclo de mercado: {e}")

        time.sleep(30)

# ==========================================
# 7. SERVIDOR WEB FLASK (KEEP-ALIVE)
# ==========================================
@app.route("/")
def home():
    return "Bot activo 24/7 y escaneando SOL/USDT", 200

if __name__ == "__main__":
    t_market = threading.Thread(target=market_engine, daemon=True)
    t_market.start()

    t_telegram = threading.Thread(target=telegram_listener, daemon=True)
    t_telegram.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
