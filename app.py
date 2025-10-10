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
    match = re.search(r'(\d{1,9})(?:\.\d+)?\b', clean)
    if match:
        try:
            if ':' in text:
                return None
            return float(match.group(1))
        except ValueError:
            return None
    return None

# ---------------------------------------------------------------
# Función: interpretar respuestas sí/no
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
# Función: detectar agradecimientos
# ---------------------------------------------------------------
def is_thankyou_message(text):
    """Detecta mensajes de agradecimiento."""
    text_lower = text.lower().strip()
    thankyou_keywords = [
        'gracias', 'grac', 'gracia', 'thank', 'thanks', 'agradecido', 
        'agradecida', 'agradecimiento', 'te lo agradezco', 'mil gracias'
    ]
    return any(keyword in text_lower for keyword in thankyou_keywords)

# ---------------------------------------------------------------
# Función: validar nombre
# ---------------------------------------------------------------
def is_valid_name(text):
    """Valida que el texto sea un nombre válido."""
    if not text or len(text.strip()) < 2:
        return False
    # Verificar que contenga solo letras, espacios y algunos caracteres especiales comunes en nombres
    if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()):
        return True
    return False

# ---------------------------------------------------------------
# Función: validar teléfono
# ---------------------------------------------------------------
def is_valid_phone(text):
    """Valida que el texto sea un teléfono válido."""
    if not text:
        return False
    # Limpiar y verificar formato de teléfono
    clean_phone = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean_phone) is not None

# ---------------------------------------------------------------
# MENÚ PRINCIPAL MEJORADO
# ---------------------------------------------------------------
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

# ---------------------------------------------------------------
# Función: manejar comando menu
# ---------------------------------------------------------------
def handle_menu_command(phone_number):
    """Maneja el comando menu para reiniciar la conversación"""
    user_state.pop(phone_number, None)
    user_data.pop(phone_number, None)
    
    menu_text = (
        "🔄 *Conversación reiniciada*\n\n"
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Ley 73\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone_number, menu_text)

