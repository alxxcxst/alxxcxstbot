import os
import time
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from flask import Flask, jsonify

# ==============================================================================
# CONFIGURACIÓN DEL SERVIDOR FLASK (RENDER HEALTH CHECK)
# ==============================================================================
app = Flask(__name__)

# ==============================================================================
# VARIABLES DE ENTORNO Y CONFIGURACIÓN DE RIESGO (CAPITAL ULTRABAJO: $4.60 USD)
# ==============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parámetros de Cuenta y Gestión de Riesgo Blindada (Phantom Perps)
ACCOUNT_BALANCE_USD = float(os.getenv("ACCOUNT_BALANCE_USD", "4.60"))
MARGIN_USD = float(os.getenv("MARGIN_USD", "2.50"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
TAKER_FEE_ROUNDTRIP_PCT = 0.0012  # ~0.06% apertura + ~0.06% cierre en Phantom/Jupiter Perps
SINGLE_SLOT_LOCK = os.getenv("SINGLE_SLOT_LOCK", "true").lower() == "true"

# Umbrales Técnicos Cuantitativos (Velas 5M)
MAX_OVERBOUGHT_RSI = 68.0
MIN_OVERSOLD_RSI = 32.0
MAX_WICK_RATIO = 0.42

ASSET_CONFIG = {
    "SOL-USD": {
        "display": "SOL-PERP",
        "tp_pct": 0.030,      # Take Profit: +3.0% spot (+15.0% sobre margen a 5x)
        "sl_pct": 0.015,      # Stop Loss:   -1.5% spot (-7.5% sobre margen a 5x)
        "be_pct": 0.011,      # Break-Even:  +1.1% spot (cubre fees y asegura ganancia)
        "vol_factor": 1.15,   # Volumen mínimo vs MA20
        "tv_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3ASOLUSD&interval=5"
    },
    "ETH-USD": {
        "display": "ETH-PERP",
        "tp_pct": 0.020,      # Take Profit: +2.0% spot (+10.0% sobre margen a 5x, R:B 1:2)
        "sl_pct": 0.010,      # Stop Loss:   -1.0% spot (-5.0% sobre margen a 5x)
        "be_pct": 0.008,      # Break-Even:  +0.8% spot
        "vol_factor": 1.10,   # Volumen mínimo vs MA20
        "tv_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3AETHUSD&interval=5"
    }
}

# ==============================================================================
# ESTADO GLOBAL DEL SISTEMA (PROTEGIDO CON LOCK CONCURRENTE)
# ==============================================================================
state_lock = threading.Lock()
scanner_started = False
boot_time = datetime.now(timezone.utc)

active_trades = {pair: None for pair in ASSET_CONFIG.keys()}
last_processed_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}
last_alerted_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}

eval_counter = 0
telegram_update_offset = 0
last_bot_ui_message_id = None  # ID del último mensaje de UI (status/telemetría) para Auto-Clean

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({"User-Agent": "QuantMultiAssetBot/3.0"})


