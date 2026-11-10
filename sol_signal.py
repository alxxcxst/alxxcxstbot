import os
import time
import struct
import hashlib
import base64
import threading
import requests
import pandas as pd
from flask import Flask

# Librerías criptográficas nativas de Solana (solders)
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.hash import Hash

# ==========================================
# 1. PARÁMETROS Y VARIABLES DE ENTORNO
# ==========================================
TELEGRAM_BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGRAM_TOKEN")
    or os.environ.get("BOT_TOKEN")
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "8459722038"
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()

# Lee tu RPC privado configurado en Render (Helius)
RPC_URL = os.environ.get("RPC_URL", "https://mainnet.helius-rpc.com/?api-key=e7f1c1a9-1290-46d2-ac8a-5c783fb25a83")

BOT_WALLET_PUBKEY = "DWgcHcAqm6vJfWhJ7dU4iE5CnFqW4d8EoqW3wmSXd4bZ"

SYMBOL = "SOL-PERP"
TIMEFRAME = "15m"

# Gestión de capital ($2.00 USDC x 6 = $12.00 USD nocional > mínimo de $10 en Jupiter)
MARGIN_USDC = float(os.environ.get("MARGIN_USDC", "2.0"))
LEVERAGE = int(os.environ.get("LEVERAGE", "6"))

# Gestión de riesgo R:R 1:2 ajustada para cubrir Keeper fee de Jupiter Perps
SL_PCT = 0.015          # 1.5% Stop Loss
TP_PCT = 0.030          # 3.0% Take Profit
BE_TRIGGER_PCT = 0.020  # Activar Break-Even al +2.0%
BE_OFFSET_PCT = 0.003   # Proteger en +0.3% sobre entrada para cubrir fees on-chain

# Constantes del Contrato Inteligente de Jupiter Perpetuals (Mainnet)
JUP_PERPS_PROGRAM_ID = Pubkey.from_string("PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu")
JLP_POOL_PUBKEY = Pubkey.from_string("5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq")
SOL_CUSTODY = Pubkey.from_string("7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz")
USDC_CUSTODY = Pubkey.from_string("G18jKKXQwBbrHeiK3C9MRXhkHsugf7XgCSisykV46EZa")
SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOC_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# Variables de estado
latest_market_data = {"price": None, "ema": None, "rsi": None}
active_trade = None
candles_scanned_count = 0
last_bot_message_id = None
threads_started = False

app = Flask(__name__)

# ==========================================
# 2. MOTOR DE COMUNICACIÓN TELEGRAM
# ==========================================
def delete_telegram_message(chat_id, message_id):
    if not TELEGRAM_BOT_TOKEN or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
    except Exception:
        pass

def send_telegram_alert(message, target_chat_id=None, clean_previous=False):
    global last_bot_message_id
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return

    dest_id = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID

    if clean_previous and last_bot_message_id:
        delete_telegram_message(dest_id, last_bot_message_id)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": dest_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200 and clean_previous:
            last_bot_message_id = r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"[Telegram] Error: {e}")

# ==========================================
# 3. EJECUCIÓN ON-CHAIN: JUPITER PERPS
# ==========================================
def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOC_TOKEN_PROGRAM_ID
    )
    return pda

def build_and_send_jup_perp_tx(ix: Instruction, kp: Keypair) -> str:
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "confirmed"}]
    }
    bh_res = requests.post(RPC_URL, json=rpc_payload, timeout=10).json()
    if "result" not in bh_res:
        raise Exception(f"Error obteniendo blockhash: {bh_res}")
        
    latest_blockhash_str = bh_res["result"]["value"]["blockhash"]
    latest_blockhash = Hash.from_string(latest_blockhash_str)

    msg = MessageV0.try_compile(
        payer=kp.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=latest_blockhash,
    )
    tx = VersionedTransaction(msg, [kp])
    
    raw_tx_bytes = bytes(tx)
    encoded_tx = base64.b64encode(raw_tx_bytes).decode('utf-8')
    
    send_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            encoded_tx,
            {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}
        ]
    }
    
    res = requests.post(RPC_URL, json=send_payload, timeout=15).json()
    
    if "error" in res:
        raise Exception(str(res["error"]))
        
    if "result" in res:
        return str(res["result"])
        
    raise Exception(f"Respuesta RPC desconocida: {res}")

