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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", 8080))

# Parámetros de Trading SOL/USDT
SYMBOL = "SOLUSDT"
INTERVAL = "15m"
UMBRAL_BE_PCT = 0.015   # 1.5% de movimiento a favor para avisar Break-Even
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
    "be_activado": False,      # Evita enviar múltiples alertas de BE
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
# 4. COMANDOS DE TELEGRAM
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ *ALPHA BOT CONECTADO*\n\n"
        "• *Par:* SOL/USDT\n"
        "• *Velas:* 15m (EMA 200 + RSI 14)\n\n"
        "Comandos disponibles:\n"
        "/status - Ver estado del escáner y posición actual\n"
        "/reset - Limpiar posición a mano y volver a modo búsqueda"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if estado["posicion"]:
        estado_be = "🛡️ Blindado en BE" if estado["be_activado"] else "⏳ Esperando zona BE"
        pos_str = (
            f"*{estado['posicion']}* @ ${estado['precio_entrada']:.3f}\n"
            f"• TP: ${estado['take_profit']:.3f} | SL: ${estado['stop_loss']:.3f}\n"
            f"• Estado: {estado_be}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Cerrar Trade / Resetear", callback_data="btn_reset")]
        ])
    else:
        pos_str = "🟢 *NINGUNA* (Escaneando velas libremente)"
        keyboard = None

    msg = (
        "⚡ *ESTADO DEL BOT*\n\n"
        f"• *Servidor:* Render (Activo)\n"
        f"• *Velas analizadas:* {estado['velas_escaneadas']}\n"
        f"• *Posición:* {pos_str}\n"
    )
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estado["posicion"] = None
    estado["precio_entrada"] = 0.0
    estado["take_profit"] = 0.0
    estado["stop_loss"] = 0.0
    estado["be_activado"] = False
    
    await update.message.reply_text(
        "🔄 *Posición reseteada con éxito.*\n"
        "El bot vuelve a modo escaneo para el siguiente trade de la bitácora.",
        parse_mode="Markdown"
    )

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
            "🔄 *Posición cerrada en el bot.*\nModo escaneo reactivado.",
            parse_mode="Markdown"
        )

# ==========================================
# 5. MONITOREO DE PRECIO Y ALERTAS DE BE
# ==========================================
async def verificar_break_even(precio_actual: float, bot_app):
    """Revisa si el precio tocó el umbral para avisar Break-Even o cierre."""
    if not estado["posicion"]:
        return

    entrada = estado["precio_entrada"]

    # Caso SHORT
    if estado["posicion"] == "SHORT":
        movimiento_favor = (entrada - precio_actual) / entrada
        # Si cae 1.5% o más a favor y aún no avisa
        if movimiento_favor >= UMBRAL_BE_PCT and not estado["be_activado"]:
            estado["be_activado"] = True
            msg = (
                "🛡️ *ALERTA: ASEGURAR BREAK-EVEN (BE)*\n\n"
                f"SOL cayó a *${precio_actual:.3f}* (+{movimiento_favor*100:.2f}% a favor)\n"
                f"👉 *Acción en Phantom:* Mueve tu Stop Loss inmediatamente a *${entrada:.3f}* (o ligeramente abajo) para dejar el trade en riesgo cero."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app")],
                [InlineKeyboardButton("🔄 Trade Cerrado / Reset", callback_data="btn_reset")]
            ])
            await bot_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard
            )

        # Cierre automático en memoria si toca SL original o TP
        elif precio_actual >= estado["stop_loss"] or precio_actual <= estado["take_profit"]:
            estado["posicion"] = None
            estado["be_activado"] = False

    # Caso LONG
    elif estado["posicion"] == "LONG":
        movimiento_favor = (precio_actual - entrada) / entrada
        if movimiento_favor >= UMBRAL_BE_PCT and not estado["be_activado"]:
            estado["be_activado"] = True
            msg = (
                "🛡️ *ALERTA: ASEGURAR BREAK-EVEN (BE)*\n\n"
                f"SOL subió a *${precio_actual:.3f}* (+{movimiento_favor*100:.2f}% a favor)\n"
                f"👉 *Acción en Phantom:* Mueve tu Stop Loss a *${entrada:.3f}* para blindar tu capital en $0 riesgo."
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
# 6. CICLO DE ESCANEO TÉCNICO (15M)
# ==========================================
async def loop_escaneo(bot_app):
    """Ciclo en segundo plano que consulta velas y busca señales."""
    await asyncio.sleep(5)  # Espera inicio del bot
    
    while True:
        try:
            # Obtener precio spot actual de Binance para monitoreo en tiempo real
            res_ticker = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}", timeout=5)
            if res_ticker.status_code == 200:
                precio_actual = float(res_ticker.json()["price"])
                await verificar_break_even(precio_actual, bot_app)

            # Si no hay posición activa, analiza si se forma una señal nueva
            # (Aquí mantienes tu lógica actual de cálculo de EMA 200 y RSI con ta / pandas-ta)
            # Cuando tu estrategia dispare una entrada nueva, simplemente actualizas:
            # estado["posicion"] = "SHORT" (o "LONG")
            # estado["precio_entrada"] = precio_actual
            # estado["take_profit"] = precio_actual * (1 - RATIO_TP_PCT)
            # estado["stop_loss"] = precio_actual * (1 + RATIO_SL_PCT)
            # estado["be_activado"] = False
            
            estado["velas_escaneadas"] += 1

        except Exception as e:
            print(f"[ERROR] Fallo en ciclo de análisis: {e}")

        await asyncio.sleep(30)  # Monitorea precio cada 30 segundos

# ==========================================
# 7. INICIO DE LA APLICACIÓN
# ==========================================
def main():
    # 1. Iniciar servidor Flask en hilo independiente
    threading.Thread(target=run_flask, daemon=True).start()

    # 2. Configurar aplicación de Telegram
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", reset_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # 3. Iniciar loop de escaneo
    loop = asyncio.get_event_loop()
    loop.create_task(loop_escaneo(app))

    # 4. Iniciar bot en polling
    print("[INFO] Bot y servidor Flask en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