# ==============================================================================
# UTILIDADES DE FORMATO Y TECLADOS INTERACTIVOS (INLINE KEYBOARDS)
# ==============================================================================
def get_uptime_str() -> str:
    delta = datetime.now(timezone.utc) - boot_time
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def build_main_keyboard() -> dict:
    """Diseña un panel de botones interactivos limpio y profesional."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Refrescar Status", "callback_data": "cmd_status"},
                {"text": "📊 Telemetría 5M", "callback_data": "cmd_price"}
            ],
            [
                {"text": "📈 SOL 5M (TV)", "url": ASSET_CONFIG["SOL-USD"]["tv_url"]},
                {"text": "📈 ETH 5M (TV)", "url": ASSET_CONFIG["ETH-USD"]["tv_url"]}
            ],
            [
                {"text": "🔓 Liberar SOL", "callback_data": "cmd_free_sol"},
                {"text": "🔓 Liberar ETH", "callback_data": "cmd_free_eth"}
            ]
        ]
    }


def build_startup_keyboard() -> dict:
    """Teclado exclusivo de confirmación de arranque con links directos 5M."""
    return {
        "inline_keyboard": [
            [
                {"text": "📈 SOL-PERP (5M)", "url": ASSET_CONFIG["SOL-USD"]["tv_url"]},
                {"text": "📈 ETH-PERP (5M)", "url": ASSET_CONFIG["ETH-USD"]["tv_url"]}
            ],
            [
                {"text": "📊 Ver Telemetría", "callback_data": "cmd_price"},
                {"text": "🟢 Estado del Sistema", "callback_data": "cmd_status"}
            ]
        ]
    }


def build_signal_keyboard(pair: str, display: str, tv_url: str) -> dict:
    """Teclado ejecutivo para tarjetas de alerta LONG/SHORT."""
    short_code = "sol" if "SOL" in pair else "eth"
    return {
        "inline_keyboard": [
            [
                {"text": f"📈 Abrir {display} (5M) en TradingView", "url": tv_url}
            ],
            [
                {"text": "🔄 Ver Status", "callback_data": "cmd_status"},
                {"text": f"🔓 Descartar {display}", "callback_data": f"cmd_free_{short_code}"}
            ]
        ]
    }


# ==============================================================================
# COMUNICACIÓN CON TELEGRAM (ANTI-SPAM & AUTO-CLEAN)
# ==============================================================================
def delete_telegram_message(message_id: int):
    """Borra un mensaje específico en Telegram de forma silenciosa."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id}
    try:
        HTTP_SESSION.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[WARN] No se pudo borrar el mensaje {message_id}: {e}")


def answer_callback_query(callback_id: str, text: str = ""):
    """Confirma la recepción de un botón inline en Telegram para quitar el reloj de carga."""
    if not TELEGRAM_TOKEN or not callback_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        HTTP_SESSION.post(url, json=payload, timeout=5)
    except Exception:
        pass


def send_telegram(text: str, reply_markup: dict = None, is_ui_response: bool = False) -> int:
    """
    Envía un mensaje a Telegram.
    Si is_ui_response=True, borra primero la respuesta previa de telemetría/status
    para mantener únicamente 1 mensaje de control activo en el chat (Auto-Clean).
    """
    global last_bot_ui_message_id

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[LOG TELEGRAM]:\n{text}")
        return None

    if is_ui_response and last_bot_ui_message_id is not None:
        delete_telegram_message(last_bot_ui_message_id)
        last_bot_ui_message_id = None

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
        resp = HTTP_SESSION.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            msg_id = resp.json().get("result", {}).get("message_id")
            if is_ui_response and msg_id:
                last_bot_ui_message_id = msg_id
            return msg_id
    except Exception as e:
        print(f"[ERROR] Fallo al enviar mensaje a Telegram: {e}")
    return None


# ==============================================================================
# CONEXIÓN CON COINBASE EXCHANGE API
# ==============================================================================
def fetch_candles_5m(pair: str) -> pd.DataFrame:
    """Descarga hasta 300 velas históricas de 5 minutos desde Coinbase."""
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    try:
        resp = HTTP_SESSION.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                df = pd.DataFrame(
                    data,
                    columns=["timestamp", "low", "high", "open", "close", "volume"]
                )
                df = df.sort_values("timestamp").reset_index(drop=True)
                return df
    except Exception as e:
        print(f"[ERROR] Descargando velas 5m de {pair}: {e}")
    return None


def fetch_live_price(pair: str) -> float:
    """Obtiene el último precio spot en tiempo real desde Coinbase."""
    url = f"https://api.exchange.coinbase.com/products/{pair}/ticker"
    try:
        resp = HTTP_SESSION.get(url, timeout=6)
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception:
        pass
    return 0.0


# ==============================================================================
# MOTOR CUANTITATIVO: INDICADORES Y PROBABILIDAD DE CONFLUENCIA
# ==============================================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula EMA 200, RSI (14) de Wilder y Media Móvil de Volumen (MA20)."""
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # EMA 200 estructural
    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()
    # EMA 20 táctica para medir pendiente de corto plazo
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()

    # RSI (14) suavizado exponencial (Wilder)
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -1.0 * delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)

    # Media Móvil Simple de Volumen (20 periodos)
    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    return df