# ---------------------------------------------------------------
# BLOQUE PRINCIPAL: FLUJO PRÉSTAMO IMSS LEY 73 MODIFICADO
# ---------------------------------------------------------------
def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    imss_keywords = ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73", "1"]

    # Paso 1: activación inicial por palabras clave
    if any(keyword in msg for keyword in imss_keywords):
        current_state = user_state.get(phone_number)
        if current_state not in [
            "esperando_respuesta_imss",
            "esperando_monto_solicitado",
            "esperando_respuesta_nomina",
            "esperando_nombre_imss",
            "esperando_telefono_imss",
            "esperando_ciudad_imss"
        ]:
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
                "Excelente 👏\n\n¿Qué monto de préstamo deseas solicitar? (puedes indicar cualquier cantidad, ejemplo: 65000)"
            )
            user_state[phone_number] = "esperando_monto_solicitado"
        else:
            send_message(phone_number, "Por favor responde *sí* o *no* para continuar.")
        return True

    # Paso 3: monto solicitado - ELIMINAR VALIDACIONES
    if user_state.get(phone_number) == "esperando_monto_solicitado":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡Por nada! 😊\n\n"
                "Sigamos con tu solicitud...\n\n"
                "¿Qué monto deseas solicitar? (puedes indicar cualquier cantidad, ejemplo: 65000)"
            )
            return True

        monto = extract_number(msg)
        if monto is not None:
            # ✅ ACEPTAR CUALQUIER MONTO SIN VALIDACIONES
            user_data[phone_number] = {"monto_solicitado": monto}

            send_message(phone_number,
                f"🎉 *¡FELICIDADES!* Tu monto solicitado ha sido registrado: ${monto:,.0f}\n\n"
                "🌟 *BENEFICIOS DE TU PRÉSTAMO:*\n"
                "• Sin aval\n• Sin revisión en Buró\n"
                "• Descuento directo de tu pensión\n"
                "• Tasa preferencial"
            )

            send_message(phone_number,
                "💳 *PARA ACCEDER A BENEFICIOS ADICIONALES EXCLUSIVOS*:\n\n"
                "¿Tienes tu pensión depositada en Inbursa o estarías dispuesto a cambiarla?\n\n"
                "🌟 *BENEFICIOS ADICIONALES CON NÓMINA INBURSA:*\n"
                "• Rendimientos del 80% de Cetes\n"
                "• Devolución del 20% de intereses por pago puntual\n"
                "• Anticipo de nómina hasta el 50%\n"
                "• Seguro de vida y Medicall Home (telemedicina 24/7)\n"
                "• Descuentos en Sanborns y 6,000 comercios\n"
                "• Retiros sin comisión en +28,000 puntos\n\n"
                "💡 *No necesitas cancelar tu cuenta actual*\n"
                "👉 ¿Aceptas cambiar tu nómina a Inbursa? (sí/no)"
            )
            user_state[phone_number] = "esperando_respuesta_nomina"
        else:
            send_message(phone_number, "Por favor indica el monto deseado, ejemplo: 65000")
        return True

    # Paso 4: validación nómina - AGREGAR NUEVOS PASOS DESPUÉS
    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡De nada! 😊\n\n"
                "Para continuar, por favor responde *sí* o *no*:\n\n"
                "¿Aceptas cambiar tu nómina a Inbursa para acceder a beneficios adicionales?"
            )
            return True

        intent = interpret_response(msg)
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')

        # Siempre continuar al siguiente paso (nombre)
        user_data[phone_number]["nomina_inbursa"] = "ACEPTADA" if intent == "positive" else "NO POR AHORA"
        send_message(phone_number, "👤 ¿Cuál es tu nombre completo?")
        user_state[phone_number] = "esperando_nombre_imss"
        return True

    # Paso 5: Captura nombre completo
    if user_state.get(phone_number) == "esperando_nombre_imss":
        if is_valid_name(user_message):
            user_data[phone_number]["nombre_contacto"] = user_message.title()
            send_message(phone_number,
                f"✅ Nombre registrado: {user_message.title()}\n\n"
                "📞 ¿En qué número telefónico podemos contactarte?\n\n"
                "💡 Puedes proporcionar el mismo número de WhatsApp o uno diferente"
            )
            user_state[phone_number] = "esperando_telefono_imss"
        else:
            send_message(phone_number,
                "Por favor ingresa un nombre válido (solo letras y espacios):\n\n"
                "Ejemplo: Juan Pérez García"
            )
        return True

    # Paso 6: Captura teléfono de contacto
    if user_state.get(phone_number) == "esperando_telefono_imss":
        if is_valid_phone(user_message):
            user_data[phone_number]["telefono_contacto"] = user_message
            send_message(phone_number,
                f"✅ Teléfono registrado: {user_message}\n\n"
                "🏙️ ¿En qué ciudad vives?"
            )
            user_state[phone_number] = "esperando_ciudad_imss"
        else:
            send_message(phone_number,
                "Por favor ingresa un número de teléfono válido (10 dígitos mínimo):\n\n"
                "Ejemplo: 6681234567 o +526681234567"
            )
        return True

    # Paso 7: Captura ciudad
    if user_state.get(phone_number) == "esperando_ciudad_imss":
        user_data[phone_number]["ciudad"] = user_message.title()
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')
        nombre_contacto = data.get("nombre_contacto", "N/D")
        telefono_contacto = data.get("telefono_contacto", phone_number)
        ciudad = data.get("ciudad", "N/D")
        nomina_inbursa = data.get("nomina_inbursa", "N/D")

        send_message(phone_number,
            f"🎉 *¡Excelente!* Hemos registrado tu solicitud de préstamo IMSS Ley 73.\n\n"
            "📞 *Un asesor te contactará* para:\n"
            "• Confirmar los detalles de tu préstamo\n"
            "• Explicarte el proceso de desembolso\n"
            "• Orientarte sobre los beneficios\n\n"
            "¡Gracias por confiar en Inbursa! 🏦"
        )

        mensaje_asesor = (
            f"🔥 *NUEVO PROSPECTO IMSS LEY 73 - INFORMACIÓN COMPLETA*\n\n"
            f"👤 Nombre: {nombre_contacto}\n"
            f"📞 Teléfono WhatsApp: {phone_number}\n"
            f"📱 Teléfono contacto: {telefono_contacto}\n"
            f"🏙️ Ciudad: {ciudad}\n"
            f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
            f"🏦 Nómina Inbursa: {nomina_inbursa}\n\n"
            f"🎯 *Cliente potencial para préstamo IMSS Ley 73*"
        )
        send_message(ADVISOR_NUMBER, mensaje_asesor)

        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return True

    return False

