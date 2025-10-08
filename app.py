# ===============================================================
# VICKY CAMPAÑAS EN REDES – APP PRINCIPAL
# Integración completa con Meta Cloud API (WhatsApp Business)
# Flujo activo: Préstamos IMSS Ley 73
# Autor: Christian López | GPT-5
# ===============================================================

import os
import json
import logging
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

# ---------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")

# ---------------------------------------------------------------
# Configuración de logging
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------------------------------------------------------
# Inicialización de Flask
# ---------------------------------------------------------------
app = Flask(__name__)

# Diccionarios temporales para gestionar el estado de cada usuario
user_state = {}
user_data = {}

# ---------------------------------------------------------------
# Función: enviar mensaje por WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------
def send_message(to, text):
    """Envía mensajes de texto al usuario vía Meta Cloud API."""
    try:
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
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in (200, 201):
            logging.warning(f"⚠️ Error al enviar mensaje: {response.text}")
        else:
            logging.info(f"📩 Mensaje enviado correctamente a {to}")
    except Exception as e:
        logging.exception(f"❌ Error en send_message: {e}")

# ---------------------------------------------------------------
# Función auxiliar: extraer número de texto
# ---------------------------------------------------------------
def extract_number(text):
    """Extrae el primer número encontrado dentro del texto."""
    if not text:
        return None
    clean = text.replace(',', '').replace('$', '')
    match = re.search(r'(\d{2,7})(?:\.\d+)?', clean)
    return float(match.group(1)) if match else None

# ---------------------------------------------------------------
# Función: interpretar respuesta sí/no
# ---------------------------------------------------------------
def interpret_response(text):
    """Interpreta respuestas afirmativas/negativas."""
    text_lower = (text or '').lower()
    positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes']
    negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not']
    if any(k in text_lower for k in positive_keywords):
        return 'positive'
    if any(k in text_lower for k in negative_keywords):
        return 'negative'
    return 'neutral'

# ---------------------------------------------------------------
# Menú principal (para usuarios no elegibles o reinicio)
# ---------------------------------------------------------------
def send_main_menu(phone):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Ley 73\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el número del servicio que te interese 👇"
    )
    send_message(phone, menu)

# ---------------------------------------------------------------
# Función: manejar comando menu
# ---------------------------------------------------------------
def handle_menu_command(phone_number):
    """Maneja el comando menu para reiniciar la conversación"""
    user_state.pop(phone_number, None)
    user_data.pop(phone_number, None)
    
    menu_text = (
        "🔄 Conversación reiniciada\n\n"
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Ley 73\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el número o el nombre del servicio que te interesa:"
    )
    send_message(phone_number, menu_text)