def compute_confluence_probability(
    direction: str,
    close_p: float,
    ema_200: float,
    ema_20: float,
    rsi: float,
    vol_ratio: float,
    vol_factor: float,
    adverse_wick_ratio: float
) -> float:
    """
    Calcula la probabilidad estimada de éxito (60.0% - 92.0%) basada en:
    1. Alineación estructural EMA 200 + EMA 20
    2. Volumen institucional relativo vs umbral
    3. Zona de momento RSI con espacio de recorrido
    4. Limpieza de la vela / ausencia de rechazo
    """
    score = 52.0  # Base estadística de ruptura con filtro de tendencia + volumen

    # 1. Fuerza y alineación de tendencia (EMA 20 y EMA 200)
    ema_dist_pct = abs(close_p - ema_200) / ema_200 if ema_200 > 0 else 0
    if direction == "LONG":
        if close_p > ema_20 > ema_200:
            score += 10.0
        elif close_p > ema_200:
            score += 5.0
    else:
        if close_p < ema_20 < ema_200:
            score += 10.0
        elif close_p < ema_200:
            score += 5.0

    # Penalizar si el precio está excesivamente extendido (>2.5% lejos de EMA 200 en 5m)
    if 0.001 <= ema_dist_pct <= 0.018:
        score += 5.0
    elif ema_dist_pct > 0.025:
        score -= 4.0

    # 2. Volumen Institucional Relativo
    vol_surplus = max(0.0, vol_ratio - vol_factor)
    score += min(12.0, vol_surplus * 10.0)

    # 3. Calidad de la Zona RSI
    if direction == "LONG":
        if 52.0 <= rsi <= 61.0:
            score += 8.0
        elif 50.0 <= rsi <= 66.0:
            score += 4.5
    else:
        if 39.0 <= rsi <= 48.0:
            score += 8.0
        elif 34.0 <= rsi <= 50.0:
            score += 4.5

    # 4. Limpieza del cuerpo de la vela (baja mecha contraria)
    wick_quality = max(0.0, (MAX_WICK_RATIO - adverse_wick_ratio) / MAX_WICK_RATIO)
    score += wick_quality * 6.0

    return round(float(np.clip(score, 60.0, 92.0)), 1)


