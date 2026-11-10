import os
import time
import logging
import requests
import pandas as pd
import numpy as np

# Configuración de Logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("SolBotHF")

# --- CONFIGURACIÓN DE PARÁMETROS ---
SYMBOL = "SOLUSDT"
INTERVAL = "5m"
LEVERAGE = 4
MARGIN_USDC = 2.00
NOTIONAL_SIZE = MARGIN_USDC * LEVERAGE  # $8.00 USD

# Gestión de Riesgo
TP_PCT = 0.03   # Take Profit: +3.0%
SL_PCT = 0.015  # Stop Loss: -1.5%
BE_TRIGGER = 0.012  # Activación de Break-Even: +1.2%

# Filtros Anti-Ruido
RSI_PERIOD = 14
RSI_SHORT_THRESHOLD = 60  # Cruce a la baja desde sobrecompra
RSI_LONG_THRESHOLD = 40   # Cruce al alza desde sobreventa
VOL_FACTOR = 0.8          # Volumen mínimo: vol > vol_avg * 0.8

# Credenciales de Telegram (Variables de entorno recomendadas en Render)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TU_CHAT_ID")

# Estado interno de la posición simulada/activa
current_position = {
    "active": False,
    "side": None,       # "LONG" o "SHORT"
    "entry_price": 0.0,
    "tp_price": 0.0,
    "sl_price": 0.0,
    "be_activated": False
}


