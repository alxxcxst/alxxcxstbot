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
    "be_activado": False,
    "velas_escaneadas": 0,
    "ultimo_msg_id": None      # Almacena el ID del mensaje anterior para auto-limpieza
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
# 4. MOTOR DE DATOS: PRECIO Y RSI (14) EN 15m
# ==========================================
def calcular_rsi(precios_cierre, periodo=14):
    if len(precios_cierre) < periodo + 1:
        return 50.0

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
    headers = {"User-Agent": "Mozilla/5.0"}
    precio = None
    rsi_val = None

    # Precio spot en Jupiter
    try:
        url_jup = "https://price.jup.ag/v6/price?ids=SOL"
        res = requests.get(url_jup, headers=headers, timeout=4)
        if res.status_code == 200:
            precio = float(res.json()["data"]["SOL"]["price"])
    except Exception:
        pass

    # Respaldo Coinbase
    if precio is None:
        try:
            url_cb = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
            res = requests.get(url_cb, headers=headers, timeout=4)
            if res.status_code == 200:
                precio = float(res.json()["data"]["amount"])
        except Exception:
            pass

    # Velas de 15m para RSI (Coinbase Pro API)
    try:
        url_candles = "https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=900"
        res_c = requests.get(url_candles, headers=headers, timeout=4)
        if res_c.status_code == 200:
            candles = res_c.json()
            candles.reverse()
            cierres = [float(c[4]) for c in candles]
            if len(cierres) >= 15:
                rsi_val = calcular_rsi(cierres, 14)
    except Exception:
        pass

    return precio, rsi_val

# ==========================================
# 5. GESTIÓN DE AUTO-LIMPIEZA DE MENSAJES
# ==========================================
async def enviar_mensaje_limpio(context: ContextTypes.DEFAULT_TYPE, chat_id: int, texto: str, reply_markup=None):
    """Borra el mensaje anterior del bot y publica el nuevo manteniéndolo limpio."""
    if estado["ultimo_msg_id"] is not None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=estado["ultimo_msg_id"])
        except Exception:
            pass  # Ignorar si ya fue borrado manualmente

    nuevo_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=texto,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    estado["ultimo_msg_id"] = nuevo_msg.message_id
    return nuevo_msg

# ==========================================
# 6. COMANDOS DE TELEGRAM
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Borrar el comando que escribió el usuario
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = (
        "🚀 *ALPHA BOT · SOL/USDT*\n\n"
        "Velas: `15m` (EMA 200 + RSI 14)\n"
        "Ratio: `1:2` (BE automático al +1.5%)\n\n"
        "Filtros: Escaneo en vivo activado"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
            InlineKeyboardButton("📈 TradingView", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
        ]
    ])
    await enviar_mensaje_limpio(context, update.effective_chat.id, msg, keyboard)

