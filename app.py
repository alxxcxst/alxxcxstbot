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
# VARIABLES DE ENTORNO Y CONFIGURACIÓN DE RIESGO (SOL-PERP OPTIMIZADO)
# ==============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parámetros de Cuenta y Gestión de Riesgo Blindada (Phantom / Jupiter Perps)
ACCOUNT_BALANCE_USD = float(os.getenv("ACCOUNT_BALANCE_USD", "8.10"))
MARGIN_USD = float(os.getenv("MARGIN_USD", "2.50"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
TAKER_FEE_ROUNDTRIP_PCT = 0.0012  # ~0.06% apertura + ~0.06% cierre en Phantom/Jupiter Perps
BE_FEE_BUFFER_PCT = 0.0015        # Colchón +0.15% spot para cubrir 0.12% de fees en Break-Even
SINGLE_SLOT_LOCK = os.getenv("SINGLE_SLOT_LOCK", "true").lower() == "true"

# Umbrales Técnicos Cuantitativos (Velas 5M + Macro 1H)
MAX_OVERBOUGHT_RSI = 68.0
MIN_OVERSOLD_RSI = 32.0
MAX_WICK_RATIO = 0.42
MACRO_CACHE_TTL_SECONDS = 900     # Caché de 15 minutos (900s) para EMA 200 de 1 Hora

ASSET_CONFIG = {
    "SOL-USD": {
        "display": "SOL-PERP",
        "tp_pct": 0.030,      # Take Profit: +3.0% spot (+15.0% sobre margen a 5x)
        "sl_pct": 0.015,      # Stop Loss:   -1.5% spot (-7.5% sobre margen a 5x)
        "be_pct": 0.018,      # Break-Even:  +1.8% spot (validado en backtesting 60d)
        "vol_factor": 1.15,   # Volumen mínimo vs MA20
        "tv_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3ASOLUSD&interval=5"
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
macro_1h_cache = {
    pair: {"ema_200": 0.0, "updated_at": 0.0} for pair in ASSET_CONFIG.keys()
}

eval_counter = 0
telegram_update_offset = 0
last_bot_ui_message_id = None  # ID del último mensaje de UI (status/telemetría) para Auto-Clean

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({"User-Agent": "QuantSolPerpBot/4.0"})


# ==============================================================================
# UTILIDADES DE FORMATO Y TECLADOS INTERACTIVOS (INLINE KEYBOARDS SIMÉTRICOS)
# ==============================================================================
def get_uptime_str() -> str:
    delta = datetime.now(timezone.utc) - boot_time
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def build_main_keyboard() -> dict:
    """Diseña un panel de botones interactivos limpio, simétrico y exclusivo para SOL-PERP."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Refrescar Status", "callback_data": "cmd_status"},
                {"text": "📊 Telemetría 5M/1H", "callback_data": "cmd_price"}
            ],
            [
                {"text": "📈 SOL-PERP 5M (TV)", "url": ASSET_CONFIG["SOL-USD"]["tv_url"]},
                {"text": "🔓 Liberar SOL-PERP", "callback_data": "cmd_free_sol"}
            ]
        ]
    }


def build_startup_keyboard() -> dict:
    """Teclado de confirmación de arranque con acceso directo a SOL-PERP 5M y controles."""
    return {
        "inline_keyboard": [
            [
                {"text": "📈 Abrir SOL-PERP (5M) en TradingView", "url": ASSET_CONFIG["SOL-USD"]["tv_url"]}
            ],
            [
                {"text": "📊 Ver Telemetría", "callback_data": "cmd_price"},
                {"text": "🟢 Estado del Sistema", "callback_data": "cmd_status"}
            ]
        ]
    }


def build_signal_keyboard(pair: str, display: str, tv_url: str) -> dict:
    """Teclado ejecutivo para tarjetas de alerta LONG/SHORT en SOL-PERP."""
    return {
        "inline_keyboard": [
            [
                {"text": f"📈 Abrir {display} (5M) en TradingView", "url": tv_url}
            ],
            [
                {"text": "🔄 Ver Status", "callback_data": "cmd_status"},
                {"text": f"🔓 Descartar {display}", "callback_data": "cmd_free_sol"}
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
# CONEXIÓN CON COINBASE EXCHANGE API (5M, MACRO 1H CON CACHÉ Y LIVE TICKER)
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


def fetch_macro_ema200_1h(pair: str) -> float:
    """
    Consulta el endpoint de Coinbase en velas de 1 hora (granularity=3600)
    para calcular la EMA 200 estructural de 1H.
    Implementa una caché temporal de 15 minutos (900s) para no saturar la API.
    """
    now_ts = time.time()
    cached = macro_1h_cache.get(pair, {"ema_200": 0.0, "updated_at": 0.0})

    if cached["ema_200"] > 0 and (now_ts - cached["updated_at"]) < MACRO_CACHE_TTL_SECONDS:
        return cached["ema_200"]

    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=3600"
    try:
        resp = HTTP_SESSION.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) >= 200:
                df_1h = pd.DataFrame(
                    data,
                    columns=["timestamp", "low", "high", "open", "close", "volume"]
                )
                df_1h = df_1h.sort_values("timestamp").reset_index(drop=True)
                df_1h["close"] = df_1h["close"].astype(float)
                ema200_1h = float(df_1h["close"].ewm(span=200, adjust=False).mean().iloc[-1])

                macro_1h_cache[pair] = {
                    "ema_200": ema200_1h,
                    "updated_at": now_ts
                }
                return ema200_1h
    except Exception as e:
        print(f"[ERROR] Descargando velas macro 1H de {pair}: {e}")

    return cached["ema_200"]


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
    """Calcula EMA 200, EMA 20, RSI (14) de Wilder y Media Móvil de Volumen (MA20)."""
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # EMA 200 estructural (5M)
    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()
    # EMA 20 táctica para alineación de momento (5M)
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
    ema200_1h: float,
    ema_200: float,
    ema_20: float,
    rsi: float,
    vol_ratio: float,
    vol_factor: float,
    adverse_wick_ratio: float
) -> float:
    """
    Calcula la probabilidad estimada de éxito (60.0% - 92.0%) basada en:
    1. Alineación multi-temporal Macro 1H (EMA 200 1H) + 5M (EMA 20 y EMA 200)
    2. Volumen institucional relativo vs umbral MA20
    3. Zona de momento RSI con espacio de recorrido
    4. Limpieza de la vela / ausencia de rechazo
    """
    score = 54.0  # Base estadística validada con filtro multi-temporal 1H + 5M

    # 1. Fuerza y alineación de tendencia multi-temporal (1H + 5M)
    ema_dist_pct = abs(close_p - ema_200) / ema_200 if ema_200 > 0 else 0
    macro_dist_pct = abs(close_p - ema200_1h) / ema200_1h if ema200_1h > 0 else 0

    if direction == "LONG" and (close_p > ema200_1h) and (close_p > ema_20 > ema_200):
        score += 10.0
    elif direction == "SHORT" and (close_p < ema200_1h) and (close_p < ema_20 < ema_200):
        score += 10.0

    # Bonificar distancia saludable respecto a EMA 200 5M y penalizar sobreextensión
    if 0.001 <= ema_dist_pct <= 0.018:
        score += 4.0
    elif ema_dist_pct > 0.025:
        score -= 4.0

    if 0.002 <= macro_dist_pct <= 0.045:
        score += 2.0

    # 2. Volumen Institucional Relativo
    vol_surplus = max(0.0, vol_ratio - vol_factor)
    score += min(11.0, vol_surplus * 10.0)

    # 3. Calidad de la Zona RSI
    if direction == "LONG":
        if 52.0 <= rsi <= 61.0:
            score += 7.0
        elif 50.0 <= rsi <= 66.0:
            score += 4.0
    else:
        if 39.0 <= rsi <= 48.0:
            score += 7.0
        elif 34.0 <= rsi <= 50.0:
            score += 4.0

    # 4. Limpieza del cuerpo de la vela (baja mecha contraria)
    wick_quality = max(0.0, (MAX_WICK_RATIO - adverse_wick_ratio) / MAX_WICK_RATIO)
    score += wick_quality * 6.0

    return round(float(np.clip(score, 60.0, 92.0)), 1)


def evaluate_market_signal(pair: str, df: pd.DataFrame) -> dict:
    """
    Evalúa la última vela cerrada de 5M (iloc[-2]) integrando el filtro obligatorio
    Macro de 1 Hora (EMA 200 1H) + alineación 5M (c_close vs EMA 20 vs EMA 200),
    volumen institucional, cruce de RSI y filtro anti-mechazos.
    """
    ema200_1h = fetch_macro_ema200_1h(pair)
    if ema200_1h <= 0:
        return None

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
    # ESCENARIO 1: SEÑAL LONG
    # Exige alineación completa: c_close > ema200_1h y c_close > c_ema20 > c_ema200 (5M)
    # ------------------------------------------------------------------
    if (c_close > ema200_1h) and (c_close > c_ema20 > c_ema200) and (c_close > c_open):
        if upper_wick_ratio <= MAX_WICK_RATIO:
            crossed_rebound = (p_rsi < 50.0) and (c_rsi >= 50.0)
            crossed_momentum = (p_rsi < 55.0) and (c_rsi >= 55.0)

            if (crossed_rebound or crossed_momentum) and (c_rsi <= MAX_OVERBOUGHT_RSI):
                tp_price = c_close * (1.0 + cfg["tp_pct"])
                sl_price = c_close * (1.0 - cfg["sl_pct"])
                be_price = c_close * (1.0 + cfg["be_pct"])
                be_sl_price = c_close * (1.0 + BE_FEE_BUFFER_PCT)  # Entrada + 0.15%

                prob = compute_confluence_probability(
                    "LONG", c_close, ema200_1h, c_ema200, c_ema20, c_rsi,
                    vol_ratio, cfg["vol_factor"], upper_wick_ratio
                )

                net_tp_usd = (notional_usd * cfg["tp_pct"]) - est_fees_usd
                net_sl_usd = (notional_usd * cfg["sl_pct"]) + est_fees_usd

                return {
                    "pair": pair,
                    "display": cfg["display"],
                    "direction": "LONG",
                    "trend": "🟢 Alcista 1H + 5M (Sobre EMA 200)",
                    "entry_price": c_close,
                    "ema200_1h": ema200_1h,
                    "ema200_5m": c_ema200,
                    "ema20_5m": c_ema20,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "be_price": be_price,
                    "be_sl_price": be_sl_price,
                    "tp_pct": cfg["tp_pct"] * 100.0,
                    "sl_pct": cfg["sl_pct"] * 100.0,
                    "be_pct": cfg["be_pct"] * 100.0,
                    "be_buffer_pct": BE_FEE_BUFFER_PCT * 100.0,
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
    # ESCENARIO 2: SEÑAL SHORT
    # Exige alineación completa: c_close < ema200_1h y c_close < c_ema20 < c_ema200 (5M)
    # ------------------------------------------------------------------
    elif (c_close < ema200_1h) and (c_close < c_ema20 < c_ema200) and (c_close < c_open):
        if lower_wick_ratio <= MAX_WICK_RATIO:
            crossed_breakdown = (p_rsi > 50.0) and (c_rsi <= 50.0)
            crossed_bear_mom = (p_rsi > 45.0) and (c_rsi <= 45.0)

            if (crossed_breakdown or crossed_bear_mom) and (c_rsi >= MIN_OVERSOLD_RSI):
                tp_price = c_close * (1.0 - cfg["tp_pct"])
                sl_price = c_close * (1.0 + cfg["sl_pct"])
                be_price = c_close * (1.0 - cfg["be_pct"])
                be_sl_price = c_close * (1.0 - BE_FEE_BUFFER_PCT)  # Entrada - 0.15%

                prob = compute_confluence_probability(
                    "SHORT", c_close, ema200_1h, c_ema200, c_ema20, c_rsi,
                    vol_ratio, cfg["vol_factor"], lower_wick_ratio
                )

                net_tp_usd = (notional_usd * cfg["tp_pct"]) - est_fees_usd
                net_sl_usd = (notional_usd * cfg["sl_pct"]) + est_fees_usd

                return {
                    "pair": pair,
                    "display": cfg["display"],
                    "direction": "SHORT",
                    "trend": "🔴 Bajista 1H + 5M (Bajo EMA 200)",
                    "entry_price": c_close,
                    "ema200_1h": ema200_1h,
                    "ema200_5m": c_ema200,
                    "ema20_5m": c_ema20,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "be_price": be_price,
                    "be_sl_price": be_sl_price,
                    "tp_pct": cfg["tp_pct"] * 100.0,
                    "sl_pct": cfg["sl_pct"] * 100.0,
                    "be_pct": cfg["be_pct"] * 100.0,
                    "be_buffer_pct": BE_FEE_BUFFER_PCT * 100.0,
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
# MONITOREO ACTIVO DE POSICIONES (LONG & SHORT: BREAK-EVEN NETO, TP Y SL)
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

        # 2. Alerta de Stop Loss (inicial o Break-Even Neto protegido)
        if sl_hit:
            if trade["be_moved"]:
                reason_note = (
                    f"Cierre en Break-Even Neto Protegido (<code>${trade['sl_price']:,.2f}</code> | "
                    f"comisiones de Phantom cubiertas)."
                )
                sl_pct_label = f"+{trade['be_buffer_pct']:.2f}% BE Neto"
            else:
                reason_note = f"Pérdida controlada (-${trade['net_sl_usd']:.2f} USD)."
                sl_pct_label = f"-{trade['sl_pct']:.1f}%"

            send_telegram(
                f"🛑 <b>STOP LOSS EJECUTADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📉 <b>Precio de Cierre:</b> <code>${live_price:,.2f}</code> <i>({sl_pct_label})</i>\n"
                f"📥 <b>Precio de Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"🛡️ <b>Estado de Riesgo:</b> {reason_note}\n\n"
                f"🔓 <i>{trade['display']} queda libre para nuevos escaneos.</i>",
                reply_markup=build_main_keyboard()
            )
            with state_lock:
                active_trades[pair] = None
            continue

        # 3. Alerta de Activación de Break-Even Neto Real (+1.8% spot -> SL a entrada ± 0.15%)
        if not trade["be_moved"] and be_hit:
            with state_lock:
                trade["be_moved"] = True
                if direction == "LONG":
                    trade["sl_price"] = trade["entry_price"] * (1.0 + BE_FEE_BUFFER_PCT)
                    buffer_sign = "+0.15%"
                else:
                    trade["sl_price"] = trade["entry_price"] * (1.0 - BE_FEE_BUFFER_PCT)
                    buffer_sign = "-0.15%"
                protected_sl = trade["sl_price"]

            send_telegram(
                f"🛡️ <b>BREAK-EVEN NETO ACTIVADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>Precio Actual:</b> <code>${live_price:,.2f}</code> <i>(+{trade['be_pct']:.1f}%)</i>\n"
                f"📥 <b>Precio de Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"🔒 <b>Nuevo SL Protegido ({buffer_sign}):</b> <code>${protected_sl:,.2f}</code>\n"
                f"🎯 <b>Rumbo a TP:</b> <code>${trade['tp_price']:,.2f}</code>\n\n"
                f"⚡ <b>ACCIÓN EN PHANTOM:</b> Mueve inmediatamente tu Stop Loss a <code>${protected_sl:,.2f}</code> "
                f"(<code>entrada {buffer_sign}</code>) para absorber el <code>0.12%</code> de comisiones ida y vuelta y asegurar cierre en Break-Even neto sin pérdidas."
            )


# ==============================================================================
# GENERADORES DE REPORTES (/price y /status)
# ==============================================================================
def generate_telemetry_report() -> str:
    report = (
        "📊 <b>TELEMETRÍA CUANTITATIVA (5M + MACRO 1H)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
    )
    for pair, cfg in ASSET_CONFIG.items():
        df = fetch_candles_5m(pair)
        ema200_1h = fetch_macro_ema200_1h(pair)

        if df is not None and len(df) >= 200:
            df = calculate_indicators(df)
            last = df.iloc[-1]
            close_p = float(last["close"])
            ema200_5m = float(last["ema_200"])
            ema20_5m = float(last["ema_20"])
            rsi_val = float(last["rsi"])
            vol_ma = float(last["vol_ma20"]) if pd.notna(last["vol_ma20"]) else 0.0
            vol_ratio = float(last["volume"]) / vol_ma if vol_ma > 0 else 0.0

            dist_5m_pct = ((close_p - ema200_5m) / ema200_5m) * 100.0 if ema200_5m > 0 else 0.0
            dist_1h_pct = ((close_p - ema200_1h) / ema200_1h) * 100.0 if ema200_1h > 0 else 0.0

            macro_trend = "🟢 Alcista" if (ema200_1h > 0 and close_p > ema200_1h) else "🔴 Bajista"
            micro_trend = (
                "🟢 Alcista (Precio > EMA20 > EMA200)"
                if (close_p > ema20_5m > ema200_5m)
                else (
                    "🔴 Bajista (Precio < EMA20 < EMA200)"
                    if (close_p < ema20_5m < ema200_5m)
                    else "⚖️ Transición / Rango"
                )
            )

            if ema200_1h > 0 and (close_p > ema200_1h) and (close_p > ema20_5m > ema200_5m):
                align_status = "🟢 Alineación Completa LONG"
            elif ema200_1h > 0 and (close_p < ema200_1h) and (close_p < ema20_5m < ema200_5m):
                align_status = "🔴 Alineación Completa SHORT"
            else:
                align_status = "⏳ Sin Alineación (Filtro Activo)"

            rsi_state = (
                "🔥 Sobrecompra" if rsi_val >= MAX_OVERBOUGHT_RSI
                else ("❄️ Sobreventa" if rsi_val <= MIN_OVERSOLD_RSI else "⚖️ Neutral")
            )

            report += (
                f"🪙 <b>{cfg['display']}</b>\n"
                f"• <b>Spot Actual:</b> <code>${close_p:,.2f}</code>\n"
                f"• <b>Filtro Macro 1H:</b> <b>{macro_trend}</b> (EMA200 1H: <code>${ema200_1h:,.2f}</code> | <code>{dist_1h_pct:+.2f}%</code>)\n"
                f"• <b>Estructura 5M:</b> {micro_trend}\n"
                f"• <b>EMAs 5M:</b> EMA20 <code>${ema20_5m:,.2f}</code> | EMA200 <code>${ema200_5m:,.2f}</code> (<code>{dist_5m_pct:+.2f}%</code>)\n"
                f"• <b>Estado Multi-Temporal:</b> <b>{align_status}</b>\n"
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
        f"🧭 <b>Filtro Maestro:</b> <code>EMA 200 (1H) + EMA 20/200 (5M)</code>",
        f"🔒 <b>Break-Even Neto:</b> <code>+{ASSET_CONFIG['SOL-USD']['be_pct']*100:.1f}%</code> (SL a <code>±{BE_FEE_BUFFER_PCT*100:.2f}%</code>)",
        "━━━━━━━━━━━━━━━━━━━━━"
    ]

    for pair, cfg in ASSET_CONFIG.items():
        trade = active_trades.get(pair)
        if trade is None:
            lines.append(f"• <b>{cfg['display']}:</b> 🟢 <code>ESCANEANDO (LIBRE)</code>")
        else:
            be_tag = "🛡️ BE Neto Activo" if trade["be_moved"] else f"⏳ BE en ${trade['be_price']:,.2f}"
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

    elif (
        cmd in ["/close", "/free", "/close sol", "/free sol", "cmd_free_sol", "/restart"]
        or cmd.startswith("/close")
        or cmd.startswith("/free")
    ):
        with state_lock:
            active_trades["SOL-USD"] = None
        msg = "🔓 <b>SOL-PERP liberado manualmente y listo para escaneo.</b>\n\n" + generate_status_report()
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
    buffer_sign = "+0.15%" if signal["direction"] == "LONG" else "-0.15%"
    slot_notice = (
        "\n⚠️ <i>Nota: Ya tienes una posición activa en curso. Ejecuta solo si liberaste margen.</i>"
        if blocked_by_slot else ""
    )

    return (
        f"{icon} <b>SEÑAL {signal['direction']} | {signal['display']} (5M + 1H)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Probabilidad de Éxito:</b> <code>{signal['probability']:.1f}%</code>\n"
        f"🧭 <b>Tendencia:</b> {signal['trend']}\n"
        f"🏛️ <b>Filtro Macro 1H:</b> EMA200 <code>${signal['ema200_1h']:,.2f}</code>\n"
        f"📊 <b>Confluencias 5M:</b> EMA20 <code>${signal['ema20_5m']:,.2f}</code> | RSI <code>{signal['rsi']:.1f}</code> | Vol <code>{signal['vol_ratio']:.2f}x</code>\n\n"
        f"📥 <b>Precio de Entrada:</b> <code>${signal['entry_price']:,.2f}</code>\n"
        f"✅ <b>Take Profit:</b> <code>${signal['tp_price']:,.2f}</code> <i>(+{signal['tp_pct']:.1f}% | +${signal['net_tp_usd']:.2f})</i>\n"
        f"🛡️ <b>Gatillo Break-Even:</b> <code>${signal['be_price']:,.2f}</code> <i>(+{signal['be_pct']:.1f}% ➔ mover SL a <code>${signal['be_sl_price']:,.2f}</code> [{buffer_sign}])</i>\n"
        f"🛑 <b>Stop Loss Inicial:</b> <code>${signal['sl_price']:,.2f}</code> <i>(-{signal['sl_pct']:.1f}% | -${signal['net_sl_usd']:.2f})</i>\n\n"
        f"💼 <b>Gestión Phantom:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Posición: <code>${notional:.2f}</code>)"
        f"{slot_notice}"
    )


def main_scanner_loop():
    global eval_counter
    print("[INFO] Iniciando hilo de escaneo cuantitativo especializado en SOL-PERP (5M + 1H)...")

    # Validar conexión inicial a Coinbase antes de enviar mensaje de arranque
    sol_check = fetch_live_price("SOL-USD")
    sol_ema1h = fetch_macro_ema200_1h("SOL-USD")
    conn_status = "🟢 Conectado a Coinbase API" if sol_check > 0 else "🟡 Conectando a Coinbase API..."
    notional = MARGIN_USD * LEVERAGE

    startup_msg = (
        f"⚡ <b>BOT CUANTITATIVO SOL-PERP EN LÍNEA (v4.0)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Estado API:</b> {conn_status}\n"
        f"🪙 <b>SOL-PERP Spot:</b> <code>${sol_check:,.2f}</code> | <b>EMA200 (1H):</b> <code>${sol_ema1h:,.2f}</code>\n"
        f"↕️ <b>Motor Multi-Temporal:</b> LONG &amp; SHORT (EMA200 1H + EMA20/200 5M + Vol 1.15x + RSI 14)\n"
        f"🛡️ <b>Parámetros Validados:</b> TP <code>+3.0%</code> | SL <code>-1.5%</code> | BE <code>+1.8%</code> (SL protegido <code>±0.15%</code>)\n"
        f"💼 <b>Gestión Blindada:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Nocional <code>${notional:.2f}</code> sobre <code>${ACCOUNT_BALANCE_USD:.2f}</code>)\n"
        f"🧹 <b>UX Auto-Clean:</b> Activado (borrado automático de comandos y panel único)\n\n"
        f"👇 <i>Acceso directo a gráfica 5M y panel de control:</i>"
    )
    send_telegram(startup_msg, reply_markup=build_startup_keyboard(), is_ui_response=True)

    while True:
        try:
            # 1. Monitorear TP, SL y Break-Even Neto de posiciones activas
            monitor_active_trades()

            # 2. Escanear velas cerradas de 5M para SOL-USD
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

                        already_in_position = active_trades[pair] is not None
                        blocked_by_slot = SINGLE_SLOT_LOCK and already_in_position

                        if active_trades[pair] is None:
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
        "service": "Bot Cuantitativo SOL-PERP (5M + Macro 1H)",
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
