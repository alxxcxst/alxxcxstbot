import os
import time
import logging
import requests
import pandas as pd

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
TP_PCT = 0.03       # Take Profit: +3.0%
SL_PCT = 0.015      # Stop Loss: -1.5%
BE_TRIGGER = 0.012  # Activación de Break-Even: +1.2%

# Filtros Anti-Ruido
RSI_PERIOD = 14
RSI_SHORT_THRESHOLD = 60
RSI_LONG_THRESHOLD = 40
VOL_FACTOR = 0.8

# Credenciales de Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TU_CHAT_ID")

# Estado interno de la posición y control anti-spam de mensajes
current_position = {
    "active": False,
    "side": None,
    "entry_price": 0.0,
    "tp_price": 0.0,
    "sl_price": 0.0,
    "be_activated": False
}

last_sent_message_id = None  # Para eliminar el mensaje anterior y evitar spam


def send_telegram_message(text: str, replace_previous: bool = False):
    """Envía un mensaje a Telegram. Si replace_previous=True, borra el mensaje anterior para evitar spam."""
    global last_sent_message_id
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "TU_TELEGRAM_BOT_TOKEN":
        return
        
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    
    # Borrar mensaje anterior si se solicita
    if replace_previous and last_sent_message_id:
        try:
            requests.post(f"{base_url}/deleteMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": last_sent_message_id
            }, timeout=3)
        except Exception:
            pass

    # Enviar nuevo mensaje
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(f"{base_url}/sendMessage", json=payload, timeout=5)
        if response.status_code == 200:
            res_json = response.json()
            last_sent_message_id = res_json.get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"Excepción en Telegram API: {e}")


def fetch_binance_klines(symbol: str, interval: str, limit: int = 250) -> pd.DataFrame:
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
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['vol_avg'] = df['volume'].rolling(window=20).mean()
    return df


def check_telegram_commands():
    """Revisa comandos entrantes (/status, /price, /restart) para interacción en tiempo real."""
    global current_position, last_sent_message_id
    if not TELEGRAM_TOKEN:
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset=-1&timeout=1"
    try:
        response = requests.get(url, timeout=3)
        if response.status_code != 200:
            return
        data = response.json()
        results = data.get("result", [])
        if not results:
            return
            
        update = results[-1]
        message = update.get("message", {})
        text = message.get("text", "").strip().lower()
        msg_id = message.get("message_id")
        
        # Evitar procesar el mismo comando dos veces verificando si es reciente
        if not text.startswith("/"):
            return

        # Borrar el comando escrito por el usuario para mantener limpio el chat
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": msg_id
            }, timeout=2)
        except Exception:
            pass

        df = fetch_binance_klines(SYMBOL, INTERVAL, limit=50)
        if df.empty:
            return
        df = calculate_indicators(df)
        current_price = df.iloc[-1]['close']
        current_rsi = df.iloc[-1]['rsi']
        current_ema = df.iloc[-1]['ema_200']

        if text == "/status":
            status_text = "🟢 **ESTADO DEL SISTEMA - SOL/USDT**\n"
            status_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
            status_text += f"• **Nodo Render:** En línea ⚡\n"
            status_text += f"• **Posición Activa:** `{'SÍ (' + current_position['side'] + ')' if current_position['active'] else 'NO (Buscando entrada)'}`\n"
            if current_position['active']:
                status_text += f"• **Entrada:** `${current_position['entry_price']:.2f}`\n"
                status_text += f"• **Take Profit:** `${current_position['tp_price']:.2f}`\n"
                status_text += f"• **Stop Loss:** `${current_position['sl_price']:.2f}`\n"
                status_text += f"• **Break-Even Blindado:** `{'Activado ✅' if current_position['be_activated'] else 'Pendiente ⏳'}`\n"
            status_text += f"• **Precio Actual:** `${current_price:.2f}`\n"
            status_text += "━━━━━━━━━━━━━━━━━━━━━━"
            send_telegram_message(status_text, replace_previous=True)

        elif text == "/price":
            price_text = "📊 **TELEMETRÍA DE MERCADO (5M)**\n"
            price_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
            price_text += f"🪙 **Par:** `{SYMBOL}`\n"
            price_text += f"💵 **Precio Spot:** `${current_price:.2f}`\n"
            price_text += f"📈 **EMA 200:** `${current_ema:.2f}`\n"
            price_text += f"📉 **RSI (14):** `{current_rsi:.1f}`\n"
            price_text += f"🛡️ **Filtro Volumen:** `Activo`\n"
            price_text += "━━━━━━━━━━━━━━━━━━━━━━"
            send_telegram_message(price_text, replace_previous=True)

        elif text == "/restart":
            current_position["active"] = False
            restart_text = "🔄 **SISTEMA REINICIADO**\nEstado de posiciones reseteado a cero. El bot continúa operando en segundo plano."
            send_telegram_message(restart_text, replace_previous=True)

    except Exception as e:
        logger.error(f"Error procesando comandos de Telegram: {e}")


