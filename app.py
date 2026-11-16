import os
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from flask import Flask, jsonify

# ==============================================================================
# CONFIGURACIÓN DEL SERVIDOR FLASK (RENDER / KOYEB HEALTH CHECK)
# ==============================================================================
app = Flask(__name__)

# ==============================================================================
# VARIABLES DE ENTORNO Y CONFIGURACIÓN DE RIESGO CUANTITATIVA (SOL-PERP v5.0)
# ==============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parámetros de Cuenta y Gestión de Riesgo Blindada (Phantom / Jupiter Perps)
ACCOUNT_BALANCE_USD = float(os.getenv("ACCOUNT_BALANCE_USD", "8.10"))
MARGIN_USD = float(os.getenv("MARGIN_USD", "2.50"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))

# Fricciones reales de ejecución en DEX Perps (Ideas.md: Taker + Slippage + Funding)
TAKER_FEE_ROUNDTRIP_PCT = 0.0012  # ~0.06% apertura + ~0.06% cierre
EST_SLIPPAGE_PCT = 0.0005         # ~0.05% deslizamiento estimado en orden de mercado
BE_FEE_BUFFER_PCT = 0.0020        # Colchón +0.20% spot para cubrir fees + slippage en Break-Even
SINGLE_SLOT_LOCK = os.getenv("SINGLE_SLOT_LOCK", "true").lower() == "true"

# Umbrales Cuantitativos (Carver EWMAC + Box-Jenkins AR(1) + Filtro de Régimen ADX)
MAX_OVERBOUGHT_RSI = 68.0
MIN_OVERSOLD_RSI = 32.0
MAX_WICK_RATIO = 0.38             # Exige velas de intención con poca mecha contraria
MIN_BODY_DELTA_RATIO = 0.52       # Order Flow Proxy: cuerpo >= 52% del rango total de la vela
MIN_ADX_TREND = 22.0              # Filtro de Régimen: bloquea mercados laterales (ADX < 22)
MIN_CARVER_FORECAST = 5.0         # Umbral EWMAC escalado [-20, +20] de Robert Carver
MACRO_CACHE_TTL_SECONDS = 900     # Caché de 15 minutos (900s) para datos Macro de 1 Hora

ASSET_CONFIG = {
    "SOL-USD": {
        "display": "SOL-PERP",
        "tp_pct": 0.035,          # Take Profit Base: +3.5% spot (ajustable por ATR)
        "sl_pct": 0.016,          # Stop Loss Base:   -1.6% spot (ajustable por ATR, R:B ~1:2.2)
        "be_pct": 0.020,          # Break-Even Base:  +2.0% spot (deja respirar el pullback)
        "min_sl_pct": 0.014,      # Límite inferior SL dinámico (1.4%)
        "max_sl_pct": 0.020,      # Límite superior SL dinámico (2.0%)
        "rr_ratio": 2.20,         # Ratio Riesgo/Beneficio Cuantitativo (1:2.2)
        "vol_factor": 1.25,       # Absorción / Volumen institucional mínimo vs MA20
        "tv_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3ASOLUSD&interval=5"
    }
}

# ==============================================================================
# ESTADO GLOBAL DEL SISTEMA Y RESILIENCIA 24/7 (PROTEGIDO CON LOCK CONCURRENTE)
# ==============================================================================
state_lock = threading.Lock()
scanner_started = False
scanner_thread_obj = None
boot_time = datetime.now(timezone.utc)
last_heartbeat_ts = time.time()

active_trades = {pair: None for pair in ASSET_CONFIG.keys()}
last_processed_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}
last_alerted_timestamps = {pair: 0 for pair in ASSET_CONFIG.keys()}
macro_1h_cache = {
    pair: {
        "ema_200": 0.0,
        "ema_50": 0.0,
        "adx_1h": 0.0,
        "ar1_autocorr": 0.0,
        "updated_at": 0.0
    }
    for pair in ASSET_CONFIG.keys()
}

eval_counter = 0
api_error_counter = 0
telegram_update_offset = 0
last_bot_ui_message_id = None  # ID del último mensaje de UI (status/telemetría) para Auto-Clean


