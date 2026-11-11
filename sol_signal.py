import os
import time
import threading
import requests
import pandas as pd
import numpy as np
from flask import Flask

# ==========================================
# CONFIGURACIÓN DEL SERVIDOR FLASK (RENDER)
# ==========================================
app = Flask(__name__)

@app.route("/")
def health_check():
    return "🤖 Bot Cuantitativo Multiactivo (SOL-ETH) en línea.", 200

# ==========================================
# CONFIGURACIÓN GENERAL Y CREDENCIALES
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSET_CONFIG = {
    "SOL-USD": {
        "display": "SOL-PERP",
        "tp_pct": 0.030,      # Take Profit: +3.0%
        "sl_pct": 0.015,      # Stop Loss: -1.5%
        "be_pct": 0.012,      # Break-Even: +1.2%
        "vol_factor": 1.15,   
        "tv_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3ASOLUSD"
    },
    "ETH-USD": {
        "display": "ETH-PERP",
        "tp_pct": 0.015,      # Take Profit: +1.5%
        "sl_pct": 0.010,      # Stop Loss: -1.0%
        "be_pct": 0.008,      # Break-Even: +0.8%
        "vol_factor": 1.10,   
        "tv_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3AETHUSD"
    }
}

MARGIN_USD = 3.0
LEVERAGE = 5
MAX_OVERBOUGHT_RSI = 68.0
MAX_WICK_RATIO = 0.45       

active_trades = {pair: None for pair in ASSET_CONFIG.keys()}
last_processed_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}
last_alerted_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}

eval_counter = 0
telegram_update_offset = 0

