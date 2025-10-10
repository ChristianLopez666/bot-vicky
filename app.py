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

app = Flask(__name__)

user_state = {}
user_data = {}

# ----------------------
# Utilidades de mensajes
# ----------------------
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

def send_whatsapp_message(to, text):
    """Alias para enviar mensajes al asesor o prospecto."""
    send_message(to, text)

def extract_number(text):
    if not text:
        return None
    clean = text.replace(',', '').replace('$', '')
    match = re.search(r'(\d{1,9})(?:\.\d+)?\b', clean)
    if match:
        try:
            if ':' in text:
                return None
            return float(match.group(1))
        except ValueError:
            return None
    return None

def interpret_response(text):
    text_lower = (text or '').lower()
    positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes']
    negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not']
    if any(k in text_lower for k in positive_keywords):
        return 'positive'
    if any(k in text_lower for k in negative_keywords):
        return 'negative'
    return 'neutral'

def is_valid_name(text):
    if not text or len(text.strip()) < 2:
        return False
    if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()):
        return True
    return False

def is_valid_phone(text):
    if not text:
        return False
    clean_phone = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean_phone) is not None

def send_main_menu(phone):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Ley 73\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone, menu)

# ----------------------
# Detectores de flujo
# ----------------------
def detect_imss_query(text):
    keywords = ['imss', 'pensión', 'pensionado', 'jubilado', 'préstamo', 'prestamo', 'ley 73', '1']
    text = text.lower()
    return any(k in text for k in keywords)

def detect_empresarial_query(text):
    keywords = ['crédito', 'empresa', 'negocio', 'financiamiento', 'empresarial', 'pyme', '5']
    text = text.lower()
    return any(k in text for k in keywords)