# ---------------------------------------------------------------
# BLOQUE: FLUJO CRÉDITO EMPRESARIAL - MEJORADO CON DATOS DE CONTACTO
# ---------------------------------------------------------------
def handle_business_flow(phone_number, user_message):
    # ... (sin cambios en tu flujo empresarial)
    return False

# ---------------------------------------------------------------
# FLUJO PARA OPCIONES DEL MENÚ
# ---------------------------------------------------------------
def handle_menu_options(phone_number, user_message):
    """Maneja las opciones del menú principal."""
    msg = user_message.lower().strip()
    
    menu_options = {
        '1': 'imss',
        'préstamo': 'imss',
        'prestamo': 'imss',
        'imss': 'imss',
        'ley 73': 'imss',
        '2': 'seguro_auto',
        'seguro auto': 'seguro_auto',
        'seguros de auto': 'seguro_auto',
        'auto': 'seguro_auto',
        '3': 'seguro_vida',
        'seguro vida': 'seguro_vida',
        'seguros de vida': 'seguro_vida',
        'seguro salud': 'seguro_vida',
        'vida': 'seguro_vida',
        '4': 'vrim',
        'tarjetas médicas': 'vrim',
        'tarjetas medicas': 'vrim',
        'vrim': 'vrim',
        '5': 'empresarial',
        'financiamiento empresarial': 'empresarial',
        'empresa': 'empresarial',
        'negocio': 'empresarial',
        'pyme': 'empresarial',
        'crédito empresarial': 'empresarial',
        'credito empresarial': 'empresarial'
    }
    
    option = menu_options.get(msg)
    
    if option == 'imss':
        # Corrección: pasar el mensaje original del usuario, NO un string fijo
        return handle_imss_flow(phone_number, user_message)
    elif option == 'seguro_auto':
        send_message(phone_number,
            "🚗 *Seguros de Auto Inbursa*\n\n"
            "Protege tu auto con las mejores coberturas:\n\n"
            "✅ Cobertura amplia contra todo riesgo\n"
            "✅ Asistencia vial las 24 horas\n"
            "✅ Responsabilidad civil\n"
            "✅ Robo total y parcial\n\n"
            "📞 Un asesor se comunicará contigo para cotizar tu seguro."
        )
        send_message(ADVISOR_NUMBER, f"🚗 NUEVO INTERESADO EN SEGURO DE AUTO\n📞 {phone_number}")
        return True
    elif option == 'seguro_vida':
        send_message(phone_number,
            "🏥 *Seguros de Vida y Salud Inbursa*\n\n"
            "Protege a tu familia y tu salud:\n\n"
            "✅ Seguro de vida\n"
            "✅ Gastos médicos mayores\n"
            "✅ Hospitalización\n"
            "✅ Atención médica las 24 horas\n\n"
            "📞 Un asesor se comunicará contigo para explicarte las coberturas."
        )
        send_message(ADVISOR_NUMBER, f"🏥 NUEVO INTERESADO EN SEGURO VIDA/SALUD\n📞 {phone_number}")
        return True
    elif option == 'vrim':
        send_message(phone_number,
            "💳 *Tarjetas Médicas VRIM*\n\n"
            "Accede a la mejor atención médica:\n\n"
            "✅ Consultas médicas ilimitadas\n"
            "✅ Especialistas y estudios de laboratorio\n"
            "✅ Medicamentos con descuento\n"
            "✅ Atención dental y oftalmológica\n\n"
            "📞 Un asesor se comunicará contigo para explicarte los beneficios."
        )
        send_message(ADVISOR_NUMBER, f"💳 NUEVO INTERESADO EN TARJETAS VRIM\n📞 {phone_number}")
        return True
    elif option == 'empresarial':
        user_state[phone_number] = "inicio_empresarial"
        return handle_business_flow(phone_number, "inicio")
    
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
            
            logging.info(f"📱 Mensaje de {phone_number}: '{user_message}'")

            if user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
                handle_menu_command(phone_number)
                return jsonify({"status": "ok"}), 200

            if is_thankyou_message(user_message):
                send_message(phone_number,
                    "¡De nada! 😊\n\n"
                    "Quedo a tus órdenes para cualquier otra cosa.\n\n"
                    "¿Hay algo más en lo que pueda ayudarte?"
                )
                return jsonify({"status": "ok"}), 200

            if user_state.get(phone_number) in [
                "esperando_respuesta_imss", "esperando_monto_solicitado", 
                "esperando_respuesta_nomina",
                "esperando_nombre_imss",
                "esperando_telefono_imss",
                "esperando_ciudad_imss"
            ]:
                if handle_imss_flow(phone_number, user_message):
                    return jsonify({"status": "ok"}), 200

            if user_state.get(phone_number) in [
                "inicio_empresarial", "esperando_tipo_credito", 
                "esperando_giro_empresa", "esperando_monto_empresarial",
                "esperando_nombre_empresarial", "esperando_telefono_empresarial",
                "esperando_ciudad_empresarial", "esperando_contacto_empresarial"
            ]:
                if handle_business_flow(phone_number, user_message):
                    return jsonify({"status": "ok"}), 200

            if handle_menu_options(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            if user_message.lower() in ["hola", "hi", "hello", "buenas", "buenos días", "buenas tardes"]:
                send_message(phone_number,
                    "👋 ¡Hola! Soy *Vicky*, tu asistente virtual de Inbursa.\n\n"
                    "🏦 *SERVICIOS DISPONIBLES:*\n"
                    "1️⃣ Préstamos IMSS Ley 73\n"
                    "2️⃣ Seguros de Auto\n"
                    "3️⃣ Seguros de Vida y Salud\n"
                    "4️⃣ Tarjetas Médicas VRIM\n"
                    "5️⃣ Financiamiento Empresarial\n\n"
                    "Escribe el *número* o el *nombre* del servicio que te interesa.\n\n"
                    "También puedes escribir *menú* en cualquier momento."
                )
            else:
                send_message(phone_number,
                    "👋 Hola, soy *Vicky*, tu asistente de Inbursa.\n\n"
                    "No entendí tu mensaje. Te puedo ayudar con:\n\n"
                    "🏦 *SERVICIOS DISPONIBLES:*\n"
                    "• Préstamos IMSS (escribe '1' o 'préstamo')\n"  
                    "• Seguros de Auto ('2' o 'seguro auto')\n"
                    "• Seguros de Vida ('3' o 'seguro vida')\n"
                    "• Tarjetas Médicas VRIM ('4' o 'vrim')\n"
                    "• Financiamiento Empresarial ('5' o 'empresa')\n\n"
                    "Escribe *menú* para ver todas las opciones organizadas."
                )
            return jsonify({"status": "ok"}), 200

        else:
            send_message(phone_number, 
                "Por ahora solo puedo procesar mensajes de texto 📩\n\n"
                "Escribe *menú* para ver los servicios disponibles."
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