def evaluate_market_signal(pair: str, df: pd.DataFrame) -> dict:
    """
    Evalúa la última vela cerrada de 5M (iloc[-2]) para detectar entradas
    LONG y SHORT con filtros de tendencia, volumen institucional, RSI y mechazos.
    """
    closed = df.iloc[-2]
    prev = df.iloc[-3]
    cfg = ASSET_CONFIG[pair]

    c_open = float(closed["open"])
    c_high = float(closed["high"])
    c_low = float(closed["low"])
    c_close = float(closed["close"])
    c_ema200 = float(closed["ema_200"])
    c_ema20 = float(closed["ema_20"])
    c_rsi = float(closed["rsi"])
    p_rsi = float(prev["rsi"])
    c_vol = float(closed["volume"])
    c_vol_ma = float(closed["vol_ma20"]) if pd.notna(closed["vol_ma20"]) else 0.0

    if c_vol_ma <= 0:
        return None

    vol_ratio = c_vol / c_vol_ma
    if vol_ratio < cfg["vol_factor"]:
        return None

    candle_range = c_high - c_low
    if candle_range <= 0:
        return None

    upper_wick = c_high - max(c_open, c_close)
    lower_wick = min(c_open, c_close) - c_low
    upper_wick_ratio = upper_wick / candle_range
    lower_wick_ratio = lower_wick / candle_range

    notional_usd = MARGIN_USD * LEVERAGE
    est_fees_usd = notional_usd * TAKER_FEE_ROUNDTRIP_PCT

    # ------------------------------------------------------------------
    # ESCENARIO 1: SEÑAL LONG (Tendencia Alcista sobre EMA 200)
    # ------------------------------------------------------------------
    if c_close > c_ema200 and c_close > c_open:
        if upper_wick_ratio <= MAX_WICK_RATIO:
            crossed_rebound = (p_rsi < 50.0) and (c_rsi >= 50.0)
            crossed_momentum = (p_rsi < 55.0) and (c_rsi >= 55.0)

            if (crossed_rebound or crossed_momentum) and (c_rsi <= MAX_OVERBOUGHT_RSI):
                tp_price = c_close * (1.0 + cfg["tp_pct"])
                sl_price = c_close * (1.0 - cfg["sl_pct"])
                be_price = c_close * (1.0 + cfg["be_pct"])

                prob = compute_confluence_probability(
                    "LONG", c_close, c_ema200, c_ema20, c_rsi,
                    vol_ratio, cfg["vol_factor"], upper_wick_ratio
                )

                net_tp_usd = (notional_usd * cfg["tp_pct"]) - est_fees_usd
                net_sl_usd = (notional_usd * cfg["sl_pct"]) + est_fees_usd

                return {
                    "pair": pair,
                    "display": cfg["display"],
                    "direction": "LONG",
                    "trend": "🟢 Alcista (Sobre EMA 200)",
                    "entry_price": c_close,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "be_price": be_price,
                    "tp_pct": cfg["tp_pct"] * 100.0,
                    "sl_pct": cfg["sl_pct"] * 100.0,
                    "be_pct": cfg["be_pct"] * 100.0,
                    "net_tp_usd": net_tp_usd,
                    "net_sl_usd": net_sl_usd,
                    "rsi": c_rsi,
                    "vol_ratio": vol_ratio,
                    "probability": prob,
                    "tv_url": cfg["tv_url"],
                    "be_moved": False,
                    "timestamp": int(closed["timestamp"])
                }

    # ------------------------------------------------------------------
    # ESCENARIO 2: SEÑAL SHORT (Tendencia Bajista bajo EMA 200)
    # ------------------------------------------------------------------
    elif c_close < c_ema200 and c_close < c_open:
        if lower_wick_ratio <= MAX_WICK_RATIO:
            crossed_breakdown = (p_rsi > 50.0) and (c_rsi <= 50.0)
            crossed_bear_mom = (p_rsi > 45.0) and (c_rsi <= 45.0)

            if (crossed_breakdown or crossed_bear_mom) and (c_rsi >= MIN_OVERSOLD_RSI):
                tp_price = c_close * (1.0 - cfg["tp_pct"])
                sl_price = c_close * (1.0 + cfg["sl_pct"])
                be_price = c_close * (1.0 - cfg["be_pct"])

                prob = compute_confluence_probability(
                    "SHORT", c_close, c_ema200, c_ema20, c_rsi,
                    vol_ratio, cfg["vol_factor"], lower_wick_ratio
                )

                net_tp_usd = (notional_usd * cfg["tp_pct"]) - est_fees_usd
                net_sl_usd = (notional_usd * cfg["sl_pct"]) + est_fees_usd

                return {
                    "pair": pair,
                    "display": cfg["display"],
                    "direction": "SHORT",
                    "trend": "🔴 Bajista (Bajo EMA 200)",
                    "entry_price": c_close,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "be_price": be_price,
                    "tp_pct": cfg["tp_pct"] * 100.0,
                    "sl_pct": cfg["sl_pct"] * 100.0,
                    "be_pct": cfg["be_pct"] * 100.0,
                    "net_tp_usd": net_tp_usd,
                    "net_sl_usd": net_sl_usd,
                    "rsi": c_rsi,
                    "vol_ratio": vol_ratio,
                    "probability": prob,
                    "tv_url": cfg["tv_url"],
                    "be_moved": False,
                    "timestamp": int(closed["timestamp"])
                }

    return None