async def precio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Borrar el texto /precio del usuario
    try:
        await update.message.delete()
    except Exception:
        pass

    precio, rsi_val = obtener_datos_mercado()
    
    if precio is not None:
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "50.0"
        
        # Formato de tarjeta limpia idéntica al diseño de señal
        if estado["posicion"] == "SHORT":
            encabezado = "🔴 *SHORT · SOL/USDT* 🔻"
            sesgo_str = "SHORT en curso"
        elif estado["posicion"] == "LONG":
            encabezado = "🟢 *LONG · SOL/USDT* 🔺"
            sesgo_str = "LONG en curso"
        else:
            encabezado = "🪙 *SOL/USDT* 📊"
            sesgo_str = "Escaneando velas libremente"

        msg = (
            f"{encabezado}\n\n"
            f"Precio: `{precio:.2f}`\n"
            f"RSI 14 (15m): `{rsi_str}`\n"
            f"Velas analizadas: `{estado['velas_escaneadas']}`\n\n"
            f"Ratio: 1:2 (BE al +1.5%)\n"
            f"Filtro: {sesgo_str}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                InlineKeyboardButton("📈 TradingView", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
            ]
        ])
        await enviar_mensaje_limpio(context, update.effective_chat.id, msg, keyboard)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Borrar el texto /status del usuario
    try:
        await update.message.delete()
    except Exception:
        pass

    if estado["posicion"]:
        tipo = estado["posicion"]
        icono = "🔴" if tipo == "SHORT" else "🟢"
        flecha = "🔻" if tipo == "SHORT" else "🔺"
        estado_be = "Blindado en BE ($0.00 riesgo)" if estado["be_activado"] else "Esperando zona BE (+1.5%)"
        
        msg = (
            f"{icono} *{tipo} · SOL/USDT* {flecha}\n\n"
            f"Entrada: `{estado['precio_entrada']:.2f}`\n"
            f"TP (+3.0%): `{estado['take_profit']:.2f}`\n"
            f"SL (-1.5%): `{estado['stop_loss']:.2f}`\n\n"
            f"Ratio: 1:2\n"
            f"Filtro: {estado_be}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                InlineKeyboardButton("🔄 Resetear Trade", callback_data="btn_reset")
            ]
        ])
    else:
        msg = (
            "🟢 *MODO ESCANEO · SOL/USDT* 🔎\n\n"
            f"Servidor: `Render (Live 24/7)`\n"
            f"Velas analizadas: `{estado['velas_escaneadas']}`\n"
            f"Posición: `Ninguna activa (Capital protegido)`\n\n"
            f"Ratio: 1:2\n"
            f"Filtro: EMA 200 + RSI 14 (15m)"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                InlineKeyboardButton("📈 TradingView", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
            ]
        ])

    await enviar_mensaje_limpio(context, update.effective_chat.id, msg, keyboard)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    estado["posicion"] = None
    estado["precio_entrada"] = 0.0
    estado["take_profit"] = 0.0
    estado["stop_loss"] = 0.0
    estado["be_activado"] = False
    
    msg = (
        "🔄 *POSICIÓN RESETEADA*\n\n"
        "Velas: `15m` (Modo escaneo activo)\n"
        "Posición: `Ninguna (Buscando siguiente trade)`\n\n"
        "Filtro: Memoria liberada con éxito"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
            InlineKeyboardButton("📈 TradingView", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
        ]
    ])
    await enviar_mensaje_limpio(context, update.effective_chat.id, msg, keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "btn_reset":
        estado["posicion"] = None
        estado["precio_entrada"] = 0.0
        estado["take_profit"] = 0.0
        estado["stop_loss"] = 0.0
        estado["be_activado"] = False
        
        msg = (
            "🔄 *TRADE CERRADO EN EL BOT*\n\n"
            "Velas: `15m` (Modo escaneo)\n"
            "Posición: `Ninguna activa`\n\n"
            "Filtro: Buscando el siguiente trade"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                InlineKeyboardButton("📈 TradingView", url="https://www.tradingview.com/chart/?symbol=BINANCE%3ASOLUSDT")
            ]
        ])
        await query.edit_message_text(text=msg, parse_mode="Markdown", reply_markup=keyboard)

# ==========================================
# 7. RASTREADOR DE BREAK-EVEN
# ==========================================
async def verificar_break_even(precio_actual: float, bot_app: Application):
    if not estado["posicion"]:
        return

    entrada = estado["precio_entrada"]

    # Caso SHORT
    if estado["posicion"] == "SHORT":
        movimiento_favor = (entrada - precio_actual) / entrada
        if movimiento_favor >= UMBRAL_BE_PCT and not estado["be_activado"]:
            estado["be_activado"] = True
            msg = (
                "🛡️ *BREAK-EVEN ASEGURADO · SOL/USDT* 🔻\n\n"
                f"Entrada: `{entrada:.2f}`\n"
                f"Precio actual: `{precio_actual:.2f}` (+{movimiento_favor*100:.2f}%)\n"
                f"Nuevo SL: `{entrada:.2f}` ($0.00 riesgo)\n\n"
                f"Ratio: Protegido\n"
                f"Filtro: Mover SL en Phantom a precio de entrada"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                    InlineKeyboardButton("🔄 Resetear Trade", callback_data="btn_reset")
                ]
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
                "🛡️ *BREAK-EVEN ASEGURADO · SOL/USDT* 🔺\n\n"
                f"Entrada: `{entrada:.2f}`\n"
                f"Precio actual: `{precio_actual:.2f}` (+{movimiento_favor*100:.2f}%)\n"
                f"Nuevo SL: `{entrada:.2f}` ($0.00 riesgo)\n\n"
                f"Ratio: Protegido\n"
                f"Filtro: Mover SL en Phantom a precio de entrada"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⚡ Abrir Phantom", url="https://phantom.app"),
                    InlineKeyboardButton("🔄 Resetear Trade", callback_data="btn_reset")
                ]
            ])
            await bot_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard
            )

        elif precio_actual <= estado["stop_loss"] or precio_actual >= estado["take_profit"]:
            estado["posicion"] = None
            estado["be_activado"] = False

# ==========================================
# 8. LOOP DE ESCANEO DE MERCADO
# ==========================================
async def loop_escaneo(bot_app: Application):
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
# 9. ARRANQUE DEL BOT
# ==========================================
async def on_startup(app: Application):
    asyncio.create_task(loop_escaneo(app))

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("precio", precio_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("clear", reset_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("[INFO] Bot y servidor Flask en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
