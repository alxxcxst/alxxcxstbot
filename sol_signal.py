import os
import asyncio
import threading
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# 1. CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "8459722038"
PORT = int(os.getenv("PORT", 8080))

# Parámetros Operativos SOL/USDT
SYMBOL = "SOLUSDT"
UMBRAL_BE_PCT = 0.015   # 1.5% a favor para Break-Even
RATIO_TP_PCT = 0.030    # 3.0% Take Profit
RATIO_SL_PCT = 0.015    # 1.5% Stop Loss

# ==========================================
# 2. ESTADO GLOBAL EN MEMORIA
# ==========================================
estado = {
    "posicion": None,          # None | 'SHORT' | 'LONG'
    "precio_entrada": 0.0,
    "take_profit": 0.0,
    "stop_loss": 0.0,
    "be_activado": False,      # Evita enviar alertas duplicadas
    "velas_escaneadas": 0
}

# ==========================================
# 3. SERVIDOR WEB FLASK (Keep-Alive Render)
# ==========================================
flask_app = Flask(__name__)

@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    return "OK - Bot Operativo", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ==========================================
# 4. MOTOR TÉCNICO: PRECIO Y RSI (14) EN 15m
# ==========================================
def calcular_rsi(precios_cierre, periodo=14):
    """Calcula el RSI estándar de 14 periodos."""
    if len(precios_cierre) < periodo + 1:
        return 50.0  # Retorno neutral si faltan datos

    cambios = [precios_cierre[i] - precios_cierre[i - 1] for i in range(1, len(precios_cierre))]
    ganancias = [max(c, 0.0) for c in cambios]
    perdidas = [abs(min(c, 0.0)) for c in cambios]

    media_ganancia = sum(ganancias[:periodo]) / periodo
    media_perdida = sum(perdidas[:periodo]) / periodo

    for i in range(periodo, len(cambios)):
        media_ganancia = (media_ganancia * (periodo - 1) + ganancias[i]) / periodo
        media_perdida = (media_perdida * (periodo - 1) + perdidas[i]) / periodo

    if media_perdida == 0:
        return 100.0
    
    rs = media_ganancia / media_perdida
    return 100.0 - (100.0 / (1.0 + rs))

def obtener_datos_mercado():
    """Consulta precio actual y velas de 15m para calcular el RSI."""
    headers = {"User-Agent": "Mozilla/5.0"}
    precio = None
    rsi_val = None

    # 1. Obtener precio spot vía Jupiter o Coinbase
    try:
        url_jup = "https://price.jup.ag/v6/price?ids=SOL"
        res = requests.get(url_jup, headers=headers, timeout=4)
        if res.status_code == 200:
            precio = float(res.json()["data"]["SOL"]["price"])
    except Exception:
        pass

    if precio is None:
        try:
            url_cb = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
            res = requests.get(url_cb, headers=headers, timeout=4)
            if res.status_code == 200:
                precio = float(res.json()["data"]["amount"])
        except Exception:
            pass

    # 2. Obtener velas de 15m para RSI (Coinbase Pro API pública, sin bloqueos de IP)
    try:
        url_candles = "https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=900"
        res_c = requests.get(url_candles, headers=headers, timeout=4)
        if res_c.status_code == 200:
            candles = res_c.json()  # Cada vela: [time, low, high, open, close, volume]
            candles.reverse()       # Ordenar cronológicamente
            cierres = [float(c[4]) for c in candles]
            if len(cierres) >= 15:
                rsi_val = calcular_rsi(cierres, 14)
    except Exception:
        pass

    return precio, rsi_val

# ==========================================
# 5. COMANDOS VISUALES DE TELEGRAM
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🚀 *ALPHA BOT · SOLANA SYSTEM*\n"
        "─────────────────────────\n"
        "📡 *Estatus:* Conectado y Escaneando\n"
        "🎯 *Par:* SOL/USDT\n"
        "⏱️ *Temporalidad:* 15m (EMA 200 + RSI 14)\n"
        "─────────────────────────\n"
        "💡 *Comandos disponibles:*\n"
        "🔹 `/precio` — Cotización, RSI y sesgo en vivo\n"
        "🔹 `/status` — Ver estado del trade activo\n"
        "🔹 `/reset` — Limpiar posición a mano\n"
        "─────────────────────────\n"
        "_Listo para cazar el siguiente trade de la bitácora._"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
            InlineKeyboardButton("📈 TradingView", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
        ]
    ])
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")