# ==========================================
# COMUNICACIÓN CON TELEGRAM
# ==========================================
def send_telegram(text: str, reply_markup: dict = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[LOG TELEGRAM]:\n{text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"Error al enviar mensaje a Telegram: {e}")

# ==========================================
# CONEXIÓN API COINBASE
# ==========================================
def fetch_candles_5m(pair: str):
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            df = pd.DataFrame(data, columns=["timestamp", "low", "high", "open", "close", "volume"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
    except Exception as e:
        print(f"Error descargando velas de {pair}: {e}")
    return None

def fetch_live_price(pair: str) -> float:
    url = f"https://api.exchange.coinbase.com/products/{pair}/ticker"
    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception:
        pass
    return 0.0

# ==========================================
# CÁLCULOS CUANTITATIVOS
# ==========================================
def calculate_indicators(df: pd.DataFrame):
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["open"] = df["open"].astype(float)

    # EMA 200
    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()

    # RSI (14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # Media Móvil de Volumen (MA20)
    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    return df

# ==========================================
# EVALUACIÓN DE SEÑAL
# ==========================================
def evaluate_market_signal(pair: str, df: pd.DataFrame):
    closed = df.iloc[-2]
    prev = df.iloc[-3]

    cfg = ASSET_CONFIG[pair]
    c_close = closed["close"]
    c_ema = closed["ema_200"]
    c_rsi = closed["rsi"]
    p_rsi = prev["rsi"]
    c_vol = closed["volume"]
    c_vol_ma = closed["vol_ma20"]
    vol_ratio = c_vol / c_vol_ma if c_vol_ma > 0 else 0

    candle_range = closed["high"] - closed["low"]
    upper_wick = closed["high"] - max(closed["open"], c_close)
    wick_ratio = upper_wick / candle_range if candle_range > 0 else 0

    # Filtro estructural: Precio sobre EMA 200 (Tendencia Alcista)
    if c_close <= c_ema:
        return None

    # Escudo Anti-Mechazos
    if wick_ratio > MAX_WICK_RATIO:
        return None

    # Filtro de Volumen Institucional
    if vol_ratio < cfg["vol_factor"]:
        return None

    # Cruce de RSI
    crossed_rebound = (p_rsi < 50.0) and (c_rsi >= 50.0)
    crossed_momentum = (p_rsi < 55.0) and (c_rsi >= 55.0)

    if (crossed_rebound or crossed_momentum) and (c_rsi <= MAX_OVERBOUGHT_RSI):
        tp_price = c_close * (1 + cfg["tp_pct"])
        sl_price = c_close * (1 - cfg["sl_pct"])
        be_price = c_close * (1 + cfg["be_pct"])

        return {
            "pair": pair,
            "display": cfg["display"],
            "trend": "🟢 Alcista (Sobre EMA 200)",
            "type": "LONG",
            "entry_price": c_close,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "be_price": be_price,
            "tp_pct": cfg["tp_pct"] * 100,
            "sl_pct": cfg["sl_pct"] * 100,
            "tv_url": cfg["tv_url"],
            "be_moved": False,
            "timestamp": int(closed["timestamp"])
        }
    return None

# ==========================================
# MONITOREO DE POSICIONES ACTIVAS
# ==========================================
def monitor_active_trades():
    for pair, trade in list(active_trades.items()):
        if trade is None:
            continue
        live_price = fetch_live_price(pair)
        if live_price <= 0:
            continue

        # Break-Even
        if not trade["be_moved"] and live_price >= trade["be_price"]:
            trade["be_moved"] = True
            send_telegram(
                f"🛡️ <b>BREAK-EVEN | {trade['display']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>Precio Actual:</b> <code>${live_price:,.2f}</code>\n"
                f"📥 <b>Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"📊 <b>Tendencia:</b> {trade['trend']}\n\n"
                f"⚡ <i>Acción: Mueve tu Stop Loss al precio de entrada en Phantom.</i>"
            )

        # Take Profit
        if live_price >= trade["tp_price"]:
            send_telegram(
                f"✅ <b>TAKE PROFIT ALCANZADO | {trade['display']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Cierre:</b> <code>${live_price:,.2f}</code> <i>(+{trade['tp_pct']}%)</i>\n"
                f"📥 <b>Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n\n"
                f"⚡ <i>Posición cerrada con éxito. El par queda libre.</i>"
            )
            active_trades[pair] = None

        # Stop Loss
        elif live_price <= trade["sl_price"]:
            send_telegram(
                f"❌ <b>STOP LOSS EJECUTADO | {trade['display']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📉 <b>Cierre:</b> <code>${live_price:,.2f}</code> <i>(-{trade['sl_pct']}%)</i>\n"
                f"📥 <b>Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n\n"
                f"⚡ <i>Posición cerrada por gestión de riesgo. El par queda libre.</i>"
            )
            active_trades[pair] = None

# ==========================================
# COMANDOS DE TELEGRAM
# ==========================================
def handle_telegram_commands():
    global telegram_update_offset, active_trades
    if not TELEGRAM_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": telegram_update_offset, "timeout": 5}

    try:
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code != 200:
            return

        updates = resp.json().get("result", [])
        for update in updates:
            telegram_update_offset = update["update_id"] + 1
            message = update.get("message", {})
            text = message.get("text", "").strip().lower()

            if text in ["/price", "/telemetria"]:
                report = "📊 <b>TELEMETRÍA (5M)</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
                for pair, cfg in ASSET_CONFIG.items():
                    df = fetch_candles_5m(pair)
                    if df is not None and len(df) >= 200:
                        df = calculate_indicators(df)
                        last = df.iloc[-1]
                        vol_ratio = last["volume"] / last["vol_ma20"] if last["vol_ma20"] > 0 else 0
                        
                        trend_label = "🟢 Alcista" if last["close"] > last["ema_200"] else "🔴 Bajista"

                        report += (
                            f"🪙 <b>{cfg['display']}</b>\n"
                            f"🧭 Tendencia: <b>{trend_label}</b>\n"
                            f"🏷️ Spot: <code>${last['close']:,.2f}</code>\n"
                            f"📈 EMA 200: <code>${last['ema_200']:,.2f}</code>\n"
                            f"📉 RSI: <code>{last['rsi']:.1f}</code>\n"
                            f"📊 Vol MA20: <code>{vol_ratio:.2f}x</code>\n\n"
                        )
                send_telegram(report)

            elif text in ["/status", "/estado"]:
                sol_st = "🟢 LIBRE" if active_trades["SOL-USD"] is None else "🟡 EN POSICIÓN"
                eth_st = "🟢 LIBRE" if active_trades["ETH-USD"] is None else "🟡 EN POSICIÓN"
                msg = (
                    f"🟢 <b>ESTADO DEL SISTEMA</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>SOL-PERP:</b> {sol_st}\n"
                    f"• <b>ETH-PERP:</b> {eth_st}\n"
                    f"• <b>Velas 5M evaluadas:</b> {eval_counter}\n"
                    f"• <b>Gestión:</b> ${MARGIN_USD} USDC a {LEVERAGE}x\n"
                )
                send_telegram(msg)

            elif text.startswith("/close") or text.startswith("/free"):
                parts = text.split()
                if len(parts) > 1 and "eth" in parts[1]:
                    active_trades["ETH-USD"] = None
                    send_telegram("🔓 <b>ETH-PERP liberado manualmente.</b> Escaneando mercado.")
                elif len(parts) > 1 and "sol" in parts[1]:
                    active_trades["SOL-USD"] = None
                    send_telegram("🔓 <b>SOL-PERP liberado manualmente.</b> Escaneando mercado.")
                else:
                    active_trades["SOL-USD"] = None
                    active_trades["ETH-USD"] = None
                    send_telegram("🔓 <b>Todos los activos liberados.</b> Modo escaneo activo.")

            elif text in ["/restart"]:
                send_telegram("⚡ <b>Reiniciando variables de escaneo...</b>")
                active_trades = {p: None for p in ASSET_CONFIG.keys()}

    except Exception as e:
        print(f"Error en comandos de Telegram: {e}")

# ==========================================
# BUCLE PRINCIPAL DEL ESCÁNER
# ==========================================
def main_scanner_loop():
    global eval_counter
    print("Iniciando escáner dual en segundo plano...")

    send_telegram(
        "⚡ <b>BOT CUANTITATIVO MULTIACTIVO ACTIVO (v2.0)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Pares vigilados:</b> SOL-PERP (TP 3.0%) | ETH-PERP (TP 1.5%)\n"
        "• <b>Margen configurado:</b> $3 USDC a 5x ($15 posición)\n"
        "• <b>Comandos:</b> /price | /status | /close [sol|eth] | /restart"
    )

    while True:
        try:
            monitor_active_trades()

            for pair in ASSET_CONFIG.keys():
                df = fetch_candles_5m(pair)
                if df is None or len(df) < 205:
                    continue

                closed_candle_ts = int(df.iloc[-2]["timestamp"])

                if closed_candle_ts > last_processed_timestamps[pair]:
                    df = calculate_indicators(df)
                    signal = evaluate_market_signal(pair, df)

                    if signal:
                        # Control Anti-Spam: solo una alerta por cierre de vela
                        if signal["timestamp"] != last_alerted_timestamps[pair]:
                            last_alerted_timestamps[pair] = signal["timestamp"]

                            if active_trades[pair] is None:
                                active_trades[pair] = signal

                            keyboard = {
                                "inline_keyboard": [
                                    [{"text": f"📈 Abrir {signal['display']} en TradingView", "url": signal["tv_url"]}]
                                ]
                            }

                            card_text = (
                                f"🟢 <b>SEÑAL LONG | {signal['display']}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🧭 <b>Tendencia:</b> {signal['trend']}\n"
                                f"📥 <b>Entrada:</b> <code>${signal['entry_price']:,.2f}</code>\n\n"
                                f"🎯 <b>Take Profit:</b> <code>${signal['tp_price']:,.2f}</code> <i>(+{signal['tp_pct']}%)</i>\n"
                                f"🛡️ <b>Break-Even:</b> <code>${signal['be_price']:,.2f}</code>\n"
                                f"🛑 <b>Stop Loss:</b> <code>${signal['sl_price']:,.2f}</code> <i>(-{signal['sl_pct']}%)</i>\n\n"
                                f"📊 <b>Gestión:</b> ${MARGIN_USD} USDC | {LEVERAGE}x | Phantom"
                            )

                            send_telegram(card_text, reply_markup=keyboard)

                    last_processed_timestamps[pair] = closed_candle_ts
                    eval_counter += 1

            handle_telegram_commands()

        except Exception as e:
            print(f"Error en el bucle principal: {e}")

        time.sleep(3)

# ==========================================
# PUNTO DE ARRANQUE
# ==========================================
if __name__ == "__main__":
    bot_thread = threading.Thread(target=main_scanner_loop, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