def execute_order_onchain(direction: str, entry_price: float):
    """Envía un Market Request (LONG o SHORT) al contrato de Jupiter Perps."""
    if not SOLANA_PRIVATE_KEY:
        send_telegram_alert("⚠️ <b>Error:</b> SOLANA_PRIVATE_KEY no configurada.")
        return None

    try:
        kp = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        owner = kp.pubkey()

        notional_usd = MARGIN_USDC * LEVERAGE
        size_usd_delta = int(notional_usd * 1_000_000)
        collateral_delta = int(MARGIN_USDC * 1_000_000)
        side_byte = 1 if direction == "LONG" else 2

        slippage_price = entry_price * (1.012 if direction == "LONG" else 0.988)
        price_slippage_int = int(slippage_price * 1_000_000)
        counter = int(time.time() * 1000) % 1_000_000_000

        # CORRECCIÓN DE CUSTODIAS PARA SHORT:
        # Para LONG: target = SOL_CUSTODY, collateral = SOL_CUSTODY
        # Para SHORT: target = USDC_CUSTODY, collateral = USDC_CUSTODY
        if direction == "LONG":
            target_custody = SOL_CUSTODY
            collateral_custody = SOL_CUSTODY
        else:
            target_custody = USDC_CUSTODY
            collateral_custody = USDC_CUSTODY

        perpetuals_pda, _ = Pubkey.find_program_address([b"perpetuals"], JUP_PERPS_PROGRAM_ID)
        event_auth_pda, _ = Pubkey.find_program_address([b"__event_authority"], JUP_PERPS_PROGRAM_ID)

        position_pda, _ = Pubkey.find_program_address(
            [
                b"position",
                bytes(owner),
                bytes(JLP_POOL_PUBKEY),
                bytes(target_custody),
                bytes(collateral_custody),
                bytes([side_byte]),
            ],
            JUP_PERPS_PROGRAM_ID,
        )

        position_req_pda, _ = Pubkey.find_program_address(
            [
                b"position_request",
                bytes(position_pda),
                struct.pack("<Q", counter),
                b"\x01",
            ],
            JUP_PERPS_PROGRAM_ID,
        )

        funding_account = get_associated_token_address(owner, USDC_MINT)
        position_req_ata = get_associated_token_address(position_req_pda, USDC_MINT)

        disc = hashlib.sha256(b"global:create_increase_position_market_request").digest()[:8]
        data = (
            disc
            + struct.pack("<QQBQ", size_usd_delta, collateral_delta, side_byte, price_slippage_int)
            + b"\x00"
            + struct.pack("<Q", counter)
        )

        accounts = [
            AccountMeta(owner, is_signer=True, is_writable=True),
            AccountMeta(funding_account, is_signer=False, is_writable=True),
            AccountMeta(perpetuals_pda, is_signer=False, is_writable=False),
            AccountMeta(JLP_POOL_PUBKEY, is_signer=False, is_writable=False),
            AccountMeta(position_pda, is_signer=False, is_writable=True),
            AccountMeta(position_req_pda, is_signer=False, is_writable=True),
            AccountMeta(position_req_ata, is_signer=False, is_writable=True),
            AccountMeta(target_custody, is_signer=False, is_writable=False),
            AccountMeta(collateral_custody, is_signer=False, is_writable=False),
            AccountMeta(USDC_MINT, is_signer=False, is_writable=False),
            AccountMeta(JUP_PERPS_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(ASSOC_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(event_auth_pda, is_signer=False, is_writable=False),
            AccountMeta(JUP_PERPS_PROGRAM_ID, is_signer=False, is_writable=False),
        ]

        ix = Instruction(JUP_PERPS_PROGRAM_ID, data, accounts)
        tx_sig = build_and_send_jup_perp_tx(ix, kp)

        send_telegram_alert(
            f"⚡ <b>ORDEN ENVIADA A JUPITER PERPS</b>\n\n"
            f"Activo: <code>{SYMBOL} ({direction})</code>\n"
            f"Margen: <code>${MARGIN_USDC:.2f} USDC ({LEVERAGE}x)</code>\n"
            f"Tamaño Nocional: <code>${notional_usd:.2f} USD</code>\n"
            f"Precio Entrada: <code>${entry_price:.2f}</code>\n"
            f"Tx Hash: <a href='https://solscan.io/tx/{tx_sig}'>Ver en Solscan</a>"
        )
        return str(position_pda)

    except Exception as e:
        print(f"[Jupiter Perps Error] {e}")
        send_telegram_alert(f"⚠️ <b>Error al abrir en Jupiter Perps:</b>\n<code>{e}</code>")
        return None

    except Exception as e:
        print(f"[Jupiter Perps Error] {e}")
        send_telegram_alert(f"⚠️ <b>Error al abrir en Jupiter Perps:</b>\n<code>{e}</code>")
        return None

def close_position_onchain(active_pos: dict, exit_price: float, reason: str) -> bool:
    try:
        kp = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        owner = kp.pubkey()
        direction = active_pos["type"]
        position_pda = Pubkey.from_string(active_pos["position_pda"])

        collateral_custody = SOL_CUSTODY if direction == "LONG" else USDC_CUSTODY
        target_custody = SOL_CUSTODY if direction == "LONG" else USDC_CUSTODY
        
        slippage_price = exit_price * (0.988 if direction == "LONG" else 1.012)
        price_slippage_int = int(slippage_price * 1_000_000)
        counter = int(time.time() * 1000) % 1_000_000_000

        perpetuals_pda, _ = Pubkey.find_program_address([b"perpetuals"], JUP_PERPS_PROGRAM_ID)
        event_auth_pda, _ = Pubkey.find_program_address([b"__event_authority"], JUP_PERPS_PROGRAM_ID)

        position_req_pda, _ = Pubkey.find_program_address(
            [
                b"position_request",
                bytes(position_pda),
                struct.pack("<Q", counter),
                b"\x02",
            ],
            JUP_PERPS_PROGRAM_ID,
        )

        receiving_account = get_associated_token_address(owner, USDC_MINT)
        position_req_ata = get_associated_token_address(position_req_pda, USDC_MINT)

        disc = hashlib.sha256(b"global:create_decrease_position_market_request").digest()[:8]
        data = (
            disc
            + struct.pack("<QQQ", 0, 0, price_slippage_int)
            + b"\x00\x01\x01"
            + struct.pack("<Q", counter)
        )

        accounts = [
            AccountMeta(owner, is_signer=True, is_writable=True),
            AccountMeta(receiving_account, is_signer=False, is_writable=True),
            AccountMeta(perpetuals_pda, is_signer=False, is_writable=False),
            AccountMeta(JLP_POOL_PUBKEY, is_signer=False, is_writable=False),
            AccountMeta(position_pda, is_signer=False, is_writable=False),
            AccountMeta(position_req_pda, is_signer=False, is_writable=True),
            AccountMeta(position_req_ata, is_signer=False, is_writable=True),
            AccountMeta(target_custody, is_signer=False, is_writable=False),
            AccountMeta(collateral_custody, is_signer=False, is_writable=False),
            AccountMeta(USDC_MINT, is_signer=False, is_writable=False),
            AccountMeta(JUP_PERPS_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(ASSOC_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(event_auth_pda, is_signer=False, is_writable=False),
            AccountMeta(JUP_PERPS_PROGRAM_ID, is_signer=False, is_writable=False),
        ]

        ix = Instruction(JUP_PERPS_PROGRAM_ID, data, accounts)
        tx_sig = build_and_send_jup_perp_tx(ix, kp)

        send_telegram_alert(
            f"{reason}\n\n"
            f"Par: <b>{SYMBOL}</b> ({direction})\n"
            f"Entrada: <code>${active_pos['entry']:.2f}</code>\n"
            f"Salida: <code>${exit_price:.2f}</code>\n"
            f"Tx Cierre: <a href='https://solscan.io/tx/{tx_sig}'>Ver en Solscan</a>"
        )
        return True

    except Exception as e:
        print(f"[Jupiter Close Error] {e}")
        send_telegram_alert(f"🚨 <b>Error al cerrar posición:</b>\n<code>{e}</code>")
        return False

# ==========================================
# 4. FEED DE MERCADO Y CÁLCULO TÉCNICO
# ==========================================
def get_live_sol_price():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
        res = requests.get(url, headers=headers, timeout=5).json()
        return float(res["data"]["amount"])
    except Exception:
        pass

    try:
        url_jup = "https://price.jup.ag/v6/price?ids=SOL"
        res = requests.get(url_jup, headers=headers, timeout=4).json()
        return float(res["data"]["SOL"]["price"])
    except Exception:
        return None

def fetch_candles_15m():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = "https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=900"
        res = requests.get(url, headers=headers, timeout=10).json()
        if not isinstance(res, list) or len(res) < 200:
            return None

        df = pd.DataFrame(res, columns=["time", "low", "high", "open", "close", "volume"])
        df = df.sort_values(by="time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[Mercado] Error velas: {e}")
        return None

def calculate_technical_indicators(df):
    close = df["close"]
    df["ema_200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi_14"] = 100 - (100 / (1 + rs))

    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    return df

# ==========================================
# 5. GESTIÓN AUTÓNOMA: BREAK-EVEN Y CIERRES
# ==========================================
def manage_open_position(current_price):
    global active_trade
    if not active_trade:
        return

    direction = active_trade["type"]
    entry = active_trade["entry"]
    tp = active_trade["tp"]
    sl = active_trade["sl"]
    be_active = active_trade["be_activated"]

    if direction == "LONG":
        if not be_active and current_price >= entry * (1 + BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = round(entry * (1 + BE_OFFSET_PCT), 2)
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN ACTIVADO · LONG</b>\n\n"
                f"Precio actual: <code>${current_price:.2f}</code> (+2.0%)\n"
                f"Stop Loss subido a: <code>${active_trade['sl']:.2f}</code> (+0.3% para cubrir fees)"
            )
        elif current_price >= tp:
            if close_position_onchain(active_trade, current_price, "🎯 <b>TAKE PROFIT EJECUTADO (+3.0%)</b>"):
                active_trade = None
        elif current_price <= sl:
            if close_position_onchain(active_trade, current_price, "🛑 <b>STOP LOSS EJECUTADO</b>"):
                active_trade = None

    elif direction == "SHORT":
        if not be_active and current_price <= entry * (1 - BE_TRIGGER_PCT):
            active_trade["be_activated"] = True
            active_trade["sl"] = round(entry * (1 - BE_OFFSET_PCT), 2)
            send_telegram_alert(
                f"🛡️ <b>BREAK-EVEN ACTIVADO · SHORT</b>\n\n"
                f"Precio actual: <code>${current_price:.2f}</code> (+2.0%)\n"
                f"Stop Loss bajado a: <code>${active_trade['sl']:.2f}</code> (+0.3% para cubrir fees)"
            )
        elif current_price <= tp:
            if close_position_onchain(active_trade, current_price, "🎯 <b>TAKE PROFIT EJECUTADO (+3.0%)</b>"):
                active_trade = None
        elif current_price >= sl:
            if close_position_onchain(active_trade, current_price, "🛑 <b>STOP LOSS EJECUTADO</b>"):
                active_trade = None

# ==========================================
# 6. LISTENER TELEGRAM (COMANDOS)
# ==========================================
def telegram_listener():
    global active_trade
    offset = None
    print("[Telegram] Listener activo.")

    while True:
        token = TELEGRAM_BOT_TOKEN
        if not token:
            time.sleep(5)
            continue

        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"timeout": 10}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    msg_id = msg.get("message_id")
                    text = msg.get("text", "").strip().lower()

                    if msg_id and (text.startswith("/") or text in ["precio", "status", "reset", "close", "test", "testlong", "testshort"]):
                        delete_telegram_message(chat_id, msg_id)

                    if text.startswith("/precio") or text == "precio":
                        p = latest_market_data.get("price") or get_live_sol_price()
                        ema = latest_market_data.get("ema")
                        rsi = latest_market_data.get("rsi")
                        if p:
                            ema_str = f"${ema:.2f}" if ema else "Sincronizando..."
                            rsi_str = f"{rsi:.1f}" if rsi else "Sincronizando..."
                            trend = "\nSesgo: 🟢 LONG Bias" if (ema and p > ema) else "\nSesgo: 🔴 SHORT Bias"
                            resp_text = (
                                "📊 <b>SOL-PERP (JUPITER) · EN VIVO</b>\n\n"
                                f"Precio: <code>${p:.2f}</code>\n"
                                f"EMA 200: <code>{ema_str}</code>\n"
                                f"RSI (14): <code>{rsi_str}</code>"
                                f"{trend}"
                            )
                        else:
                            resp_text = "⏳ Sincronizando precio..."
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/status") or text == "status":
                        pos = f"{active_trade['type']} @ ${active_trade['entry']:.2f}" if active_trade else "Ninguna (Escaneando)"
                        status_wallet = f"{BOT_WALLET_PUBKEY[:6]}...{BOT_WALLET_PUBKEY[-4:]}"
                        resp_text = (
                            "⚡ <b>ESTADO JUPITER PERPS BOT</b>\n\n"
                            f"Wallet: <code>{status_wallet}</code>\n"
                            f"Margen: <code>${MARGIN_USDC:.2f} USDC ({LEVERAGE}x = ${MARGIN_USDC*LEVERAGE:.2f})</code>\n"
                            f"Velas escaneadas: {candles_scanned_count}\n"
                            f"Posición activa: <code>{pos}</code>"
                        )
                        send_telegram_alert(resp_text, target_chat_id=chat_id, clean_previous=True)

                    elif text in ["/test", "/testlong", "test"]:
                        if active_trade:
                            send_telegram_alert("⚠️ Ya hay una posición activa. Ciérrala primero con /close antes de hacer un test.", target_chat_id=chat_id, clean_previous=True)
                        else:
                            p = latest_market_data.get("price") or get_live_sol_price()
                            if p:
                                send_telegram_alert("🧪 <b>Ejecutando TEST LONG manual en Jupiter Perps...</b>", target_chat_id=chat_id, clean_previous=True)
                                tp = round(p * (1 + TP_PCT), 2)
                                sl = round(p * (1 - SL_PCT), 2)
                                pos_pda = execute_order_onchain("LONG", p)
                                if pos_pda:
                                    active_trade = {
                                        "type": "LONG",
                                        "entry": p,
                                        "tp": tp,
                                        "sl": sl,
                                        "position_pda": pos_pda,
                                        "be_activated": False
                                    }
                            else:
                                send_telegram_alert("❌ Error obteniendo precio en vivo para el test.", target_chat_id=chat_id, clean_previous=True)

                    elif text == "/testshort":
                        if active_trade:
                            send_telegram_alert("⚠️ Ya hay una posición activa. Ciérrala primero con /close.", target_chat_id=chat_id, clean_previous=True)
                        else:
                            p = latest_market_data.get("price") or get_live_sol_price()
                            if p:
                                send_telegram_alert("🧪 <b>Ejecutando TEST SHORT manual en Jupiter Perps...</b>", target_chat_id=chat_id, clean_previous=True)
                                tp = round(p * (1 - TP_PCT), 2)
                                sl = round(p * (1 + SL_PCT), 2)
                                pos_pda = execute_order_onchain("SHORT", p)
                                if pos_pda:
                                    active_trade = {
                                        "type": "SHORT",
                                        "entry": p,
                                        "tp": tp,
                                        "sl": sl,
                                        "position_pda": pos_pda,
                                        "be_activated": False
                                    }
                            else:
                                send_telegram_alert("❌ Error obteniendo precio en vivo.", target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/close") or text == "close":
                        if active_trade:
                            p = latest_market_data.get("price") or get_live_sol_price() or active_trade["entry"]
                            if close_position_onchain(active_trade, p, "⚠️ <b>CIERRE MANUAL POR TELEGRAM</b>"):
                                active_trade = None
                        else:
                            send_telegram_alert("ℹ️ No hay ninguna posición abierta para cerrar.", target_chat_id=chat_id, clean_previous=True)

                    elif text.startswith("/reset") or text == "reset":
                        active_trade = None
                        send_telegram_alert("🔄 Estado en memoria reiniciado.", target_chat_id=chat_id, clean_previous=True)

        except Exception:
            time.sleep(5)

        time.sleep(1)

# ==========================================
# 7. MOTOR PRINCIPAL DE MERCADO
# ==========================================
def market_engine():
    global candles_scanned_count, latest_market_data, active_trade
    last_evaluated_time = None
    time.sleep(3)

    wallet_desc = f"<code>{BOT_WALLET_PUBKEY[:6]}...{BOT_WALLET_PUBKEY[-4:]}</code>"
    send_telegram_alert(
        f"🤖 <b>JUPITER PERPS BOT CONECTADO</b>\n\n"
        f"Mercado: <b>{SYMBOL} (15m)</b>\n"
        f"Wallet Phantom: {wallet_desc}\n"
        f"Posición por trade: <code>${MARGIN_USDC:.2f} USDC a {LEVERAGE}x (${MARGIN_USDC*LEVERAGE:.2f} USD)</code>\n"
        f"Comandos: <code>/precio</code> | <code>/status</code> | <code>/test</code> | <code>/testshort</code> | <code>/close</code>",
        clean_previous=True
    )

    while True:
        try:
            live_price = get_live_sol_price()
            if live_price:
                latest_market_data["price"] = live_price
                manage_open_position(live_price)

            df = fetch_candles_15m()
            if df is not None and len(df) >= 200:
                df = calculate_technical_indicators(df)

                closed_candle = df.iloc[-2]
                candle_time = closed_candle["time"]

                latest_market_data["ema"] = float(closed_candle["ema_200"])
                latest_market_data["rsi"] = float(closed_candle["rsi_14"])

                if candle_time != last_evaluated_time:
                    last_evaluated_time = candle_time
                    candles_scanned_count += 1

                    close_p = float(closed_candle["close"])
                    ema_val = float(closed_candle["ema_200"])
                    rsi_val = float(closed_candle["rsi_14"])
                    vol_val = float(closed_candle["volume"])
                    vol_avg = float(closed_candle["vol_ma20"])
                    prev_rsi = float(df.iloc[-3]["rsi_14"])

                    if not active_trade:
                        if (close_p > ema_val) and (prev_rsi < 42 and rsi_val >= 42) and (vol_val > vol_avg):
                            entry = live_price if live_price else close_p
                            tp = round(entry * (1 + TP_PCT), 2)
                            sl = round(entry * (1 - SL_PCT), 2)

                            pos_pda = execute_order_onchain("LONG", entry)
                            if pos_pda:
                                active_trade = {
                                    "type": "LONG",
                                    "entry": entry,
                                    "tp": tp,
                                    "sl": sl,
                                    "position_pda": pos_pda,
                                    "be_activated": False
                                }

                        elif (close_p < ema_val) and (prev_rsi > 58 and rsi_val <= 58) and (vol_val > vol_avg):
                            entry = live_price if live_price else close_p
                            tp = round(entry * (1 - TP_PCT), 2)
                            sl = round(entry * (1 + SL_PCT), 2)

                            pos_pda = execute_order_onchain("SHORT", entry)
                            if pos_pda:
                                active_trade = {
                                    "type": "SHORT",
                                    "entry": entry,
                                    "tp": tp,
                                    "sl": sl,
                                    "position_pda": pos_pda,
                                    "be_activated": False
                                }

        except Exception as e:
            print(f"[Engine] Error en ciclo: {e}")

        time.sleep(30)

# ==========================================
# 8. ARRANQUE SEGURO EN RENDER Y SERVIDOR WEB
# ==========================================
def start_background_workers():
    global threads_started
    if not threads_started:
        threads_started = True
        t_market = threading.Thread(target=market_engine, daemon=True)
        t_market.start()
        t_telegram = threading.Thread(target=telegram_listener, daemon=True)
        t_telegram.start()

start_background_workers()

@app.route("/")
def home():
    return "Jupiter Perps Bot Activo 24/7", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