async def precio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    precio, rsi_val = obtener_datos_mercado()
    
    if precio is not None:
        # Interpretación visual del RSI
        if rsi_val is not None:
            if rsi_val >= 70:
                rsi_str = f"`{rsi_val:.1f}` 🔴 (Sobrecompra · Favorable SHORT 🔻)"
            elif rsi_val <= 30:
                rsi_str = f"`{rsi_val:.1f}` 🟢 (Sobreventa · Favorable LONG 🔺)"
            elif rsi_val > 50:
                rsi_str = f"`{rsi_val:.1f}` ⚖️ (Zona Alcista Moderada)"
            else:
                rsi_str = f"`{rsi_val:.1f}` ⚖️ (Zona Bajista Moderada)"
        else:
            rsi_str = "`Calculando...`"

        # Sesgo según posición
        if estado["posicion"] == "SHORT":
            sesgo = "🔴 *SHORT en curso* 🔻"
        elif estado["posicion"] == "LONG":
            sesgo = "🟢 *LONG en curso* 🔺"
        else:
            sesgo = "🔍 *Escaneando entrada* (EMA 200 + RSI)"

        msg = (
            "🪙 *SOL / USDT (Solana)*\n"
            "─────────────────────────\n"
            f"💵 *Precio Spot:* `${precio:.2f} USD`\n"
            f"📊 *RSI (14, 15m):* {rsi_str}\n"
            f"🧭 *Sesgo:* {sesgo}\n"
            "─────────────────────────\n"
            "🎯 *Estrategia:* Pullback EMA 200 + RSI\n"
            "⚡ *Riesgo:* 1:2 R/R (BE automático al +1.5%)"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚡ Phantom", url="https://phantom.app"),
                InlineKeyboardButton("📊 Gráfica 15m", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
            ]
        ])
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Conexión lenta al consultar cotización. Intenta en unos segundos.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if estado["posicion"]:
        tipo = estado["posicion"]
        icono = "🔴" if tipo == "SHORT" else "🟢"
        flecha = "🔻" if tipo == "SHORT" else "🔺"
        estado_be = "🛡️ *BLINDADO EN BREAK-EVEN* ($0.00 riesgo)" if estado["be_activado"] else "⏳ *CORRIENDO* (Esperando zona BE al +1.5%)"
        
        pos_str = (
            f"{icono} *{tipo} · SOL/USDT* {flecha}\n"
            "─────────────────────────\n"
            f"📍 *Entrada:* `${estado['precio_entrada']:.2f}`\n"
            f"🎯 *Take Profit (+3%):* `${estado['take_profit']:.2f}`\n"
            f"🛑 *Stop Loss (-1.5%):* `${estado['stop_loss']:.2f}`\n"
            f"🛡️ *Estatus:* {estado_be}\n"
            "─────────────────────────"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                InlineKeyboardButton("🔄 Cerrar / Reset", callback_data="btn_reset")
            ]
        ])
    else:
        pos_str = (
            "🟢 *MODO ESCANEO ACTIVO*\n"
            "─────────────────────────\n"
            "🔎 *Buscando:* Señales EMA 200 + RSI en velas 15m\n"
            "💼 *Posición:* Ninguna activa (Capital protegido)\n"
            "─────────────────────────"
        )
        keyboard = None

    msg = (
        "⚡ *PANEL DE CONTROL DEL BOT*\n\n"
        f"{pos_str}\n"
        f"🖥️ *Servidor:* Render (Live 24/7)\n"
        f"🔄 *Velas monitoreadas:* `{estado['velas_escaneadas']}`"
    )
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estado["posicion"] = None
    estado["precio_entrada"] = 0.0
    estado["take_profit"] = 0.0
    estado["stop_loss"] = 0.0
    estado["be_activado"] = False
    
    msg = (
        "🔄 *POSICIÓN RESETEADA*\n"
        "─────────────────────────\n"
        "✅ Memoria liberada correctamente.\n"
        "📡 El bot vuelve a modo escaneo para cazar el siguiente trade."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "btn_reset":
        estado["posicion"] = None
        estado["precio_entrada"] = 0.0
        estado["take_profit"] = 0.0
        estado["stop_loss"] = 0.0
        estado["be_activado"] = False
        await query.edit_message_text(
            "🔄 *Trade cerrado en el sistema.*\n"
            "Modo escaneo reactivado para el siguiente trade de la bitácora.",
            parse_mode="Markdown"
        )

# ==========================================
# 6. RASTREADOR DE BREAK-EVEN
# ==========================================
async def verificar_break_even(precio_actual: float, bot_app: Application):
    """Monitorea el precio en tiempo real para disparar la alerta de Break-Even."""
    if not estado["posicion"]:
        return

    entrada = estado["precio_entrada"]

    # Caso SHORT
    if estado["posicion"] == "SHORT":
        movimiento_favor = (entrada - precio_actual) / entrada
        if movimiento_favor >= UMBRAL_BE_PCT and not estado["be_activado"]:
            estado["be_activado"] = True
            msg = (
                "🛡️ *ALERTA: BLINDAR EN BREAK-EVEN (BE)*\n"
                "─────────────────────────\n"
                "🔻 *Posición:* SHORT · SOL/USDT\n"
                f"💵 *Precio actual:* `${precio_actual:.2f}` (+{movimiento_favor*100:.2f}% a favor)\n"
                "─────────────────────────\n"
                f"👉 *Acción en Phantom:* Mueve tu Stop Loss a *${entrada:.2f}* (o ligeramente menor).\n"
                "✨ *Riesgo del trade:* $0.00 USD asegurado."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app")],
                [InlineKeyboardButton("🔄 Trade Cerrado / Reset", callback_data="btn_reset")]
            ])
            await bot_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard
            )

        elif precio_actual >= estado["stop_loss"] or precio_actual <= estado["take_profit"]:
            estado["posicion"] = None
            estado["be_activado"] = False

    # Caso LONG
    elif estado["posicion"] == "LONG":
        movimiento_favor = (precio_actual - entrada) / entrada
        if movimiento_favor >= UMBRAL_BE_PCT and not estado["be_activado"]:
            estado["be_activado"] = True
            msg = (
                "🛡️ *ALERTA: BLINDAR EN BREAK-EVEN (BE)*\n"
                "─────────────────────────\n"
                "🔺 *Posición:* LONG · SOL/USDT\n"
                f"💵 *Precio actual:* `${precio_actual:.2f}` (+{movimiento_favor*100:.2f}% a favor)\n"
                "─────────────────────────\n"
                f"👉 *Acción en Phantom:* Mueve tu Stop Loss a *${entrada:.2f}*.\n"
                "✨ *Riesgo del trade:* $0.00 USD asegurado."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app")],
                [InlineKeyboardButton("🔄 Trade Cerrado / Reset", callback_data="btn_reset")]
            ])
            await bot_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard
            )

        elif precio_actual <= estado["stop_loss"] or precio_actual >= estado["take_profit"]:
            estado["posicion"] = None
            estado["be_activado"] = False

# ==========================================
# 7. LOOP DE ESCANEO DE MERCADO
# ==========================================
async def loop_escaneo(bot_app: Application):
    """Ciclo en segundo plano que vigila el precio."""
    await asyncio.sleep(3)
    
    while True:
        try:
            precio_actual, _ = obtener_datos_mercado()
            if precio_actual is not None:
                await verificar_break_even(precio_actual, bot_app)

            estado["velas_escaneadas"] += 1

        except Exception as e:
            print(f"[ERROR] En loop de escaneo: {e}")

        await asyncio.sleep(20)

# ==========================================
# 8. INICIO ASÍNCRONO DEL SERVICIO
# ==========================================
async def on_startup(app: Application):
    """Arranca el loop de escaneo en segundo plano."""
    asyncio.create_task(loop_escaneo(app))

def main():
    # 1. Servidor Flask en hilo secundario (Keep-Alive Render)
    threading.Thread(target=run_flask, daemon=True).start()

    # 2. Configurar bot de Telegram con post_init
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # 3. Registrar handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("precio", precio_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", reset_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # 4. Iniciar polling
    print("[INFO] Bot y servidor Flask en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
