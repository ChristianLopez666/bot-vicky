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
            logging.warning(f"⚠️ Error al enviar mensaje: {response.status_code} - {response.text}")
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
# Menú principal (para usuarios no elegibles)
# ---------------------------------------------------------------
def send_main_menu(phone):
    menu = (
        "📋 *Otros servicios disponibles:*\n"
        "1️⃣ Seguros de Auto\n"
        "2️⃣ Seguros de Vida y Salud\n"
        "3️⃣ Tarjetas Médicas VRIM\n"
        "4️⃣ Financiamiento Empresarial\n"
        "5️⃣ Préstamos Personales\n\n"
        "Escribe el número del servicio que te interese 👇"
    )
    send_message(phone, menu)

# ---------------------------------------------------------------
# BLOQUE PRINCIPAL: FLUJO PRÉSTAMO IMSS LEY 73
# ---------------------------------------------------------------
def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    # Paso 1: activación inicial
    if any(x in msg for x in ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73"]):
        send_message(phone_number,
            "👋 ¡Hola! Antes de continuar, necesito confirmar algo importante.\n\n"
            "¿Eres pensionado o jubilado del IMSS bajo la Ley 73? (Responde *sí* o *no*)"
        )
        user_state[phone_number] = "esperando_respuesta_imss"
        return True

    # Paso 2: validación de respuesta IMSS
    if user_state.get(phone_number) == "esperando_respuesta_imss":
        if "no" in msg:
            send_message(phone_number,
                "Desafortunadamente no eres prospecto para este tipo de préstamo por la naturaleza del producto. 😔\n\n"
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

    # Paso 3: monto de pensión
    if user_state.get(phone_number) == "esperando_monto_pension":
        pension_monto = extract_number(msg)
        if pension_monto:
            user_data[phone_number] = {"pension_mensual": pension_monto}
            send_message(phone_number,
                "Perfecto 💰\n\n¿Qué monto deseas solicitar? (El mínimo es de $40 000 MXN)"
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
                    "Por el momento el monto mínimo para aplicar al préstamo es de $40 000 MXN. 💵\n\n"
                    "Si deseas solicitar una cantidad mayor, puedo continuar con tu registro ✅\n"
                    "O si prefieres, puedo mostrarte otras opciones que podrían interesarte 👇"
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
                    "👉 No necesitas cancelar tu cuenta actual y puedes regresar después de tres meses si no estás conforme."
                )
                user_state[phone_number] = "esperando_respuesta_nomina"
        else:
            send_message(phone_number, "Por favor indica el monto deseado, ejemplo: 65000")
        return True

    # Paso 5: validación nómina y beneficios
    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        if "sí" in msg or "si" in msg or "dispuesto" in msg:
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
                f"📢 *Nuevo prospecto IMSS Ley 73*\n\n"
                f"📞 Número: {phone_number}\n"
                f"💰 Pensión mensual: ${data.get('pension_mensual', 'N/D')}\n"
                f"💵 Monto solicitado: ${data.get('monto_solicitado', 'N/D')}\n"
                f"🏦 Acepta cambiar nómina a Inbursa ✅"
            )
            send_message(ADVISOR_NUMBER, mensaje_asesor)
            user_state.pop(phone_number, None)
        else:
            send_message(phone_number,
                "Entiendo, sin cambiar la nómina no es posible acceder al préstamo IMSS Ley 73. 😔\n\n"
                "Pero puedo mostrarte otros productos que pueden interesarte 👇"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
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

        # Iterar sobre todas las entradas y cambios
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for message in messages:
                    phone_number = message.get("from")
                    message_type = message.get("type")

                    if message_type == "text":
                        user_message = message["text"]["body"].strip()
                        logging.info(f"📱 Mensaje de {phone_number}: {user_message}")

                        # Procesar flujo IMSS
                        if handle_imss_flow(phone_number, user_message):
                            continue

                        # Si no aplica flujo IMSS, mostrar menú general
                        send_message(phone_number,
                            "👋 Hola, soy *Vicky*, asistente virtual de Inbursa.\n"
                            "Te puedo ayudar con préstamos, seguros o tarjetas médicas.\n\n"
                            "Escribe *préstamo IMSS* si eres pensionado o *menú* para ver todas las opciones."
                        )
                    else:
                        send_message(phone_number, "Por ahora solo puedo procesar mensajes de texto 📩")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# Endpoint de salud
# ---------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ---------------------------------------------------------------
# Ejecución principal
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