# ==============================================================================
# MONITOREO ACTIVO DE POSICIONES (LONG & SHORT: BREAK-EVEN, TP Y SL)
# ==============================================================================
def monitor_active_trades():
    for pair, trade in list(active_trades.items()):
        if trade is None:
            continue

        live_price = fetch_live_price(pair)
        if live_price <= 0:
            continue

        direction = trade["direction"]
        be_hit = (
            (direction == "LONG" and live_price >= trade["be_price"]) or
            (direction == "SHORT" and live_price <= trade["be_price"])
        )
        tp_hit = (
            (direction == "LONG" and live_price >= trade["tp_price"]) or
            (direction == "SHORT" and live_price <= trade["tp_price"])
        )
        sl_hit = (
            (direction == "LONG" and live_price <= trade["sl_price"]) or
            (direction == "SHORT" and live_price >= trade["sl_price"])
        )

        # 1. Alerta de Take Profit (prioridad si salta directo a TP)
        if tp_hit:
            send_telegram(
                f"✅ <b>TAKE PROFIT ALCANZADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Precio de Cierre:</b> <code>${live_price:,.2f}</code> <i>(+{trade['tp_pct']:.1f}%)</i>\n"
                f"📥 <b>Precio de Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"💵 <b>PnL Neto Est. ({LEVERAGE}x):</b> <code>+${trade['net_tp_usd']:.2f} USD</code>\n\n"
                f"🔓 <i>Posición finalizada con éxito. {trade['display']} queda libre para escaneo.</i>",
                reply_markup=build_main_keyboard()
            )
            with state_lock:
                active_trades[pair] = None
            continue

        # 2. Alerta de Stop Loss
        if sl_hit:
            reason_note = (
                "Cierre en Break-Even / Sin pérdida."
                if trade["be_moved"]
                else f"Pérdida controlada (-${trade['net_sl_usd']:.2f} USD)."
            )
            send_telegram(
                f"🛑 <b>STOP LOSS EJECUTADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📉 <b>Precio de Cierre:</b> <code>${live_price:,.2f}</code> <i>(-{trade['sl_pct']:.1f}%)</i>\n"
                f"📥 <b>Precio de Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"🛡️ <b>Estado de Riesgo:</b> {reason_note}\n\n"
                f"🔓 <i>{trade['display']} queda libre para nuevos escaneos.</i>",
                reply_markup=build_main_keyboard()
            )
            with state_lock:
                active_trades[pair] = None
            continue

        # 3. Alerta de Activación de Break-Even
        if not trade["be_moved"] and be_hit:
            trade["be_moved"] = True
            send_telegram(
                f"🛡️ <b>BREAK-EVEN ACTIVADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>Precio Actual:</b> <code>${live_price:,.2f}</code> <i>(+{trade['be_pct']:.1f}%)</i>\n"
                f"📥 <b>Precio de Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"🎯 <b>Rumbo a TP:</b> <code>${trade['tp_price']:,.2f}</code>\n\n"
                f"⚡ <b>ACCIÓN EN PHANTOM:</b> Mueve inmediatamente tu Stop Loss a <code>${trade['entry_price']:,.2f}</code> para blindar la operación con riesgo cero."
            )


# ==============================================================================
# GENERADORES DE REPORTES (/price y /status)
# ==============================================================================
def generate_telemetry_report() -> str:
    report = (
        "📊 <b>TELEMETRÍA CUANTITATIVA (5M)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
    )
    for pair, cfg in ASSET_CONFIG.items():
        df = fetch_candles_5m(pair)
        if df is not None and len(df) >= 200:
            df = calculate_indicators(df)
            last = df.iloc[-1]
            close_p = float(last["close"])
            ema_p = float(last["ema_200"])
            rsi_val = float(last["rsi"])
            vol_ma = float(last["vol_ma20"]) if pd.notna(last["vol_ma20"]) else 0.0
            vol_ratio = float(last["volume"]) / vol_ma if vol_ma > 0 else 0.0

            dist_pct = ((close_p - ema_p) / ema_p) * 100.0 if ema_p > 0 else 0.0
            trend_icon = "🟢 Alcista" if close_p > ema_p else "🔴 Bajista"
            rsi_state = (
                "🔥 Sobrecompra" if rsi_val >= MAX_OVERBOUGHT_RSI
                else ("❄️ Sobreventa" if rsi_val <= MIN_OVERSOLD_RSI else "⚖️ Neutral")
            )

            report += (
                f"🪙 <b>{cfg['display']}</b>\n"
                f"• <b>Spot Actual:</b> <code>${close_p:,.2f}</code>\n"
                f"• <b>Tendencia:</b> <b>{trend_icon}</b> (EMA200: <code>${ema_p:,.2f}</code> | <code>{dist_pct:+.2f}%</code>)\n"
                f"• <b>Volumen Relativo:</b> <code>{vol_ratio:.2f}x</code> (Umbral: <code>{cfg['vol_factor']:.2f}x</code>)\n"
                f"• <b>RSI (14):</b> <code>{rsi_val:.1f}</code> ({rsi_state})\n\n"
            )
        else:
            report += f"🪙 <b>{cfg['display']}</b>: <i>Sincronizando datos con Coinbase...</i>\n\n"

    report += f"⏱️ <i>Actualizado: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</i>"
    return report


