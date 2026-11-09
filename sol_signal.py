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
# Compatible con cualquier nombre que tengas en Render Environment
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "8459722038"
PORT = int(os.getenv("PORT", 8080))

# Parámetros Operativos SOL/USDT
SYMBOL = "SOLUSDT"
UMBRAL_BE_PCT = 0.015   # 1.5% de movimiento favorable para avisar Break-Even
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
    "be_activado": False,      # Bandera para evitar alertas duplicadas
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
# 4. COMANDOS Y BOTONES DE TELEGRAM
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ *ALPHA BOT CONECTADO*\n\n"
        "• *Par:* SOL/USDT\n"
        "• *Velas:* 15m (EMA 200 + RSI 14)\n\n"
        "Comandos:\n"
        "/status - Ver estado del escáner y posición\n"
        "/reset - Limpiar posición a mano para nueva señal"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if estado["posicion"]:
        estado_be = "🛡️ Blindado en BE" if estado["be_activado"] else "⏳ Esperando zona BE (+1.5%)"
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
            "🔄 *Posición cerrada en el bot.*\nModo escaneo reactivado para buscar el siguiente trade.",
            parse_mode="Markdown"
        )

# ==========================================
# 5. RASTREADOR DE PRECIO Y BREAK-EVEN
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
                "🛡️ *ALERTA: ASEGURAR BREAK-EVEN (BE)*\n\n"
                f"SOL cayó a *${precio_actual:.3f}* (+{movimiento_favor*100:.2f}% a favor)\n\n"
                f"👉 *Acción en Phantom:* Mueve tu Stop Loss a *${entrada:.3f}* (o ligeramente abajo) para dejar el trade con $0.00 de riesgo."
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
                "🛡️ *ALERTA: ASEGURAR BREAK-EVEN (BE)*\n\n"
                f"SOL subió a *${precio_actual:.3f}* (+{movimiento_favor*100:.2f}% a favor)\n\n"
                f"👉 *Acción en Phantom:* Mueve tu Stop Loss a *${entrada:.3f}* para blindar tu capital en $0.00 de riesgo."
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
# 6. LOOP DE ESCANEO DE MERCADO
# ==========================================
async def loop_escaneo(bot_app: Application):
    """Ciclo en segundo plano que vigila el precio y busca nuevas señales."""
    await asyncio.sleep(3)
    
    while True:
        try:
            # Monitoreo de precio Spot SOL/USDT vía Binance API pública
            res_ticker = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}", timeout=5)
            if res_ticker.status_code == 200:
                precio_actual = float(res_ticker.json()["price"])
                await verificar_break_even(precio_actual, bot_app)

            estado["velas_escaneadas"] += 1

        except Exception as e:
            print(f"[ERROR] En loop de escaneo: {e}")

        await asyncio.sleep(20)  # Consulta cada 20 segundos

# ==========================================
# 7. FUNCIÓN DE ARRANQUE CON ASYNCIO
# ==========================================
async def on_startup(app: Application):
    """Arranca el loop de escaneo dentro del bucle de eventos nativo de Telegram."""
    asyncio.create_task(loop_escaneo(app))

def main():
    # 1. Levantar servidor Flask en hilo secundario (Keep-Alive Render)
    threading.Thread(target=run_flask, daemon=True).start()

    # 2. Configurar bot de Telegram con post_init (compatible con Python 3.14+)
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # 3. Registrar handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", reset_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # 4. Iniciar polling
    print("[INFO] Bot y servidor Flask en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
