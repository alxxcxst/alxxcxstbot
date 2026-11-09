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
UMBRAL_BE_PCT = 0.015   # 1.5% a favor para avisar Break-Even
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
    "be_activado": False,      # Evita enviar alertas duplicadas de BE
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
# 4. FUNCIÓN AUXILIAR DE PRECIO (INMUNE A BLOQUEOS)
# ==========================================
def obtener_precio_sol():
    """Consulta el precio spot de SOL vía Jupiter o Coinbase evitando bloqueos de Render."""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # Intento 1: Jupiter API (Solana DEX nativo, sin bloqueos de IP en la nube)
    try:
        url_jup = "https://price.jup.ag/v6/price?ids=SOL"
        res = requests.get(url_jup, headers=headers, timeout=4)
        if res.status_code == 200:
            data = res.json()
            return float(data["data"]["SOL"]["price"])
    except Exception:
        pass

    # Intento 2: Coinbase API pública
    try:
        url_cb = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
        res = requests.get(url_cb, headers=headers, timeout=4)
        if res.status_code == 200:
            data = res.json()
            return float(data["data"]["amount"])
    except Exception:
        pass

    return None

# ==========================================
# 5. COMANDOS Y BOTONES DE TELEGRAM
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ *ALPHA BOT CONECTADO*\n\n"
        "• *Par:* SOL/USDT\n"
        "• *Velas:* 15m (EMA 200 + RSI 14)\n\n"
        "Comandos disponibles:\n"
        "/status - Ver estado del escáner y posición actual\n"
        "/precio - Consultar precio spot en vivo de SOL\n"
        "/reset - Limpiar trade activo a mano"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def precio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde con el precio en tiempo real de SOL."""
    precio = obtener_precio_sol()
    if precio is not None:
        msg = (
            f"🪙 *SOL/USDT (Solana)*\n\n"
            f"• Precio actual: *${precio:.2f} USD*\n"
            f"• Intervalo monitoreado: 15m\n"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Conexión lenta al consultar el precio. Intenta de nuevo en unos segundos.")

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
# 7. LOOP DE ESCANEO DE MERCADO
# ==========================================
async def loop_escaneo(bot_app: Application):
    """Ciclo en segundo plano que vigila el precio y busca nuevas señales."""
    await asyncio.sleep(3)
    
    while True:
        try:
            precio_actual = obtener_precio_sol()
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
    """Arranca el loop de escaneo dentro del bucle de eventos nativo de Telegram."""
    asyncio.create_task(loop_escaneo(app))

def main():
    # 1. Levantar servidor Flask en hilo secundario (Keep-Alive Render)
    threading.Thread(target=run_flask, daemon=True).start()

    # 2. Configurar bot de Telegram con post_init
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # 3. Registrar handlers de comandos y botones
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