def generate_status_report() -> str:
    notional = MARGIN_USD * LEVERAGE
    lines = [
        "🟢 <b>ESTADO DEL SISTEMA CUANTITATIVO</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"⏱️ <b>Tiempo Activo:</b> <code>{get_uptime_str()}</code>",
        f"🕯️ <b>Velas 5M Evaluadas:</b> <code>{eval_counter}</code>",
        f"💼 <b>Balance Base:</b> <code>${ACCOUNT_BALANCE_USD:.2f} USD</code>",
        f"🛡️ <b>Sizing Operativo:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Nocional: <code>${notional:.2f}</code>)",
        f"🔒 <b>Candado Microcuenta:</b> <code>{'ACTIVO (Máx 1 trade)' if SINGLE_SLOT_LOCK else 'DESACTIVADO'}</code>",
        "━━━━━━━━━━━━━━━━━━━━━"
    ]

    for pair, cfg in ASSET_CONFIG.items():
        trade = active_trades.get(pair)
        if trade is None:
            lines.append(f"• <b>{cfg['display']}:</b> 🟢 <code>ESCANEANDO (LIBRE)</code>")
        else:
            be_tag = "🛡️ BE Activo" if trade["be_moved"] else "⏳ BE Pendiente"
            lines.append(
                f"• <b>{cfg['display']}:</b> 🟡 <b>EN POSICIÓN {trade['direction']}</b>\n"
                f"   ↳ Entrada: <code>${trade['entry_price']:,.2f}</code> | TP: <code>${trade['tp_price']:,.2f}</code> | SL: <code>${trade['sl_price']:,.2f}</code> ({be_tag})"
            )

    lines.append("\n🔄 <i>Pulsa Refrescar Status para actualizar en vivo.</i>")
    return "\n".join(lines)


# ==============================================================================
# GESTOR DE COMANDOS Y CALLBACKS DE TELEGRAM (UX ANTI-SPAM)
# ==============================================================================
def execute_command_action(cmd: str):
    """Ejecuta la lógica asociada a un comando de texto o botón interactivo."""
    global active_trades

    if cmd in ["/price", "/telemetria", "cmd_price"]:
        report = generate_telemetry_report()
        send_telegram(report, reply_markup=build_main_keyboard(), is_ui_response=True)

    elif cmd in ["/status", "/estado", "/start", "/help", "cmd_status"]:
        msg = generate_status_report()
        send_telegram(msg, reply_markup=build_main_keyboard(), is_ui_response=True)

    elif cmd in ["/close sol", "/free sol", "cmd_free_sol"]:
        with state_lock:
            active_trades["SOL-USD"] = None
        msg = "🔓 <b>SOL-PERP liberado manualmente.</b>\n\n" + generate_status_report()
        send_telegram(msg, reply_markup=build_main_keyboard(), is_ui_response=True)

    elif cmd in ["/close eth", "/free eth", "cmd_free_eth"]:
        with state_lock:
            active_trades["ETH-USD"] = None
        msg = "🔓 <b>ETH-PERP liberado manualmente.</b>\n\n" + generate_status_report()
        send_telegram(msg, reply_markup=build_main_keyboard(), is_ui_response=True)

    elif cmd.startswith("/close") or cmd.startswith("/free") or cmd == "/restart":
        with state_lock:
            active_trades = {p: None for p in ASSET_CONFIG.keys()}
        msg = "⚡ <b>Todos los activos han sido liberados y reiniciados.</b>\n\n" + generate_status_report()
        send_telegram(msg, reply_markup=build_main_keyboard(), is_ui_response=True)