def evaluate_market():
    global current_position

    df = fetch_binance_klines(SYMBOL, INTERVAL, limit=250)
    if df.empty or len(df) < 205:
        return

    df = calculate_indicators(df)
    prev = df.iloc[-2]
    prev_prev = df.iloc[-3]
    
    price = prev['close']
    ema_200 = prev['ema_200']
    rsi_current = prev['rsi']
    rsi_previous = prev_prev['rsi']
    vol_current = prev['volume']
    vol_avg = prev['vol_avg']

    # --- GESTIÓN DE POSICIÓN ACTIVA ---
    if current_position["active"]:
        side = current_position["side"]
        entry = current_position["entry_price"]
        
        if side == "LONG":
            if not current_position["be_activated"] and (price - entry) / entry >= BE_TRIGGER:
                current_position["sl_price"] = entry
                current_position["be_activated"] = True
                msg = f"🛡️ **BREAK-EVEN BLINDADO (LONG)**\nPrecio alcanzó objetivo rápido. SL movido a entrada: `${entry:.2f}`"
                send_telegram_message(msg, replace_previous=True)
            
            if price >= current_position["tp_price"] or price <= current_position["sl_price"]:
                result_type = "TAKE PROFIT 🎯 (+3.0%)" if price >= current_position["tp_price"] else "STOP LOSS 🛑 (-1.5%)"
                msg = f"✅ **CIERRE DE POSICIÓN LONG**\nResultado: {result_type}\nPrecio: `${price:.2f}`"
                send_telegram_message(msg, replace_previous=True)
                current_position["active"] = False
                
        elif side == "SHORT":
            if not current_position["be_activated"] and (entry - price) / entry >= BE_TRIGGER:
                current_position["sl_price"] = entry
                current_position["be_activated"] = True
                msg = f"🛡️ **BREAK-EVEN BLINDADO (SHORT)**\nPrecio alcanzó objetivo rápido. SL movido a entrada: `${entry:.2f}`"
                send_telegram_message(msg, replace_previous=True)
                
            if price <= current_position["tp_price"] or price >= current_position["sl_price"]:
                result_type = "TAKE PROFIT 🎯 (+3.0%)" if price <= current_position["tp_price"] else "STOP LOSS 🛑 (-1.5%)"
                msg = f"✅ **CIERRE DE POSICIÓN SHORT**\nResultado: {result_type}\nPrecio: `${price:.2f}`"
                send_telegram_message(msg, replace_previous=True)
                current_position["active"] = False
        return

    # --- FILTROS ANTI-RUIDO Y APERTURA ---
    if vol_current < (vol_avg * VOL_FACTOR):
        return

    # Señal SHORT Ejecutiva
    if price < ema_200 and rsi_previous >= RSI_SHORT_THRESHOLD and rsi_current < RSI_SHORT_THRESHOLD:
        entry = price
        tp = entry * (1 - TP_PCT)
        sl = entry * (1 + SL_PCT)
        
        current_position = {
            "active": True, "side": "SHORT", "entry_price": entry,
            "tp_price": tp, "sl_price": sl, "be_activated": False
        }
        
        msg = (
            f"🚨 **ALTA FRECUENCIA • SEÑAL SHORT** 🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 **Activo:** `{SYMBOL}` (5M)\n"
            f"📉 **Entrada:** `${entry:.2f}`\n"
            f"💰 **Margen:** `${MARGIN_USDC} USDC` ({LEVERAGE}x | Nocional: `${NOTIONAL_SIZE}`)\n"
            f"🎯 **Take Profit (+3.0%):** `${tp:.2f}`\n"
            f"🛑 **Stop Loss (-1.5%):** `${sl:.2f}`\n"
            f"📊 **Filtros:** EMA200: `${ema_200:.2f}` | RSI: `{rsi_current:.1f}`\n"
            f"🛡️ **Break-Even:** Automático al +{BE_TRIGGER*100}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram_message(msg, replace_previous=False) # Las señales de entrada no borran estados para historial limpio

    # Señal LONG Ejecutiva
    elif price > ema_200 and rsi_previous <= RSI_LONG_THRESHOLD and rsi_current > RSI_LONG_THRESHOLD:
        entry = price
        tp = entry * (1 + TP_PCT)
        sl = entry * (1 - SL_PCT)
        
        current_position = {
            "active": True, "side": "LONG", "entry_price": entry,
            "tp_price": tp, "sl_price": sl, "be_activated": False
        }
        
        msg = (
            f"🚨 **ALTA FRECUENCIA • SEÑAL LONG** 🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 **Activo:** `{SYMBOL}` (5M)\n"
            f"📈 **Entrada:** `${entry:.2f}`\n"
            f"💰 **Margen:** `${MARGIN_USDC} USDC` ({LEVERAGE}x | Nocional: `${NOTIONAL_SIZE}`)\n"
            f"🎯 **Take Profit (+3.0%):** `${tp:.2f}`\n"
            f"🛑 **Stop Loss (-1.5%):** `${sl:.2f}`\n"
            f"📊 **Filtros:** EMA200: `${ema_200:.2f}` | RSI: `{rsi_current:.1f}`\n"
            f"🛡️ **Break-Even:** Automático al +{BE_TRIGGER*100}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram_message(msg, replace_previous=False)


def main():
    logger.info("Iniciando Bot Autónomo de Alta Frecuencia para SOL/USDT...")
    send_telegram_message("🤖 **Bot de Alta Frecuencia (SOL/USDT 5M)** en línea y operando en Render.", replace_previous=True)
    
    while True:
        try:
            check_telegram_commands()
            evaluate_market()
        except Exception as e:
            logger.error(f"Error crítico en el ciclo: {e}")
        
        time.sleep(10)  # Ciclo rápido para capturar comandos instantáneamente y evaluar velas


if __name__ == "__main__":
    main()