def create_resilient_http_session() -> requests.Session:
    """
    Crea una sesión HTTP industrial con Retry y Exponential Backoff
    para soportar micro-cortes de red, límites 429 y errores 5xx en Render/Koyeb.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "QuantSolPerpBot/5.0-CarverBoxJenkins"})
    return session


HTTP_SESSION = create_resilient_http_session()


# ==============================================================================
# UTILIDADES DE FORMATO Y TECLADOS INTERACTIVOS (INLINE KEYBOARDS INTACTOS)
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
        else:
            print(f"[ERROR] Telegram API ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"[ERROR] Fallo al enviar mensaje a Telegram: {e}")
    return None


# ==============================================================================
# CONEXIÓN RESILIENTE CON COINBASE API (5M, MACRO 1H CON CACHÉ Y LIVE TICKER)
# ==============================================================================
def fetch_candles_5m(pair: str) -> pd.DataFrame:
    """Descarga hasta 300 velas históricas de 5 minutos desde Coinbase con manejo de fallos."""
    global api_error_counter
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
                df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                return df
    except Exception as e:
        api_error_counter += 1
        print(f"[ERROR] Descargando velas 5m de {pair}: {e}")
    return None


def compute_adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calcula el Average Directional Index (ADX) suavizado de Wilder para filtrar rangos."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0.0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100.0 * (pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100.0 * (pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))

    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean().fillna(20.0)
    return adx


def fetch_macro_context_1h(pair: str) -> dict:
    """
    Consulta el endpoint de 1H (granularity=3600) con caché de 15 minutos y calcula:
    1. EMA 200 estructural de 1H y EMA 50 direccional de 1H.
    2. ADX(14) de 1H para detectar si el mercado macro está en tendencia o rango lateral.
    3. Autocorrelación AR(1) de Box-Jenkins sobre retornos logarítmicos estacionarios (\lambda=0).
    """
    global api_error_counter
    now_ts = time.time()
    cached = macro_1h_cache.get(pair)

    if cached and cached["ema_200"] > 0 and (now_ts - cached["updated_at"]) < MACRO_CACHE_TTL_SECONDS:
        return cached

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
                df_1h = df_1h.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                for col in ["open", "high", "low", "close", "volume"]:
                    df_1h[col] = df_1h[col].astype(float)

                ema200_1h = float(df_1h["close"].ewm(span=200, adjust=False).mean().iloc[-1])
                ema50_1h = float(df_1h["close"].ewm(span=50, adjust=False).mean().iloc[-1])
                adx_1h = float(compute_adx_series(df_1h, period=14).iloc[-1])

                # Box-Jenkins: Transformación logarítmica para estabilizar varianza y 1ra diferencia (d=1)
                log_ret = np.log(df_1h["close"] / df_1h["close"].shift(1)).dropna()
                ar1_corr = float(log_ret.tail(48).autocorr(lag=1)) if len(log_ret) >= 48 else 0.0
                if pd.isna(ar1_corr):
                    ar1_corr = 0.0

                macro_data = {
                    "ema_200": ema200_1h,
                    "ema_50": ema50_1h,
                    "adx_1h": adx_1h,
                    "ar1_autocorr": ar1_corr,
                    "updated_at": now_ts
                }
                macro_1h_cache[pair] = macro_data
                return macro_data
    except Exception as e:
        api_error_counter += 1
        print(f"[ERROR] Descargando contexto macro 1H de {pair}: {e}")

    return cached if cached else {"ema_200": 0.0, "ema_50": 0.0, "adx_1h": 0.0, "ar1_autocorr": 0.0, "updated_at": 0.0}


def fetch_macro_ema200_1h(pair: str) -> float:
    """Wrapper compatible que devuelve la EMA 200 de 1H desde el motor de contexto macro."""
    return fetch_macro_context_1h(pair).get("ema_200", 0.0)


def fetch_live_price(pair: str) -> float:
    """Obtiene el último precio spot en tiempo real desde Coinbase."""
    global api_error_counter
    url = f"https://api.exchange.coinbase.com/products/{pair}/ticker"
    try:
        resp = HTTP_SESSION.get(url, timeout=6)
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception:
        api_error_counter += 1
    return 0.0


# ==============================================================================
# MOTOR CUANTITATIVO: CARVER EWMAC + BOX-JENKINS + VOLATILITY TARGETING
# ==============================================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula la suite completa de indicadores cuantitativos en 5M:
    - EMA 200, EMA 64, EMA 20 y EMA 16.
    - ATR (14) de Wilder para Volatility Targeting (Robert Carver).
    - Forecast EWMAC(16, 64) normalizado por volatilidad y acotado a [-20, +20].
    - ADX (14) intradía para filtro de régimen tendencial.
    - RSI (14) de Wilder y Media Móvil Institucional de Volumen (MA20).
    - Autocorrelación AR(1) local de Box-Jenkins sobre serie diferenciada.
    """
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # Medias Exponenciales Estructurales y Tácticas
    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema_64"] = df["close"].ewm(span=64, adjust=False).mean()
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema_16"] = df["close"].ewm(span=16, adjust=False).mean()

    # True Range y ATR (14) para normalización de volatilidad
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr_14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    # Forecast EWMAC(16, 64) de Robert Carver escalado a [-20, +20]
    raw_ewmac = (df["ema_16"] - df["ema_64"]) / df["atr_14"].replace(0, np.nan)
    df["carver_forecast"] = (raw_ewmac * 8.5).clip(lower=-20.0, upper=20.0).fillna(0.0)

    # ADX (14) intradía para confirmar expansión de rango
    df["adx_14"] = compute_adx_series(df, period=14)

    # RSI (14) suavizado exponencial (Wilder)
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -1.0 * delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)

    # Media Móvil Simple de Volumen (20 periodos) y Canal Donchian previo (12 velas = 1 hora)
    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    df["donchian_high_12"] = df["high"].shift(1).rolling(window=12).max()
    df["donchian_low_12"] = df["low"].shift(1).rolling(window=12).min()

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
    adverse_wick_ratio: float,
    carver_forecast: float = 0.0,
    adx_val: float = 25.0
) -> float:
    """
    Calcula la probabilidad estimada de éxito (60.0% - 94.0%) integrando:
    1. Alineación multi-temporal 1H + 5M y fuerza del Forecast EWMAC de Carver.
    2. Régimen de tendencia ADX (penaliza lateralidad, premia expansión).
    3. Absorción de flujo de órdenes (Volumen relativo vs MA20).
    4. Zona óptima de momento RSI y limpieza de mecha (Order Flow Delta).
    """
    score = 55.0

    # 1. Fuerza EWMAC de Robert Carver y alineación multi-temporal
    forecast_strength = min(10.0, (abs(carver_forecast) / 20.0) * 10.0)
    score += forecast_strength

    if direction == "LONG" and (close_p > ema200_1h) and (close_p > ema_20 > ema_200):
        score += 6.0
    elif direction == "SHORT" and (close_p < ema200_1h) and (close_p < ema_20 < ema_200):
        score += 6.0

    # 2. Intensidad del Régimen Tendencial (ADX)
    if adx_val >= 28.0:
        score += 6.0
    elif adx_val >= MIN_ADX_TREND:
        score += 3.0

    # 3. Volumen Institucional Relativo
    vol_surplus = max(0.0, vol_ratio - vol_factor)
    score += min(10.0, vol_surplus * 9.0)

    # 4. Calidad de la Zona RSI y Limpieza de Vela
    if direction == "LONG" and (52.0 <= rsi <= 64.0):
        score += 5.0
    elif direction == "SHORT" and (36.0 <= rsi <= 48.0):
        score += 5.0

    wick_quality = max(0.0, (MAX_WICK_RATIO - adverse_wick_ratio) / MAX_WICK_RATIO)
    score += wick_quality * 5.0

    return round(float(np.clip(score, 60.0, 94.0)), 1)