def handle_telegram_updates():
    """
    Escucha comandos de texto y botones inline (callback_query).
    1. Si es un mensaje del usuario, lo borra de inmediato (deleteMessage).
    2. Reemplaza el panel anterior del bot para mantener 1 solo mensaje de respuesta.
    """
    global telegram_update_offset
    if not TELEGRAM_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "offset": telegram_update_offset,
        "timeout": 2,
        "allowed_updates": ["message", "callback_query"]
    }

    try:
        resp = HTTP_SESSION.get(url, params=params, timeout=6)
        if resp.status_code != 200:
            return

        updates = resp.json().get("result", [])
        for update in updates:
            telegram_update_offset = update["update_id"] + 1

            # Caso A: Mensaje de texto enviado por el usuario
            if "message" in update:
                message = update["message"]
                msg_id = message.get("message_id")
                text = message.get("text", "").strip().lower()

                if text.startswith("/"):
                    # Regla Anti-Spam 1: Borrar de inmediato el comando del usuario
                    delete_telegram_message(msg_id)
                    # Limpiar sufijos de @botname si existen
                    clean_cmd = text.split("@")[0]
                    execute_command_action(clean_cmd)

            # Caso B: Botón interactivo presionado (inline_keyboard)
            elif "callback_query" in update:
                cb = update["callback_query"]
                cb_id = cb.get("id")
                cb_data = cb.get("data", "")
                answer_callback_query(cb_id, "Actualizando panel...")
                execute_command_action(cb_data)

    except Exception as e:
        print(f"[ERROR] Procesando updates de Telegram: {e}")


# ==============================================================================
# BUCLE PRINCIPAL DEL ESCÁNER CUANTITATIVO
# ==============================================================================
def format_signal_card(signal: dict, blocked_by_slot: bool = False) -> str:
    icon = "🟢" if signal["direction"] == "LONG" else "🔴"
    notional = MARGIN_USD * LEVERAGE
    slot_notice = (
        "\n⚠️ <i>Nota: Candado Microcuenta activo (ya tienes otra posición abierta). Ejecuta solo si liberaste margen.</i>"
        if blocked_by_slot else ""
    )

    return (
        f"{icon} <b>SEÑAL {signal['direction']} | {signal['display']} (5M)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Probabilidad de Éxito:</b> <code>{signal['probability']:.1f}%</code>\n"
        f"🧭 <b>Tendencia:</b> {signal['trend']}\n"
        f"📊 <b>Confluencias:</b> RSI <code>{signal['rsi']:.1f}</code> | Vol <code>{signal['vol_ratio']:.2f}x</code>\n\n"
        f"📥 <b>Precio de Entrada:</b> <code>${signal['entry_price']:,.2f}</code>\n"
        f"✅ <b>Take Profit:</b> <code>${signal['tp_price']:,.2f}</code> <i>(+{signal['tp_pct']:.1f}% | +${signal['net_tp_usd']:.2f})</i>\n"
        f"🛡️ <b>Activar Break-Even:</b> <code>${signal['be_price']:,.2f}</code> <i>(+{signal['be_pct']:.1f}%)</i>\n"
        f"🛑 <b>Stop Loss:</b> <code>${signal['sl_price']:,.2f}</code> <i>(-{signal['sl_pct']:.1f}% | -${signal['net_sl_usd']:.2f})</i>\n\n"
        f"💼 <b>Gestión Phantom:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Posición: <code>${notional:.2f}</code>)"
        f"{slot_notice}"
    )


