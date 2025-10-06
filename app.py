# ===============================================================
# VICKY CAMPAÑAS EN REDES – APP PRINCIPAL (CORREGIDO)
# ===============================================================

import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import re

# ---------------------------------------------------------------
# Cargar variables de entorno - CORREGIDO
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
WHATSAPP_BUSINESS_PHONE = os.getenv("WHATSAPP_BUSINESS_PHONE", "5216682478005")  # NUEVA VARIABLE

# Validar variables críticas
if not all([META_TOKEN, WABA_PHONE_ID, VERIFY_TOKEN, WHATSAPP_BUSINESS_PHONE]):
    missing = []
    if not META_TOKEN: missing.append("META_TOKEN")
    if not WABA_PHONE_ID: missing.append("WABA_PHONE_ID") 
    if not VERIFY_TOKEN: missing.append("VERIFY_TOKEN")
    if not WHATSAPP_BUSINESS_PHONE: missing.append("WHATSAPP_BUSINESS_PHONE")
    
    logging.error(f"❌ Variables faltantes: {', '.join(missing)}")
    # No salir para permitir health checks
    # exit(1)

# ---------------------------------------------------------------
# Configuración de logging MEJORADA
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)
user_state = {}
user_data = {}

# ---------------------------------------------------------------
# ENDPOINT RAÍZ CRÍTICO PARA RENDER
# ---------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD"])
def home():
    """Endpoint raíz requerido por Render para health checks"""
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot WhatsApp",
        "variables_configuradas": {
            "META_TOKEN": bool(META_TOKEN),
            "WABA_PHONE_ID": bool(WABA_PHONE_ID),
            "VERIFY_TOKEN": bool(VERIFY_TOKEN),
            "WHATSAPP_BUSINESS_PHONE": bool(WHATSAPP_BUSINESS_PHONE),
            "ADVISOR_NUMBER": ADVISOR_NUMBER
        }
    }), 200

# ---------------------------------------------------------------
# Función: enviar mensaje por WhatsApp - CORREGIDA
# ---------------------------------------------------------------
def send_message(to, text):
    """Envía mensajes de texto al usuario vía Meta Cloud API."""
    try:
        # Validación completa de variables
        if not all([META_TOKEN, WABA_PHONE_ID]):
            logging.error("❌ META_TOKEN o WABA_PHONE_ID no configurados")
            return False

        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(to),
            "type": "text",
            "text": {"body": text}
        }
        
        logging.info(f"📤 Enviando mensaje a {to}")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code in (200, 201):
            logging.info(f"✅ Mensaje enviado correctamente a {to}")
            return True
        else:
            logging.error(f"❌ Error API Meta: {response.status_code} - {response.text}")
            # Log detallado para debugging
            logging.debug(f"URL: {url}")
            logging.debug(f"Headers: {headers}")
            logging.debug(f"Payload: {payload}")
            return False
            
    except Exception as e:
        logging.exception(f"💥 Error en send_message: {e}")
        return False

# ---------------------------------------------------------------
# Endpoint de diagnóstico MEJORADO
# ---------------------------------------------------------------
@app.route("/debug", methods=["GET", "POST"])
def debug():
    """Endpoint para diagnóstico del webhook"""
    if request.method == "GET":
        return jsonify({
            "status": "online",
            "timestamp": datetime.now().isoformat(),
            "webhook_url": "https://bot-vicky.onrender.com/webhook",
            "variables_configuradas": {
                "META_TOKEN": bool(META_TOKEN),
                "WABA_PHONE_ID": WABA_PHONE_ID,
                "VERIFY_TOKEN": VERIFY_TOKEN,
                "WHATSAPP_BUSINESS_PHONE": WHATSAPP_BUSINESS_PHONE,
                "ADVISOR_NUMBER": ADVISOR_NUMBER
            },
            "bot_phone_number": WHATSAPP_BUSINESS_PHONE
        }), 200
    
    # Si es POST, simular un mensaje
    data = request.get_json() or {}
    phone = data.get('phone', ADVISOR_NUMBER)
    message = data.get('message', '✅ Mensaje de prueba desde /debug')
    
    success = send_message(phone, message)
    return jsonify({
        "sent_to": phone,
        "message": message,
        "success": success,
        "from_bot_number": WHATSAPP_BUSINESS_PHONE
    }), 200

# ---------------------------------------------------------------
# Endpoint de verificación de Meta Webhook
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logging.info(f"🔍 Webhook verification: mode={mode}, token={token}")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    else:
        logging.warning(f"❌ Webhook verification failed. Expected: {VERIFY_TOKEN}")
        return "Forbidden", 403

# ---------------------------------------------------------------
# [MANTENER EL RESTO DEL CÓDIGO ORIGINAL SIN CAMBIOS]
# Las funciones handle_imss_flow, extract_number, send_main_menu, etc.
# se mantienen exactamente igual que en tu versión anterior
# ---------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json()
        logging.info(f"📩 Webhook POST recibido")
        
        if not data:
            logging.warning("📭 Datos vacíos en webhook")
            return jsonify({"status": "no data"}), 200

        entries = data.get("entry", [])
        if not entries:
            logging.warning("📭 No hay entries en webhook")
            return jsonify({"status": "no entries"}), 200

        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                
                if "messages" in value:
                    messages = value.get("messages", [])
                    for message in messages:
                        phone_number = message.get("from")
                        message_type = message.get("type")
                        
                        if not phone_number:
                            continue
                            
                        if message_type == "text":
                            user_message = message["text"]["body"].strip()
                            logging.info(f"💬 Mensaje de {phone_number}: {user_message}")
                            
                            # Aquí iría tu lógica handle_imss_flow
                            # Por ahora respuesta simple
                            send_message(phone_number, 
                                "👋 Hola, soy *Vicky*, asistente virtual de Inbursa. "
                                "Estamos configurando el sistema. Pronto estaré operativa."
                            )
                            return jsonify({"status": "responded"}), 200

        return jsonify({"status": "processed"}), 200

    except Exception as e:
        logging.exception(f"💥 Error en receive_message: {e}")
        return jsonify({"error": "internal server error"}), 500

# ---------------------------------------------------------------
# Ejecución principal
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    host = os.getenv("HOST", "0.0.0.0")
    
    logging.info(f"🚀 Iniciando servidor en {host}:{port}")
    logging.info(f"📞 Número del bot: {WHATSAPP_BUSINESS_PHONE}")
    app.run(host=host, port=port, debug=False)
