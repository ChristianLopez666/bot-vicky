# app.py - Bot Vicky para WhatsApp Business
# Webhook: https://bot-vicky.onrender.com/webhook

import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import re

# ===============================================================
# CONFIGURACIÓN INICIAL
# ===============================================================

load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)

# Almacenamiento en memoria
user_state = {}
user_data = {}

# ===============================================================
# FUNCIONES PRINCIPALES
# ===============================================================

def send_message(to, text):
    """Envía mensajes de texto por WhatsApp Business API"""
    try:
        clean_number = re.sub(r'[^\d+]', '', to)
        
        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_number,
            "type": "text",
            "text": {"body": text}
        }
        
        logging.info(f"📤 Enviando mensaje a {clean_number}")
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code in [200, 201]:
            logging.info(f"✅ Mensaje enviado a {clean_number}")
            return True
        else:
            logging.error(f"❌ Error {response.status_code}: {response.text}")
            return False
            
    except Exception as e:
        logging.exception(f"🚨 Error en send_message: {e}")
        return False

def extract_number(text):
    """Extrae números del texto del usuario"""
    try:
        match = re.search(r"\d+", text.replace(",", "").replace(".", ""))
        return int(match.group()) if match else None
    except:
        return None

def send_main_menu(phone):
    """Envía el menú principal de servicios"""
    menu = (
        "📋 *Otros servicios disponibles:*\n"
        "1️⃣ Seguros de Auto\n"
        "2️⃣ Seguros de Vida y Salud\n"
        "3️⃣ Tarjetas Médicas VRIM\n"
        "4️⃣ Financiamiento Empresarial\n"
        "5️⃣ Préstamos Personales\n\n"
        "Escribe el número del servicio que te interese 👇"
    )
    return send_message(phone, menu)

# ===============================================================
# FLUJO PRÉSTAMO IMSS LEY 73
# ===============================================================