def send_telegram_message(text: str):
    """Envía alertas en tiempo real al canal de Telegram (@alxxcxstbot)."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "TU_TELEGRAM_BOT_TOKEN":
        logger.warning("Token de Telegram no configurado. Alerta omitida.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code != 200:
            logger.error(f"Error al enviar Telegram: {response.text}")
    except Exception as e:
        logger.error(f"Excepción en Telegram API: {e}")


def fetch_binance_klines(symbol: str, interval: str, limit: int = 250) -> pd.DataFrame:
    """Obtiene velas recientes de Binance para calcular indicadores."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except Exception as e:
        logger.error(f"Error al conectar con Binance API: {e}")
        return pd.DataFrame()


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula EMA 200, RSI de 14 periodos y la media de volumen."""
    # EMA 200
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # RSI 14
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # Volumen promedio (20 periodos)
    df['vol_avg'] = df['volume'].rolling(window=20).mean()
    
    return df


def evaluate_market():
    """Ejecuta la lógica de la estrategia en cada cierre de vela de 5 minutos."""
    global current_position

    df = fetch_binance_klines(SYMBOL, INTERVAL, limit=250)
    if df.empty or len(df) < 205:
        logger.info("Datos insuficientes recibidos de Binance. Esperando siguiente ciclo...")
        return

    df = calculate_indicators(df)
    
    # Tomar la penúltima vela (vela cerrada) y la anterior para cruces
    prev = df.iloc[-2]
    prev_prev = df.iloc[-3]
    
    price = prev['close']
    ema_200 = prev['ema_200']
    rsi_current = prev['rsi']
    rsi_previous = prev_prev['rsi']
    vol_current = prev['volume']
    vol_avg = prev['vol_avg']

    logger.info(f"Evaluando {SYMBOL} | Precio: {price:.2f} | EMA200: {ema_200:.2f} | RSI: {rsi_current:.2f} | Vol: {vol_current:.2f}")

    # --- GESTIÓN DE POSICIÓN ACTIVA (Monitoreo de TP, SL y Break-Even) ---
    if current_position["active"]:
        side = current_position["side"]
        entry = current_position["entry_price"]
        
        # Calcular rendimiento actual
        if side == "LONG":
            pnl_pct = (price - entry) / entry * LEVERAGE
            # Activar Break-Even automático si alcanza el objetivo rápido
            if not current_position["be_activated"] and (price - entry) / entry >= BE_TRIGGER:
                current_position["sl_price"] = entry
                current_position["be_activated"] = True
                msg = f"🛡️ *BREAK-EVEN BLINDADO (LONG)*\nPrecio alcanzó +{BE_TRIGGER*100}%. Stop Loss movido a entrada: `${entry:.2f}`"
                send_telegram_message(msg)
                logger.info(msg)
            
            # Verificar TP o SL
            if price >= current_position["tp_price"] or price <= current_position["sl_price"]:
                result_type = "TAKE PROFIT 🎯" if price >= current_position["tp_price"] else "STOP LOSS 🛑"
                msg = f"✅ *CIERRE DE POSICIÓN {side}*\nResultado: {result_type}\nPrecio de Cierre: `${price:.2f}`"
                send_telegram_message(msg)
                current_position["active"] = False
                
        elif side == "SHORT":
            pnl_pct = (entry - price) / entry * LEVERAGE
            # Activar Break-Even automático
            if not current_position["be_activated"] and (entry - price) / entry >= BE_TRIGGER:
                current_position["sl_price"] = entry
                current_position["be_activated"] = True
                msg = f"🛡️ *BREAK-EVEN BLINDADO (SHORT)*\nPrecio alcanzó +{BE_TRIGGER*100}%. Stop Loss movido a entrada: `${entry:.2f}`"
                send_telegram_message(msg)
                logger.info(msg)
                
            # Verificar TP o SL
            if price <= current_position["tp_price"] or price >= current_position["sl_price"]:
                result_type = "TAKE PROFIT 🎯" if price <= current_position["tp_price"] else "STOP LOSS 🛑"
                msg = f"✅ *CIERRE DE POSICIÓN {side}*\nResultado: {result_type}\nPrecio de Cierre: `${price:.2f}`"
                send_telegram_message(msg)
                current_position["active"] = False
        return

    # --- FILTROS ANTI-RUIDO Y APERTURA DE NUEVAS SEÑALES ---
    # Capa 3: Filtro de Volumen flexible pero seguro
    if vol_current < (vol_avg * VOL_FACTOR):
        logger.info("Filtro de volumen bloqueó la entrada (liquidez baja/ruido).")
        return

    # Capa 1 y Capa 2: Señal SHORT
    # Tendencia bajista (Precio < EMA 200) y RSI cruzando a la baja desde zona de sobrecompra (> 60)
    if price < ema_200 and rsi_previous >= RSI_SHORT_THRESHOLD and rsi_current < RSI_SHORT_THRESHOLD:
        entry = price
        tp = entry * (1 - TP_PCT)
        sl = entry * (1 + SL_PCT)
        
        current_position = {
            "active": True,
            "side": "SHORT",
            "entry_price": entry,
            "tp_price": tp,
            "sl_price": sl,
            "be_activated": False
        }
        
        msg = (
            f"🚨 *NUEVA SEÑAL SHORT (5M)* 🚨\n\n"
            f"🪙 Par: `{SYMBOL}`\n"
            f"📉 Entrada: `${entry:.2f}`\n"
            f"💰 Margen: `${MARGIN_USDC} USDC` ({LEVERAGE}x | Nocional: `${NOTIONAL_SIZE}`)\n"
            f"🎯 Take Profit (+3.0%): `${tp:.2f}`\n"
            f"🛑 Stop Loss (-1.5%): `${sl:.2f}`\n"
            f"📊 Indicadores -> EMA200: `${ema_200:.2f}` | RSI: `{rsi_current:.1f}`\n"
            f"🛡️ *Break-Even Automático:* Activo (Disparo al +{BE_TRIGGER*100}%)"
        )
        send_telegram_message(msg)
        logger.info("Señal SHORT emitida con éxito.")

    # Capa 1 y Capa 2: Señal LONG
    # Tendencia alcista (Precio > EMA 200) y RSI cruzando al alza desde zona de sobreventa (< 40)
    elif price > ema_200 and rsi_previous <= RSI_LONG_THRESHOLD and rsi_current > RSI_LONG_THRESHOLD:
        entry = price
        tp = entry * (1 + TP_PCT)
        sl = entry * (1 - SL_PCT)
        
        current_position = {
            "active": True,
            "side": "LONG",
            "entry_price": entry,
            "tp_price": tp,
            "sl_price": sl,
            "be_activated": False
        }
        
        msg = (
            f"🚨 *NUEVA SEÑAL LONG (5M)* 🚨\n\n"
            f"🪙 Par: `{SYMBOL}`\n"
            f"📈 Entrada: `${entry:.2f}`\n"
            f"💰 Margen: `${MARGIN_USDC} USDC` ({LEVERAGE}x | Nocional: `${NOTIONAL_SIZE}`)\n"
            f"🎯 Take Profit (+3.0%): `${tp:.2f}`\n"
            f"🛑 Stop Loss (-1.5%): `${sl:.2f}`\n"
            f"📊 Indicadores -> EMA200: `${ema_200:.2f}` | RSI: `{rsi_current:.1f}`\n"
            f"🛡️ *Break-Even Automático:* Activo (Disparo al +{BE_TRIGGER*100}%)"
        )
        send_telegram_message(msg)
        logger.info("Señal LONG emitida con éxito.")


def main():
    """Bucle principal de ejecución continua sincronizado con las velas de 5 minutos."""
    logger.info("Iniciando Bot Autónomo de Alta Frecuencia para SOL/USDT...")
    send_telegram_message("🤖 *Bot de Alta Frecuencia (SOL/USDT 5M)* iniciado y operando en Render.")
    
    while True:
        try:
            evaluate_market()
        except Exception as e:
            logger.error(f"Error crítico en el ciclo del bot: {e}")
        
        # Pausa de 60 segundos antes de reevaluar (optimizado para revisar transiciones rápidas)
        time.sleep(60)


if __name__ == "__main__":
    main()