def main_scanner_loop():
    global eval_counter
    print("[INFO] Iniciando hilo de escaneo cuantitativo multiactivo (SOL & ETH)...")

    # Validar conexión inicial a Coinbase antes de enviar mensaje de arranque
    sol_check = fetch_live_price("SOL-USD")
    eth_check = fetch_live_price("ETH-USD")
    conn_status = "🟢 Conectado a Coinbase API" if (sol_check > 0 and eth_check > 0) else "🟡 Conectando a Coinbase API..."
    notional = MARGIN_USD * LEVERAGE

    startup_msg = (
        f"⚡ <b>BOT CUANTITATIVO MULTIACTIVO EN LÍNEA (v3.0)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Estado API:</b> {conn_status}\n"
        f"🪙 <b>Cotización Inicial:</b> SOL <code>${sol_check:,.2f}</code> | ETH <code>${eth_check:,.2f}</code>\n"
        f"↕️ <b>Motor Direccional:</b> LONG &amp; SHORT (EMA 200 + Vol MA20 + RSI 14)\n"
        f"🛡️ <b>Gestión Blindada:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Nocional <code>${notional:.2f}</code> sobre balance de <code>${ACCOUNT_BALANCE_USD:.2f}</code>)\n"
        f"🧹 <b>UX Auto-Clean:</b> Activado (borrado automático de comandos y panel único)\n\n"
        f"👇 <i>Accesos directos a gráficas 5M y panel de control:</i>"
    )
    send_telegram(startup_msg, reply_markup=build_startup_keyboard(), is_ui_response=True)

    while True:
        try:
            # 1. Monitorear TP, SL y Break-Even de posiciones activas
            monitor_active_trades()

            # 2. Escanear velas cerradas de 5M para cada par
            for pair, cfg in ASSET_CONFIG.items():
                df = fetch_candles_5m(pair)
                if df is None or len(df) < 205:
                    continue

                closed_candle_ts = int(df.iloc[-2]["timestamp"])

                if closed_candle_ts > last_processed_timestamps[pair]:
                    df = calculate_indicators(df)
                    signal = evaluate_market_signal(pair, df)

                    if signal and signal["timestamp"] != last_alerted_timestamps[pair]:
                        last_alerted_timestamps[pair] = signal["timestamp"]

                        # Verificar si ya existe otra posición abierta cuando SINGLE_SLOT_LOCK está activo
                        other_active = any(
                            t is not None for p, t in active_trades.items() if p != pair
                        )
                        blocked_by_slot = SINGLE_SLOT_LOCK and other_active

                        if active_trades[pair] is None and not blocked_by_slot:
                            with state_lock:
                                active_trades[pair] = signal

                        card_text = format_signal_card(signal, blocked_by_slot=blocked_by_slot)
                        sig_kb = build_signal_keyboard(pair, cfg["display"], cfg["tv_url"])
                        send_telegram(card_text, reply_markup=sig_kb, is_ui_response=False)

                    last_processed_timestamps[pair] = closed_candle_ts
                    eval_counter += 1

            # 3. Procesar comandos y botones de Telegram (Auto-Clean)
            handle_telegram_updates()

        except Exception as e:
            print(f"[ERROR] Excepción en main_scanner_loop: {e}")

        time.sleep(3)


# ==============================================================================
# ARRANQUE SEGURO DEL WORKER BAJO GUNICORN Y EJECUCIÓN DIRECTA
# ==============================================================================
def start_background_scanner():
    """
    Garantiza que el hilo del escáner inicie tanto cuando Gunicorn importa `app:app`
    en Render como cuando se ejecuta localmente con `python app.py`.
    """
    global scanner_started
    with state_lock:
        if not scanner_started:
            scanner_started = True
            bot_thread = threading.Thread(target=main_scanner_loop, daemon=True)
            bot_thread.start()


@app.route("/")
def health_check():
    return jsonify({
        "status": "online",
        "service": "Bot Cuantitativo Multiactivo (SOL-PERP & ETH-PERP)",
        "uptime": get_uptime_str(),
        "candles_evaluated": eval_counter,
        "active_positions": {
            k: (v["direction"] if v else None) for k, v in active_trades.items()
        }
    }), 200


# Arrancar hilo al cargar el módulo en Gunicorn
start_background_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