# ----------------------
# EMBUDO IMSS
# ----------------------
def funnel_imss(user_id, message):
    phone_number = user_id
    msg = message.lower().strip()
    logging.info(f"[IMSS] Mensaje recibido: {msg}")
    state = user_state.get(phone_number, "inicio_imss")

    # Paso 1: Pregunta inicial
    if state == "inicio_imss":
        send_message(phone_number,
            "Hola 👋, ¿eres pensionado o jubilado del IMSS bajo la Ley 73?"
        )
        user_state[phone_number] = "imss_pregunta_pensionado"
        return jsonify({"status": "ok", "funnel": "imss"})

    # Paso 2: Respuesta pensionado
    if state == "imss_pregunta_pensionado":
        resp = interpret_response(msg)
        if resp == 'negative':
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            return jsonify({"status": "ok", "funnel": "imss"})
        elif resp == 'positive':
            send_message(phone_number,
                "¿Cuánto recibes aproximadamente al mes por concepto de pensión?"
            )
            user_state[phone_number] = "imss_pregunta_monto_pension"
            return jsonify({"status": "ok", "funnel": "imss"})
        else:
            send_message(phone_number, "Por favor responde sí o no para continuar.")
            return jsonify({"status": "ok", "funnel": "imss"})

    # Paso 3: Monto de pensión mensual
    if state == "imss_pregunta_monto_pension":
        monto = extract_number(msg)
        if monto is None:
            send_message(phone_number, "Indica el monto mensual que recibes por pensión, ejemplo: 6500")
            return jsonify({"status": "ok", "funnel": "imss"})
        if monto < 5000:
            send_message(phone_number,
                "Actualmente solo se otorgan créditos a partir de pensiones de $5,000 mensuales.\n"
                "Sin embargo, puedo notificar a un asesor para ofrecerte otras opciones financieras."
            )
            send_whatsapp_message(ADVISOR_NUMBER,
                f"IMSS prospecto pensión menor a $5,000\nWhatsApp: {phone_number}\nPensión: ${monto:,.0f}"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            return jsonify({"status": "ok", "funnel": "imss"})
        user_data[phone_number] = {"pension_mensual": monto}
        send_message(phone_number,
            "Perfecto 👍, ¿qué monto de préstamo te interesa solicitar? (mínimo $40,000)"
        )
        user_state[phone_number] = "imss_pregunta_monto_solicitado"
        return jsonify({"status": "ok", "funnel": "imss"})

    # Paso 4: Monto solicitado
    if state == "imss_pregunta_monto_solicitado":
        monto = extract_number(msg)
        if monto is None or monto < 40000:
            send_message(phone_number, "Indica el monto que deseas solicitar (mínimo $40,000), ejemplo: 65000")
            return jsonify({"status": "ok", "funnel": "imss"})
        user_data[phone_number]["monto_solicitado"] = monto
        send_message(phone_number,
            "¿Tienes depositada tu pensión en Inbursa?"
        )
        user_state[phone_number] = "imss_pregunta_nomina_inbursa"
        return jsonify({"status": "ok", "funnel": "imss"})

    # Paso 5: Pregunta nómina Inbursa
    if state == "imss_pregunta_nomina_inbursa":
        resp = interpret_response(msg)
        if resp == 'positive':
            send_message(phone_number,
                "Excelente 👏, te pondré en contacto con nuestro asesor financiero para continuar con el trámite."
            )
            # Notificación completa al asesor
            datos = user_data.get(phone_number, {})
            formatted = (
                f"IMSS Ley 73\n"
                f"WhatsApp: {phone_number}\n"
                f"Pensión mensual: ${datos.get('pension_mensual', 'N/D'):,.0f}\n"
                f"Monto solicitado: ${datos.get('monto_solicitado', 'N/D'):,.0f}\n"
                f"Nómina Inbursa: Sí"
            )
            send_whatsapp_message(ADVISOR_NUMBER, formatted)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            return jsonify({"status": "ok", "funnel": "imss"})
        elif resp == 'negative':
            send_message(phone_number,
                "Para acceder a los beneficios del crédito es necesario cambiar tu nómina a Inbursa.\n"
                "Esto te da acceso a tasas preferenciales y beneficios adicionales."
            )
            send_message(phone_number,
                "Excelente 👏, te pondré en contacto con nuestro asesor financiero para continuar con el trámite."
            )
            datos = user_data.get(phone_number, {})
            formatted = (
                f"IMSS Ley 73\n"
                f"WhatsApp: {phone_number}\n"
                f"Pensión mensual: ${datos.get('pension_mensual', 'N/D'):,.0f}\n"
                f"Monto solicitado: ${datos.get('monto_solicitado', 'N/D'):,.0f}\n"
                f"Nómina Inbursa: No, requiere cambio"
            )
            send_whatsapp_message(ADVISOR_NUMBER, formatted)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            return jsonify({"status": "ok", "funnel": "imss"})
        else:
            send_message(phone_number, "Por favor responde sí o no para continuar.")
            return jsonify({"status": "ok", "funnel": "imss"})

    send_main_menu(phone_number)
    return jsonify({"status": "ok", "funnel": "imss"})

# ----------------------
# EMBUDO EMPRESARIAL
# ----------------------
def funnel_empresarial(user_id, message):
    phone_number = user_id
    msg = message.lower().strip()
    logging.info(f"[EMPRESARIAL] Mensaje recibido: {msg}")
    state = user_state.get(phone_number, "inicio_empresarial")

    # Paso 1: Preguntar tipo de crédito
    if state == "inicio_empresarial":
        send_message(phone_number,
            "Hola 👋, ¿qué tipo de crédito necesitas?"
        )
        user_state[phone_number] = "emp_tipo_credito"
        return jsonify({"status": "ok", "funnel": "empresarial"})

    # Paso 2: Empresario o representante
    if state == "emp_tipo_credito":
        user_data[phone_number] = {"tipo_credito": message}
        send_message(phone_number,
            "¿Eres empresario o representante de una empresa?"
        )
        user_state[phone_number] = "emp_es_empresario"
        return jsonify({"status": "ok", "funnel": "empresarial"})

    # Paso 3: Giro empresa
    if state == "emp_es_empresario":
        resp = interpret_response(msg)
        if resp == 'negative':
            send_message(phone_number,
                "Por ahora solo otorgamos créditos empresariales a empresas o empresarios.\n"
                "¿Te gustaría conocer otros productos financieros?"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            return jsonify({"status": "ok", "funnel": "empresarial"})
        elif resp == 'positive':
            send_message(phone_number,
                "¿A qué se dedica tu empresa?"
            )
            user_state[phone_number] = "emp_giro_empresa"
            return jsonify({"status": "ok", "funnel": "empresarial"})
        else:
            send_message(phone_number,
                "Por favor responde sí o no para continuar."
            )
            return jsonify({"status": "ok", "funnel": "empresarial"})

    # Paso 4: Monto requerido
    if state == "emp_giro_empresa":
        user_data[phone_number]["giro_empresa"] = message
        send_message(phone_number,
            "¿Qué monto necesitas aproximadamente?"
        )
        user_state[phone_number] = "emp_monto_requerido"
        return jsonify({"status": "ok", "funnel": "empresarial"})

    # Paso 5: Datos de contacto
    if state == "emp_monto_requerido":
        monto = extract_number(msg)
        if monto is None:
            send_message(phone_number, "Indica el monto aproximado que necesitas, ejemplo: 150000")
            return jsonify({"status": "ok", "funnel": "empresarial"})
        user_data[phone_number]["monto_requerido"] = monto
        send_message(phone_number,
            "¿Cuál es tu nombre completo?"
        )
        user_state[phone_number] = "emp_nombre"
        return jsonify({"status": "ok", "funnel": "empresarial"})

    if state == "emp_nombre":
        if is_valid_name(message):
            user_data[phone_number]["nombre"] = message.title()
            send_message(phone_number,
                "¿En qué número alternativo podemos contactarte?"
            )
            user_state[phone_number] = "emp_telefono"
        else:
            send_message(phone_number,
                "Por favor ingresa un nombre válido (solo letras y espacios):\nEjemplo: Juan Pérez García"
            )
        return jsonify({"status": "ok", "funnel": "empresarial"})

    if state == "emp_telefono":
        if is_valid_phone(message):
            user_data[phone_number]["telefono"] = message
            send_message(phone_number,
                "¿En qué horario prefieres que te contacte el asesor?"
            )
            user_state[phone_number] = "emp_horario"
        else:
            send_message(phone_number,
                "Por favor ingresa un número de teléfono válido (10 dígitos mínimo)."
            )
        return jsonify({"status": "ok", "funnel": "empresarial"})

    if state == "emp_horario":
        user_data[phone_number]["horario"] = message
        datos = user_data.get(phone_number, {})
        formatted = (
            f"Campaña: Créditos Empresariales\n"
            f"WhatsApp: {phone_number}\n"
            f"Nombre: {datos.get('nombre', 'N/D')}\n"
            f"Teléfono alternativo: {datos.get('telefono', phone_number)}\n"
            f"Tipo crédito: {datos.get('tipo_credito', 'N/D')}\n"
            f"Giro empresa: {datos.get('giro_empresa', 'N/D')}\n"
            f"Monto requerido: ${datos.get('monto_requerido', 'N/D'):,.0f}\n"
            f"Horario contacto: {datos.get('horario', 'N/D')}"
        )
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
        send_message(phone_number,
            "¡Gracias! Un asesor se pondrá en contacto contigo en el horario indicado."
        )
        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return jsonify({"status": "ok", "funnel": "empresarial"})
    
    send_main_menu(phone_number)
    return jsonify({"status": "ok", "funnel": "empresarial"})

# ----------------------
# ENDPOINT PRINCIPAL /webhook
# ----------------------
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

@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json()
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
            logging.info(f"📱 Mensaje de {phone_number}: '{user_message}'")

            # --- CORRECCIÓN: FLUJO MAESTRO CON ESTADO ---
            current_state = user_state.get(phone_number)
            # Si está en embudo IMSS, SIEMPRE enviar a funnel_imss
            if current_state and current_state.startswith("imss_"):
                return funnel_imss(phone_number, user_message)
            # Si está en embudo empresarial, SIEMPRE enviar a funnel_empresarial
            if current_state and current_state.startswith("emp_"):
                return funnel_empresarial(phone_number, user_message)

            # Si es mensaje inicial, detectar palabra clave y arrancar embudo
            if detect_imss_query(user_message):
                user_state[phone_number] = "inicio_imss"
                return funnel_imss(phone_number, user_message)
            if detect_empresarial_query(user_message):
                user_state[phone_number] = "inicio_empresarial"
                return funnel_empresarial(phone_number, user_message)

            # Comando de menú
            if user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
                user_state.pop(phone_number, None)
                user_data.pop(phone_number, None)
                send_main_menu(phone_number)
                return jsonify({"status": "ok", "funnel": "menu"})

            # Saludos genéricos
            if user_message.lower() in ["hola", "hi", "hello", "buenas", "buenos días", "buenas tardes"]:
                send_main_menu(phone_number)
                return jsonify({"status": "ok", "funnel": "menu"})

            # Si no entiende, muestra menú
            send_main_menu(phone_number)
            return jsonify({"status": "ok", "funnel": "menu"})
        else:
            send_message(phone_number, 
                "Por ahora solo puedo procesar mensajes de texto 📩\n\n"
                "Escribe *menú* para ver los servicios disponibles."
            )
            return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------------
# Endpoint de salud
# ----------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
