import os
import time
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ==========================================
# CONFIGURACIÓN GENERAL Y CREDENCIALES
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parámetros modulares de gestión de riesgo por activo
ASSET_CONFIG = {
    "SOL-USD": {
        "display": "SOL-PERP",
        "tp_pct": 0.030,      # Take Profit: +3.0%
        "sl_pct": 0.015,      # Stop Loss: -1.5%
        "be_pct": 0.012,      # Break-Even: +1.2%
        "vol_factor": 1.15,   # Factor mínimo de volumen MA20
    },
    "ETH-USD": {
        "display": "ETH-PERP",
        "tp_pct": 0.015,      # Take Profit: +1.5% (Objetivo realista en 5M)
        "sl_pct": 0.010,      # Stop Loss: -1.0%
        "be_pct": 0.008,      # Break-Even: +0.8%
        "vol_factor": 1.10,   # Factor mínimo de volumen MA20
    }
}

# Parámetros cuantitativos comunes
MARGIN_USD = 3.0
LEVERAGE = 5
MAX_OVERBOUGHT_RSI = 68.0
MIN_OVERSOLD_RSI = 32.0
MAX_WICK_RATIO = 0.45       # Escudo anti-mechazos (>45% de mecha bloquea)

# Estados en memoria
active_trades = {pair: None for pair in ASSET_CONFIG.keys()}
last_processed_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}
eval_counter = 0
telegram_update_offset = 0