def evaluate_market_signal(pair: str, df: pd.DataFrame) -> dict:
    """
    Evalúa la última vela cerrada de 5M (iloc[-2]) bajo la arquitectura v5.0:
    1. Filtro Macro 1H (EMA 200 1H + EMA 50 1H + Persistencia Box-Jenkins).
    2. Filtro de Régimen Anti-Rango: ADX(14) >= 22.0 (en 5M o 1H) y Carver EWMAC >= |5.0|.
    3. Microestructura / Order Flow Proxy: Cuerpo dominante (>= 52% del rango),
       baja mecha contraria (<= 38%) y volumen institucional (>= 1.25x MA20).
    4. Stops y Take Profit ajustados dinámicamente por volatilidad ATR (Ratio 1:2.2).
    """
    macro_ctx = fetch_macro_context_1h(pair)
    ema200_1h = macro_ctx["ema_200"]
    ema50_1h = macro_ctx["ema_50"]
    adx_1h = macro_ctx["adx_1h"]
    ar1_1h = macro_ctx["ar1_autocorr"]

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
    c_atr = float(closed["atr_14"]) if pd.notna(closed["atr_14"]) else (c_close * 0.006)
    c_forecast = float(closed["carver_forecast"])
    c_adx = float(closed["adx_14"])

    if c_vol_ma <= 0:
        return None

    # 1. Filtro de Volumen Institucional (Absorción / Participación)
    vol_ratio = c_vol / c_vol_ma
    if vol_ratio < cfg["vol_factor"]:
        return None

    # 2. Filtro de Régimen Anti-Lateralidad (Ideas.md #5: evita operar en chop)
    effective_adx = max(c_adx, adx_1h)
    if effective_adx < MIN_ADX_TREND:
        return None

    # Filtro Box-Jenkins: Evitar operar tendencias cuando hay fuerte autocorrelación negativa (reversión)
    if ar1_1h < -0.22:
        return None

    candle_range = c_high - c_low
    if candle_range <= 0:
        return None

    body_size = abs(c_close - c_open)
    body_delta_ratio = body_size / candle_range
    if body_delta_ratio < MIN_BODY_DELTA_RATIO:
        return None

    upper_wick = c_high - max(c_open, c_close)
    lower_wick = min(c_open, c_close) - c_low
    upper_wick_ratio = upper_wick / candle_range
    lower_wick_ratio = lower_wick / candle_range

    # 3. Dimensionamiento Dinámico por Volatilidad (Carver ATR Stop acotado a límites seguros)
    raw_sl_pct = (2.2 * c_atr) / c_close
    dynamic_sl_pct = float(np.clip(raw_sl_pct, cfg["min_sl_pct"], cfg["max_sl_pct"]))
    dynamic_tp_pct = round(dynamic_sl_pct * cfg["rr_ratio"], 4)
    dynamic_be_pct = round(dynamic_tp_pct * 0.58, 4)  # Gatillo BE al 58% del recorrido al TP

    notional_usd = MARGIN_USD * LEVERAGE
    est_friction_usd = notional_usd * (TAKER_FEE_ROUNDTRIP_PCT + EST_SLIPPAGE_PCT)

    # ------------------------------------------------------------------
    # ESCENARIO 1: SEÑAL LONG CUANTITATIVA
    # Régimen Macro 1H Alcista + EWMAC Carver >= +5.0 + Impulso RSI/Order Flow
    # ------------------------------------------------------------------
    macro_bullish = (c_close > ema200_1h) and (ema50_1h >= ema200_1h * 0.995)
    micro_bullish = (c_close > c_ema20 > c_ema200) and (c_forecast >= MIN_CARVER_FORECAST)

    if macro_bullish and micro_bullish and (c_close > c_open):
        if upper_wick_ratio <= MAX_WICK_RATIO:
            rsi_impulse = ((p_rsi < 52.0 <= c_rsi) or (p_rsi < 56.0 <= c_rsi)) and (c_rsi <= MAX_OVERBOUGHT_RSI)
            structural_break = pd.notna(closed["donchian_high_12"]) and (c_close >= float(closed["donchian_high_12"]) * 0.9985)

            if rsi_impulse and structural_break:
                tp_price = c_close * (1.0 + dynamic_tp_pct)
                sl_price = c_close * (1.0 - dynamic_sl_pct)
                be_price = c_close * (1.0 + dynamic_be_pct)
                be_sl_price = c_close * (1.0 + BE_FEE_BUFFER_PCT)  # Entrada + 0.20% (Cubre Fees + Slippage)

                prob = compute_confluence_probability(
                    "LONG", c_close, ema200_1h, c_ema200, c_ema20, c_rsi,
                    vol_ratio, cfg["vol_factor"], upper_wick_ratio, c_forecast, effective_adx
                )

                net_tp_usd = (notional_usd * dynamic_tp_pct) - est_friction_usd
                net_sl_usd = (notional_usd * dynamic_sl_pct) + est_friction_usd

                return {
                    "pair": pair,
                    "display": cfg["display"],
                    "direction": "LONG",
                    "trend": f"🟢 Alcista 1H+5M (EWMAC {c_forecast:+.1f} | ADX {effective_adx:.0f})",
                    "entry_price": c_close,
                    "ema200_1h": ema200_1h,
                    "ema200_5m": c_ema200,
                    "ema20_5m": c_ema20,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "be_price": be_price,
                    "be_sl_price": be_sl_price,
                    "tp_pct": dynamic_tp_pct * 100.0,
                    "sl_pct": dynamic_sl_pct * 100.0,
                    "be_pct": dynamic_be_pct * 100.0,
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
    # ESCENARIO 2: SEÑAL SHORT CUANTITATIVA
    # Régimen Macro 1H Bajista + EWMAC Carver <= -5.0 + Impulso RSI/Order Flow
    # ------------------------------------------------------------------
    macro_bearish = (c_close < ema200_1h) and (ema50_1h <= ema200_1h * 1.005)
    micro_bearish = (c_close < c_ema20 < c_ema200) and (c_forecast <= -MIN_CARVER_FORECAST)

    if macro_bearish and micro_bearish and (c_close < c_open):
        if lower_wick_ratio <= MAX_WICK_RATIO:
            rsi_breakdown = ((p_rsi > 48.0 >= c_rsi) or (p_rsi > 44.0 >= c_rsi)) and (c_rsi >= MIN_OVERSOLD_RSI)
            structural_break = pd.notna(closed["donchian_low_12"]) and (c_close <= float(closed["donchian_low_12"]) * 1.0015)

            if rsi_breakdown and structural_break:
                tp_price = c_close * (1.0 - dynamic_tp_pct)
                sl_price = c_close * (1.0 + dynamic_sl_pct)
                be_price = c_close * (1.0 - dynamic_be_pct)
                be_sl_price = c_close * (1.0 - BE_FEE_BUFFER_PCT)  # Entrada - 0.20% (Cubre Fees + Slippage)

                prob = compute_confluence_probability(
                    "SHORT", c_close, ema200_1h, c_ema200, c_ema20, c_rsi,
                    vol_ratio, cfg["vol_factor"], lower_wick_ratio, c_forecast, effective_adx
                )

                net_tp_usd = (notional_usd * dynamic_tp_pct) - est_friction_usd
                net_sl_usd = (notional_usd * dynamic_sl_pct) + est_friction_usd

                return {
                    "pair": pair,
                    "display": cfg["display"],
                    "direction": "SHORT",
                    "trend": f"🔴 Bajista 1H+5M (EWMAC {c_forecast:+.1f} | ADX {effective_adx:.0f})",
                    "entry_price": c_close,
                    "ema200_1h": ema200_1h,
                    "ema200_5m": c_ema200,
                    "ema20_5m": c_ema20,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "be_price": be_price,
                    "be_sl_price": be_sl_price,
                    "tp_pct": dynamic_tp_pct * 100.0,
                    "sl_pct": dynamic_sl_pct * 100.0,
                    "be_pct": dynamic_be_pct * 100.0,
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

        # 1. Alerta de Take Profit
        if tp_hit:
            send_telegram(
                f"✅ <b>TAKE PROFIT ALCANZADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Precio de Cierre:</b> <code>${live_price:,.2f}</code> <i>(+{trade['tp_pct']:.2f}%)</i>\n"
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
                    f"comisiones y slippage cubiertos)."
                )
                sl_pct_label = f"+{trade['be_buffer_pct']:.2f}% BE Neto"
            else:
                reason_note = f"Pérdida controlada (-${trade['net_sl_usd']:.2f} USD)."
                sl_pct_label = f"-{trade['sl_pct']:.2f}%"

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

        # 3. Alerta de Activación de Break-Even Neto Real (SL a entrada ± 0.20%)
        if not trade["be_moved"] and be_hit:
            with state_lock:
                trade["be_moved"] = True
                if direction == "LONG":
                    trade["sl_price"] = trade["entry_price"] * (1.0 + BE_FEE_BUFFER_PCT)
                    buffer_sign = "+0.20%"
                else:
                    trade["sl_price"] = trade["entry_price"] * (1.0 - BE_FEE_BUFFER_PCT)
                    buffer_sign = "-0.20%"
                protected_sl = trade["sl_price"]

            send_telegram(
                f"🛡️ <b>BREAK-EVEN NETO ACTIVADO | {trade['display']} ({direction})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>Precio Actual:</b> <code>${live_price:,.2f}</code> <i>(+{trade['be_pct']:.2f}%)</i>\n"
                f"📥 <b>Precio de Entrada:</b> <code>${trade['entry_price']:,.2f}</code>\n"
                f"🔒 <b>Nuevo SL Protegido ({buffer_sign}):</b> <code>${protected_sl:,.2f}</code>\n"
                f"🎯 <b>Rumbo a TP:</b> <code>${trade['tp_price']:,.2f}</code>\n\n"
                f"⚡ <b>ACCIÓN EN PHANTOM:</b> Mueve inmediatamente tu Stop Loss a <code>${protected_sl:,.2f}</code> "
                f"(<code>entrada {buffer_sign}</code>) para absorber comisiones Taker y slippage con riesgo cero."
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
        macro_ctx = fetch_macro_context_1h(pair)
        ema200_1h = macro_ctx["ema_200"]
        adx_1h = macro_ctx["adx_1h"]
        ar1_1h = macro_ctx["ar1_autocorr"]

        if df is not None and len(df) >= 200:
            df = calculate_indicators(df)
            last = df.iloc[-1]
            close_p = float(last["close"])
            ema200_5m = float(last["ema_200"])
            ema20_5m = float(last["ema_20"])
            rsi_val = float(last["rsi"])
            adx_5m = float(last["adx_14"])
            carver_fc = float(last["carver_forecast"])
            atr_val = float(last["atr_14"])
            vol_ma = float(last["vol_ma20"]) if pd.notna(last["vol_ma20"]) else 0.0
            vol_ratio = float(last["volume"]) / vol_ma if vol_ma > 0 else 0.0

            dist_5m_pct = ((close_p - ema200_5m) / ema200_5m) * 100.0 if ema200_5m > 0 else 0.0
            dist_1h_pct = ((close_p - ema200_1h) / ema200_1h) * 100.0 if ema200_1h > 0 else 0.0
            effective_adx = max(adx_5m, adx_1h)

            macro_trend = "🟢 Alcista" if (ema200_1h > 0 and close_p > ema200_1h) else "🔴 Bajista"
            regime_state = (
                f"🔥 Tendencial (ADX {effective_adx:.1f})"
                if effective_adx >= MIN_ADX_TREND
                else f"🛑 Rango / Chop Bloqueado (ADX {effective_adx:.1f} &lt; {MIN_ADX_TREND:.0f})"
            )

            if effective_adx >= MIN_ADX_TREND and ema200_1h > 0:
                if (close_p > ema200_1h) and (close_p > ema20_5m > ema200_5m) and (carver_fc >= MIN_CARVER_FORECAST):
                    align_status = "🟢 Alineación Estructural LONG"
                elif (close_p < ema200_1h) and (close_p < ema20_5m < ema200_5m) and (carver_fc <= -MIN_CARVER_FORECAST):
                    align_status = "🔴 Alineación Estructural SHORT"
                else:
                    align_status = "⏳ Esperando Confluencia EWMAC/EMA"
            else:
                align_status = "🛡️ Bloqueo Preventivo Anti-Chop"

            rsi_state = (
                "🔥 Sobrecompra" if rsi_val >= MAX_OVERBOUGHT_RSI
                else ("❄️ Sobreventa" if rsi_val <= MIN_OVERSOLD_RSI else "⚖️ Neutral")
            )

            report += (
                f"🪙 <b>{cfg['display']}</b>\n"
                f"• <b>Spot Actual:</b> <code>${close_p:,.2f}</code> (ATR 5M: <code>${atr_val:.2f}</code>)\n"
                f"• <b>Filtro Macro 1H:</b> <b>{macro_trend}</b> (EMA200: <code>${ema200_1h:,.2f}</code> | <code>{dist_1h_pct:+.2f}%</code>)\n"
                f"• <b>Régimen de Mercado:</b> {regime_state}\n"
                f"• <b>Carver EWMAC Forecast:</b> <code>{carver_fc:+.1f}</code> / 20.0 (AR1: <code>{ar1_1h:+.2f}</code>)\n"
                f"• <b>EMAs 5M:</b> EMA20 <code>${ema20_5m:,.2f}</code> | EMA200 <code>${ema200_5m:,.2f}</code> (<code>{dist_5m_pct:+.2f}%</code>)\n"
                f"• <b>Estado del Motor:</b> <b>{align_status}</b>\n"
                f"• <b>Volumen Institucional:</b> <code>{vol_ratio:.2f}x</code> (Umbral: <code>{cfg['vol_factor']:.2f}x</code>)\n"
                f"• <b>RSI (14):</b> <code>{rsi_val:.1f}</code> ({rsi_state})\n\n"
            )
        else:
            report += f"🪙 <b>{cfg['display']}</b>: <i>Sincronizando datos con Coinbase...</i>\n\n"

    report += f"⏱️ <i>Actualizado: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</i>"
    return report


def generate_status_report() -> str:
    notional = MARGIN_USD * LEVERAGE
    lines = [
        "🟢 <b>ESTADO DEL SISTEMA CUANTITATIVO (v5.0)</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"⏱️ <b>Tiempo Activo:</b> <code>{get_uptime_str()}</code>",
        f"🕯️ <b>Velas 5M Evaluadas:</b> <code>{eval_counter}</code>",
        f"💼 <b>Balance Base:</b> <code>${ACCOUNT_BALANCE_USD:.2f} USD</code>",
        f"🛡️ <b>Sizing Operativo:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Nocional: <code>${notional:.2f}</code>)",
        f"🧭 <b>Arquitectura:</b> <code>Carver EWMAC + ADX>={MIN_ADX_TREND:.0f} + Box-Jenkins</code>",
        f"🔒 <b>Break-Even Neto:</b> <code>Dinámico ATR</code> (SL a <code>±{BE_FEE_BUFFER_PCT*100:.2f}%</code>)",
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
                    delete_telegram_message(msg_id)
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
def format_signal_card(signal: str, blocked_by_slot: bool = False) -> str:
    icon = "🟢" if signal["direction"] == "LONG" else "🔴"
    notional = MARGIN_USD * LEVERAGE
    buffer_sign = "+0.20%" if signal["direction"] == "LONG" else "-0.20%"
    slot_notice = (
        "\n⚠️ <i>Nota: Ya tienes una posición activa en curso. Ejecuta solo si liberaste margen.</i>"
        if blocked_by_slot else ""
    )

    return (
        f"{icon} <b>SEÑAL {signal['direction']} | {signal['display']} (v5.0 QUANT)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Probabilidad Cuantitativa:</b> <code>{signal['probability']:.1f}%</code>\n"
        f"🧭 <b>Régimen:</b> {signal['trend']}\n"
        f"🏛️ <b>Filtro Macro 1H:</b> EMA200 <code>${signal['ema200_1h']:,.2f}</code>\n"
        f"📊 <b>Confluencias 5M:</b> EMA20 <code>${signal['ema20_5m']:,.2f}</code> | RSI <code>{signal['rsi']:.1f}</code> | Vol <code>{signal['vol_ratio']:.2f}x</code>\n\n"
        f"📥 <b>Precio de Entrada:</b> <code>${signal['entry_price']:,.2f}</code>\n"
        f"✅ <b>Take Profit (R:B 1:2.2):</b> <code>${signal['tp_price']:,.2f}</code> <i>(+{signal['tp_pct']:.2f}% | +${signal['net_tp_usd']:.2f})</i>\n"
        f"🛡️ <b>Gatillo Break-Even:</b> <code>${signal['be_price']:,.2f}</code> <i>(+{signal['be_pct']:.2f}% ➔ mover SL a <code>${signal['be_sl_price']:,.2f}</code> [{buffer_sign}])</i>\n"
        f"🛑 <b>Stop Loss (ATR Dinámico):</b> <code>${signal['sl_price']:,.2f}</code> <i>(-{signal['sl_pct']:.2f}% | -${signal['net_sl_usd']:.2f})</i>\n\n"
        f"💼 <b>Gestión Phantom:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Posición: <code>${notional:.2f}</code>)"
        f"{slot_notice}"
    )


def main_scanner_loop():
    global eval_counter, last_heartbeat_ts
    print("[INFO] Iniciando motor cuantitativo v5.0 (Carver EWMAC + Box-Jenkins + ADX Regime)...")

    sol_check = fetch_live_price("SOL-USD")
    sol_ema1h = fetch_macro_ema200_1h("SOL-USD")
    conn_status = "🟢 Conectado a Coinbase API" if sol_check > 0 else "🟡 Conectando a Coinbase API..."
    notional = MARGIN_USD * LEVERAGE

    startup_msg = (
        f"⚡ <b>BOT CUANTITATIVO SOL-PERP EN LÍNEA (v5.0)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Estado API:</b> {conn_status}\n"
        f"🪙 <b>SOL-PERP Spot:</b> <code>${sol_check:,.2f}</code> | <b>EMA200 (1H):</b> <code>${sol_ema1h:,.2f}</code>\n"
        f"🧠 <b>Motor Cuantitativo:</b> Carver EWMAC + Filtro Régimen ADX &ge; {MIN_ADX_TREND:.0f} + Box-Jenkins AR(1)\n"
        f"🛡️ <b>Riesgo Dinámico (ATR):</b> R:B <code>1:2.2</code> | SL <code>1.4%-2.0%</code> | BE Neto <code>±0.20%</code>\n"
        f"💼 <b>Gestión Blindada:</b> <code>${MARGIN_USD:.2f} USDC</code> a <code>{LEVERAGE}x</code> (Nocional <code>${notional:.2f}</code> sobre <code>${ACCOUNT_BALANCE_USD:.2f}</code>)\n"
        f"🧹 <b>UX Auto-Clean:</b> Activado (borrado automático de comandos y panel único)\n\n"
        f"👇 <i>Acceso directo a gráfica 5M y panel de control:</i>"
    )
    send_telegram(startup_msg, reply_markup=build_startup_keyboard(), is_ui_response=True)

    while True:
        try:
            last_heartbeat_ts = time.time()

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
            print(f"[ERROR] Excepción recuperada en main_scanner_loop: {e}")

        time.sleep(3)


# ==============================================================================
# ARRANQUE SEGURO Y WATCHDOG BAJO GUNICORN / EJECUCIÓN DIRECTA
# ==============================================================================
def start_background_scanner():
    """
    Garantiza que el hilo del escáner inicie bajo Gunicorn en Render/Koyeb
    y pueda ser auto-reiniciado por el Watchdog si llegara a caer.
    """
    global scanner_started, scanner_thread_obj
    with state_lock:
        if not scanner_started or (scanner_thread_obj is not None and not scanner_thread_obj.is_alive()):
            scanner_started = True
            scanner_thread_obj = threading.Thread(target=main_scanner_loop, daemon=True)
            scanner_thread_obj.start()


@app.route("/")
def health_check():
    # Auto-verificación Watchdog en cada ping de Render/Koyeb
    if scanner_thread_obj is None or not scanner_thread_obj.is_alive():
        start_background_scanner()

    return jsonify({
        "status": "online",
        "service": "Bot Cuantitativo SOL-PERP v5.0 (Carver EWMAC + Box-Jenkins + ADX Regime)",
        "uptime": get_uptime_str(),
        "candles_evaluated": eval_counter,
        "api_errors_recovered": api_error_counter,
        "last_heartbeat_sec_ago": round(time.time() - last_heartbeat_ts, 1),
        "active_positions": {
            k: (v["direction"] if v else None) for k, v in active_trades.items()
        }
    }), 200


# Arrancar hilo al cargar el módulo en Gunicorn
start_background_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