# ---------------------------------------------------------------
# BLOQUE PRINCIPAL: FLUJO PRÉSTAMO IMSS LEY 73
# ---------------------------------------------------------------
def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    # Detección mejorada de palabras clave IMSS
    imss_keywords = ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73", "1", "5"]
    
    # Paso 1: activación inicial por palabras clave o números
    if any(keyword in msg for keyword in imss_keywords) or extract_number(msg) is not None:
        # Si ya está en flujo IMSS, no reiniciar
        if user_state.get(phone_number) not in ["esperando_respuesta_imss", "esperando_monto_pension", 
                                              "esperando_monto_solicitado", "esperando_respuesta_nomina"]:
            send_message(phone_number,
                "👋 ¡Hola! Antes de continuar, necesito confirmar algo importante.\n\n"
                "¿Eres pensionado o jubilado del IMSS bajo la Ley 73? (Responde *sí* o *no*)"
            )
            user_state[phone_number] = "esperando_respuesta_imss"
        return True

    # Paso 2: validación de respuesta IMSS
    if user_state.get(phone_number) == "esperando_respuesta_imss":
        intent = interpret_response(msg)
        if intent == 'negative':
            send_message(phone_number,
                "Entiendo. Para el préstamo IMSS Ley 73 es necesario ser pensionado del IMSS. 😔\n\n"
                "Pero tengo otros servicios que pueden interesarte:"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
        elif intent == 'positive':
            send_message(phone_number,
                "Excelente 👏\n\n¿Cuánto recibes al mes por concepto de pensión?"
            )
            user_state[phone_number] = "esperando_monto_pension"
        else:
            send_message(phone_number, "Por favor responde *sí* o *no* para continuar.")
        return True

    # Paso 3: monto de pensión
    if user_state.get(phone_number) == "esperando_monto_pension":
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

    # Paso 4: monto solicitado
    if user_state.get(phone_number) == "esperando_monto_solicitado":
        monto = extract_number(msg)
        if monto:
            if monto < 40000:
                send_message(phone_number,
                    "Por el momento el monto mínimo para aplicar al préstamo es de $40,000 MXN. 💵\n\n"
                    "Si deseas solicitar una cantidad mayor, puedo continuar con tu registro ✅\n"
                    "O si prefieres, puedo mostrarte otras opciones que podrían interesarte:"
                )
                send_main_menu(phone_number)
                user_state.pop(phone_number, None)
            else:
                user_data[phone_number]["monto_solicitado"] = monto
                send_message(phone_number,
                    "Excelente, cumples con los requisitos iniciales 👏\n\n"
                    "Para recibir los beneficios del préstamo y obtener mejores condiciones, necesito confirmar un último punto:"
                )
                send_message(phone_number,
                    "💳 ¿Tienes tu pensión depositada en Inbursa o estarías dispuesto a cambiarla?\n\n"
                    "👉 No necesitas cancelar tu cuenta actual y puedes regresar después de tres meses si no estás conforme.\n\n"
                    "Responde *sí* o *no*:"
                )
                user_state[phone_number] = "esperando_respuesta_nomina"
        else:
            send_message(phone_number, "Por favor indica el monto deseado, ejemplo: 65000")
        return True

    # Paso 5: validación nómina y beneficios
    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        intent = interpret_response(msg)
        if intent == 'positive':
            send_message(phone_number,
                "🌟 ¡Excelente! Cambiar tu nómina a Inbursa te da acceso a beneficios exclusivos:"
            )
            send_message(phone_number,
                "💰 Rendimientos del 80 % de Cetes\n"
                "💵 Préstamos hasta 12 meses de tu pensión\n"
                "♻️ Devolución del 20 % de intereses por pago puntual\n"
                "🎁 Anticipo de nómina hasta el 50 %\n"
                "🏥 Seguro de vida y Medicall Home (telemedicina 24/7, ambulancia sin costo, asistencia funeraria)\n"
                "💳 Descuentos en Sanborns y 6 000 comercios\n"
                "🏦 Retiros y depósitos *sin comisión* en más de 28 000 puntos (Inbursa, Afirme, Walmart, HSBC, Scotiabank, Mifel, Banregio, BanBajío)\n\n"
                "👉 En breve un asesor se comunicará contigo para continuar tu trámite."
            )

            data = user_data.get(phone_number, {})
            mensaje_asesor = (
                f"🔥 NUEVO PROSPECTO IMSS LEY 73\n\n"
                f"📞 Número: {phone_number}\n"
                f"💰 Pensión mensual: ${data.get('pension_mensual', 'N/D'):,.0f}\n"
                f"💵 Monto solicitado: ${data.get('monto_solicitado', 'N/D'):,.0f}\n"
                f"🏦 Acepta cambiar nómina a Inbursa ✅"
            )
            send_message(ADVISOR_NUMBER, mensaje_asesor)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
        elif intent == 'negative':
            send_message(phone_number,
                "Entiendo, sin cambiar la nómina no es posible acceder al préstamo IMSS Ley 73. 😔\n\n"
                "Pero puedo mostrarte otros productos que pueden interesarte:"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
        else:
            send_message(phone_number, "Por favor responde *sí* o *no* para continuar.")
        return True

    return False

# ---------------------------------------------------------------
# Endpoint de verificación de Meta Webhook
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    logging.warning("❌ Verificación de webhook fallida.")
    return "Forbidden", 403

# ---------------------------------------------------------------
# Endpoint principal para recepción de mensajes
# ---------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json()
        logging.info(f"📩 Datos recibidos: {json.dumps(data, ensure_ascii=False)}")

        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")

        if message_type == "text":
            user_message = message["text"]["body"].strip()

            # ✅ MANEJO DE COMANDO MENU
            if user_message.lower() == "menu":
                handle_menu_command(phone_number)
                return jsonify({"status": "ok"}), 200

            # Procesar flujo IMSS
            if handle_imss_flow(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            # ✅ MEJOR RESPUESTA PARA MENSAJES NO RECONOCIDOS
            send_message(phone_number,
                "👋 Hola, soy *Vicky*, tu asistente virtual de Inbursa.\n\n"
                "Puedo ayudarte con:\n"
                "• 📋 **Préstamos IMSS** (escribe 'préstamo' o '1')\n"  
                "• 🚗 **Seguros de Auto** ('seguro auto' o '2')\n"
                "• 🏥 **Seguros de Vida y Salud** ('seguro vida' o '3')\n"
                "• 💳 **Tarjetas Médicas VRIM** ('vrim' o '4')\n"
                "• 🏢 **Financiamiento Empresarial** ('empresa' o '5')\n\n"
                "También puedes escribir *menu* en cualquier momento para ver todas las opciones organizadas."
            )
            return jsonify({"status": "ok"}), 200

        else:
            send_message(phone_number, 
                "Por ahora solo puedo procesar mensajes de texto 📩\n\n"
                "Escribe *menu* para ver los servicios disponibles."
            )
            return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# Endpoint de salud
# ---------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

# ---------------------------------------------------------------
# Ejecución principal
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