# ==========================================
# FUNCIONES DE TELEGRAM
# ==========================================
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM LOG]: {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

# ==========================================
# CONEXIÓN API COINBASE
# ==========================================
def fetch_candles_5m(pair: str):
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Coinbase entrega: [time, low, high, open, close, volume]
            df = pd.DataFrame(data, columns=["timestamp", "low", "high", "open", "close", "volume"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
    except Exception as e:
        print(f"Error al descargar velas para {pair}: {e}")
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
# CÁLCULO DE INDICADORES CUANTITATIVOS
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
# EVALUACIÓN DE SEÑALES (v2.0)
# ==========================================
def evaluate_market_signal(pair: str, df: pd.DataFrame):
    # Evaluamos la última vela totalmente cerrada (penúltima fila del DF)
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

    # 1. Filtro de Tendencia Estructural (EMA 200)
    if c_close <= c_ema:
        return None

    # 2. Escudo Anti-Mechazos
    if wick_ratio > MAX_WICK_RATIO:
        return None

    # 3. Filtro de Volumen Institucional
    if vol_ratio < cfg["vol_factor"]:
        return None

    # 4. Gatillo de Entrada por RSI (Pullback >= 50 o Ruptura >= 55)
    crossed_rebound = (p_rsi < 50.0) and (c_rsi >= 50.0)
    crossed_momentum = (p_rsi < 55.0) and (c_rsi >= 55.0)

    if (crossed_rebound or crossed_momentum) and (c_rsi <= MAX_OVERBOUGHT_RSI):
        tp_price = c_close * (1 + cfg["tp_pct"])
        sl_price = c_close * (1 - cfg["sl_pct"])
        be_price = c_close * (1 + cfg["be_pct"])

        return {
            "pair": pair,
            "display": cfg["display"],
            "type": "LONG",
            "entry_price": c_close,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "be_price": be_price,
            "tp_pct": cfg["tp_pct"] * 100,
            "sl_pct": cfg["sl_pct"] * 100,
            "be_moved": False,
            "timestamp": int(closed["timestamp"])
        }

    return None

# ==========================================
# MONITOR DE SALIDAS (TP / SL / BREAK-EVEN)
# ==========================================
def monitor_active_trades():
    for pair, trade in list(active_trades.items()):
        if trade is None:
            continue

        live_price = fetch_live_price(pair)
        if live_price <= 0:
            continue

        # Alerta de Break-Even (+1.2% en SOL / +0.8% en ETH)
        if not trade["be_moved"] and live_price >= trade["be_price"]:
            trade["be_moved"] = True
            send_telegram(
                f"🛡️ <b>BREAK-EVEN ACTIVADO · {trade['display']}</b>\n\n"
                f"💵 Precio Actual: <b>${live_price:,.2f}</b>\n"
                f"🎯 Entrada: <b>${trade['entry_price']:,.2f}</b>\n\n"
                f"⚡ <i>Acción: Mueve tu Stop Loss en Phantom al precio de entrada para blindar la operación sin riesgo.</i>"
            )

        # Take Profit
        if live_price >= trade["tp_price"]:
            send_telegram(
                f"🎉 <b>TAKE PROFIT ALCANZADO · {trade['display']}</b>\n\n"
                f"💰 Precio Salida: <b>${live_price:,.2f}</b> (+{trade['tp_pct']}%)\n"
                f"⚡ Posición cerrada con éxito. El par queda LIBRE."
            )
            active_trades[pair] = None

        # Stop Loss
        elif live_price <= trade["sl_price"]:
            send_telegram(
                f"🛑 <b>STOP LOSS EJECUTADO · {trade['display']}</b>\n\n"
                f"📉 Precio Salida: <b>${live_price:,.2f}</b> (-{trade['sl_pct']}%)\n"
                f"⚡ Posición cerrada. El par queda LIBRE."
            )
            active_trades[pair] = None

# ==========================================
# COMANDOS INTERACTIVOS DE TELEGRAM
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
                report = "📊 <b>TELEMETRÍA MULTIACTIVO (5M)</b>\n"
                for pair, cfg in ASSET_CONFIG.items():
                    df = fetch_candles_5m(pair)
                    if df is not None and len(df) >= 200:
                        df = calculate_indicators(df)
                        last = df.iloc[-1]
                        vol_ratio = last["volume"] / last["vol_ma20"] if last["vol_ma20"] > 0 else 0
                        report += (
                            f"\n<b>[{cfg['display']}]</b>\n"
                            f"💵 Spot: ${last['close']:,.2f}\n"
                            f"📈 EMA 200: ${last['ema_200']:,.2f}\n"
                            f"📉 RSI (14): {last['rsi']:.1f}\n"
                            f"📊 Vol MA20: {vol_ratio:.2f}x\n"
                        )
                send_telegram(report)

            elif text in ["/status", "/estado"]:
                sol_st = "🟢 LIBRE" if active_trades["SOL-USD"] is None else "🟡 EN POSICIÓN"
                eth_st = "🟢 LIBRE" if active_trades["ETH-USD"] is None else "🟡 EN POSICIÓN"
                msg = (
                    f"🟢 <b>ESTADO DEL SISTEMA (Render Node)</b>\n\n"
                    f"• <b>SOL-PERP:</b> {sol_st}\n"
                    f"• <b>ETH-PERP:</b> {eth_st}\n"
                    f"• Velas evaluadas: {eval_counter}\n"
                    f"• Gestión: ${MARGIN_USD} USDC a {LEVERAGE}x\n"
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
        print(f"Error procesando updates de Telegram: {e}")

# ==========================================
# BUCLE PRINCIPAL DE ESCANEO
# ==========================================
def main_scanner_loop():
    global eval_counter
    print("Iniciando escáner cuantitativo dual (SOL + ETH)...")
    send_telegram(
        "⚡ <b>BOT CUANTITATIVO MULTIACTIVO ACTIVO (v2.0)</b>\n\n"
        "• <b>Pares vigilados:</b> SOL-PERP (TP 3.0%) | ETH-PERP (TP 1.5%)\n"
        "• <b>Margen configurado:</b> $3 USDC a 5x ($15 posición)\n"
        "• <b>Comandos:</b> /price | /status | /close [sol|eth] | /restart"
    )

    while True:
        try:
            # 1. Monitorear salidas si hay operaciones activas
            monitor_active_trades()

            # 2. Escaneo periódico de velas cerradas para cada activo
            for pair in ASSET_CONFIG.keys():
                if active_trades[pair] is not None:
                    continue  # Si ya hay un trade activo en este par, saltamos

                df = fetch_candles_5m(pair)
                if df is None or len(df) < 205:
                    continue

                closed_candle_ts = int(df.iloc[-2]["timestamp"])

                # Verificación estricta de vela cerrada para no duplicar evaluación
                if closed_candle_ts > last_processed_timestamps[pair]:
                    df = calculate_indicators(df)
                    signal = evaluate_market_signal(pair, df)

                    if signal:
                        active_trades[pair] = signal
                        send_telegram(
                            f"🚨 <b>SEÑAL LONG DETECTADA · {signal['display']}</b>\n\n"
                            f"💵 Entrada Sugerida: <b>${signal['entry_price']:,.2f}</b>\n"
                            f"🎯 Take Profit (+{signal['tp_pct']}%): <b>${signal['tp_price']:,.2f}</b>\n"
                            f"🛑 Stop Loss (-{signal['sl_pct']}%): <b>${signal['sl_price']:,.2f}</b>\n"
                            f"🛡️ Punto Break-Even: <b>${signal['be_price']:,.2f}</b>\n\n"
                            f"⚡ <i>Gestión: Abre con ${MARGIN_USD} USDC a {LEVERAGE}x en Phantom.</i>"
                        )

                    last_processed_timestamps[pair] = closed_candle_ts
                    eval_counter += 1

            # 3. Procesar comandos interactivos de Telegram
            handle_telegram_commands()

        except Exception as e:
            print(f"Error en el bucle principal: {e}")

        time.sleep(3)

if __name__ == "__main__":
    main_scanner_loop()