def handle_imss_flow(phone_number, user_message):
    """Maneja el flujo completo del préstamo IMSS"""
    try:
        msg = user_message.lower().strip()
        current_state = user_state.get(phone_number, "")
        
        logging.info(f"🔍 Estado actual de {phone_number}: {current_state}")

        # Activación inicial
        if any(x in msg for x in ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73"]):
            send_message(phone_number,
                "👋 ¡Hola! Antes de continuar, necesito confirmar algo importante.\n\n"
                "¿Eres pensionado o jubilado del IMSS bajo la Ley 73? (Responde *sí* o *no*)"
            )
            user_state[phone_number] = "esperando_respuesta_imss"
            return True

        # Validación de respuesta IMSS
        if current_state == "esperando_respuesta_imss":
            if "no" in msg:
                send_message(phone_number,
                    "Desafortunadamente no eres prospecto para este tipo de préstamo. 😔\n\n"
                    "Pero tengo otros servicios que pueden interesarte 👇"
                )
                send_main_menu(phone_number)
                user_state.pop(phone_number, None)
            elif "sí" in msg or "si" in msg:
                send_message(phone_number,
                    "Excelente 👏\n\n¿Cuánto recibes al mes por concepto de pensión?"
                )
                user_state[phone_number] = "esperando_monto_pension"
            else:
                send_message(phone_number, "Por favor responde *sí* o *no*.")
            return True

        # Monto de pensión
        if current_state == "esperando_monto_pension":
            pension_monto = extract_number(msg)
            if pension_monto:
                user_data[phone_number] = {"pension_mensual": pension_monto}
                send_message(phone_number,
                    "Perfecto 💰\n\n¿Qué monto deseas solicitar? (El mínimo es de $40,000 MXN)"
                )
                user_state[phone_number] = "esperando_monto_solicitado"
            else:
                send_message(phone_number, "Por favor ingresa una cantidad válida, ejemplo: 8500")
            return True

        # Monto solicitado
        if current_state == "esperando_monto_solicitado":
            monto = extract_number(msg)
            if monto:
                if monto < 40000:
                    send_message(phone_number,
                        "Por el momento el monto mínimo es de $40,000 MXN. 💵\n\n"
                        "Si deseas solicitar una cantidad mayor, puedo continuar con tu registro ✅"
                    )
                    send_main_menu(phone_number)
                    user_state.pop(phone_number, None)
                else:
                    user_data[phone_number]["monto_solicitado"] = monto
                    send_message(phone_number,
                        "Excelente, cumples con los requisitos iniciales 👏\n\n"
                        "Para recibir los beneficios del préstamo y obtener mejores condiciones:"
                    )
                    send_message(phone_number,
                        "💳 ¿Tienes tu pensión depositada en Inbursa o estarías dispuesto a cambiarla?\n\n"
                        "👉 No necesitas cancelar tu cuenta actual y puedes regresar después de tres meses."
                    )
                    user_state[phone_number] = "esperando_respuesta_nomina"
            else:
                send_message(phone_number, "Por favor indica el monto deseado, ejemplo: 65000")
            return True

        # Validación nómina
        if current_state == "esperando_respuesta_nomina":
            if any(x in msg for x in ["sí", "si", "dispuesto", "ok", "vale", "claro"]):
                send_message(phone_number,
                    "🌟 ¡Excelente! Cambiar tu nómina a Inbursa te da acceso a beneficios exclusivos:"
                )
                send_message(phone_number,
                    "💰 Rendimientos del 80% de Cetes\n"
                    "💵 Préstamos hasta 12 meses de tu pensión\n"
                    "♻️ Devolución del 20% de intereses\n"
                    "🎁 Anticipo de nómina hasta 50%\n"
                    "🏥 Seguro de vida y Medicall Home\n"
                    "💳 Descuentos en Sanborns y 6,000 comercios\n\n"
                    "👉 En breve un asesor se comunicará contigo."
                )

                # Notificar al asesor
                data = user_data.get(phone_number, {})
                mensaje_asesor = (
                    f"📢 *Nuevo prospecto IMSS Ley 73*\n\n"
                    f"📞 Número: {phone_number}\n"
                    f"💰 Pensión mensual: ${data.get('pension_mensual', 'N/D')}\n"
                    f"💵 Monto solicitado: ${data.get('monto_solicitado', 'N/D')}\n"
                    f"🏦 Acepta cambiar nómina ✅"
                )
                send_message(ADVISOR_NUMBER, mensaje_asesor)
                
                # Limpiar estado
                user_state.pop(phone_number, None)
                user_data.pop(phone_number, None)
            else:
                send_message(phone_number,
                    "Entiendo, sin cambiar la nómina no es posible acceder al préstamo IMSS. 😔\n\n"
                    "Pero puedo mostrarte otros productos 👇"
                )
                send_main_menu(phone_number)
                user_state.pop(phone_number, None)
            return True

        return False
        
    except Exception as e:
        logging.exception(f"❌ Error en handle_imss_flow: {e}")
        return False

# ===============================================================
# ENDPOINTS WEBHOOK
# ===============================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook por Meta"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logging.info(f"🔐 Verificación webhook: mode={mode}, token={token}")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logging.warning("❌ Verificación fallida")
        return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    """Recibe mensajes de WhatsApp"""
    try:
        data = request.get_json()
        logging.info(f"📩 Mensaje recibido: {json.dumps(data, indent=2)}")

        if not data or "entry" not in data:
            return jsonify({"status": "invalid structure"}), 200

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                
                if "messages" in value:
                    messages = value.get("messages", [])
                    for message in messages:
                        phone_number = message.get("from")
                        message_type = message.get("type")
                        
                        if message_type == "text":
                            user_message = message["text"]["body"].strip()
                            logging.info(f"💬 Mensaje de {phone_number}: {user_message}")

                            # Comando menú
                            if "menú" in user_message.lower():
                                send_main_menu(phone_number)
                                continue

                            # Flujo IMSS
                            if handle_imss_flow(phone_number, user_message):
                                continue

                            # Mensaje inicial por defecto
                            send_message(phone_number,
                                "👋 Hola, soy *Vicky*, asistente virtual de Inbursa.\n"
                                "Te puedo ayudar con préstamos, seguros o tarjetas médicas.\n\n"
                                "Escribe *préstamo IMSS* si eres pensionado o *menú* para ver todas las opciones."
                            )

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": "internal server error"}), 500

@app.route("/health", methods=["GET"])
def health():
    """Endpoint de salud"""
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot",
        "webhook": "https://bot-vicky.onrender.com/webhook",
        "timestamp": str(datetime.now())
    }), 200

# ===============================================================
# INICIALIZACIÓN
# ===============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
