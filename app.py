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
    match = re.search(r'(\d{1,7})(?:\.\d+)?\b', clean)
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
# Función: detectar agradecimientos - NUEVA
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
# BLOQUE PRINCIPAL: FLUJO PRÉSTAMO IMSS LEY 73
# ---------------------------------------------------------------
def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    # Detección mejorada de palabras clave IMSS - INCLUYE NÚMERO 1
    imss_keywords = ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73", "1"]
    
    # Paso 1: activación inicial por palabras clave
    if any(keyword in msg for keyword in imss_keywords):
        current_state = user_state.get(phone_number)
        if current_state not in ["esperando_respuesta_imss", "esperando_monto_pension", 
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
                "Excelente 👏\n\n¿Cuánto recibes al mes por concepto de pensión? (Ejemplo: 8500)"
            )
            user_state[phone_number] = "esperando_monto_pension"
        else:
            send_message(phone_number, "Por favor responde *sí* o *no* para continuar.")
        return True

    # Paso 3: monto de pensión - VALIDACIÓN MÍNIMO $5,000
    if user_state.get(phone_number) == "esperando_monto_pension":
        # ✅ DETECTAR AGRADECIMIENTOS DURANTE EL FLUJO
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡De nada! 😊\n\n"
                "Continuemos con tu solicitud...\n\n"
                "¿Cuánto recibes al mes por concepto de pensión? (Ejemplo: 8500)"
            )
            return True
            
        pension_monto = extract_number(msg)
        if pension_monto is not None:
            if pension_monto < 5000:
                send_message(phone_number,
                    "Para acceder al préstamo IMSS Ley 73 es necesario recibir una pensión mínima de $5,000 mensuales. 💵\n\n"
                    "Si tu pensión es mayor, por favor ingresa el monto correcto. "
                    "O si prefieres, puedo mostrarte otras opciones que podrían interesarte:"
                )
                send_main_menu(phone_number)
                user_state.pop(phone_number, None)
            else:
                user_data[phone_number] = {"pension_mensual": pension_monto}
                send_message(phone_number,
                    f"Perfecto 💰 Pensión registrada: ${pension_monto:,.0f}\n\n"
                    "¿Qué monto deseas solicitar? (El mínimo es de $40,000 MXN)"
                )
                user_state[phone_number] = "esperando_monto_solicitado"
        else:
            send_message(phone_number, "Por favor ingresa una cantidad válida, ejemplo: 8500")
        return True

    # Paso 4: monto solicitado - VALIDACIÓN MÍNIMO $40,000
    if user_state.get(phone_number) == "esperando_monto_solicitado":
        # ✅ DETECTAR AGRADECIMIENTOS DURANTE EL FLUJO
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡Por nada! 😊\n\n"
                "Sigamos con tu solicitud...\n\n"
                "¿Qué monto deseas solicitar? (El mínimo es de $40,000 MXN)"
            )
            return True
            
        monto = extract_number(msg)
        if monto is not None:
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
                
                # MOSTRAR BENEFICIOS INMEDIATAMENTE Y PREGUNTAR POR NÓMINA
                send_message(phone_number,
                    "🎉 *¡FELICIDADES!* Cumples con todos los requisitos para el préstamo IMSS Ley 73\n\n"
                    f"✅ Pensionado IMSS Ley 73\n"
                    f"✅ Pensión mensual: ${user_data[phone_number]['pension_mensual']:,.0f}\n"
                    f"✅ Monto solicitado: ${monto:,.0f}\n\n"
                    "🌟 *BENEFICIOS DE TU PRÉSTAMO:*\n"
                    "• Monto desde $40,000 hasta $650,000\n"
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

    # Paso 5: validación nómina - NO DETENER PROCESO SI RESPONDE NO
    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        # ✅ DETECTAR AGRADECIMIENTOS DURANTE EL FLUJO
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡De nada! 😊\n\n"
                "Para continuar, por favor responde *sí* o *no*:\n\n"
                "¿Aceptas cambiar tu nómina a Inbursa para acceder a beneficios adicionales?"
            )
            return True
            
        intent = interpret_response(msg)
        
        # OBTENER DATOS PARA NOTIFICACIÓN
        data = user_data.get(phone_number, {})
        pension = data.get('pension_mensual', 'N/D')
        monto_solicitado = data.get('monto_solicitado', 'N/D')
        
        if intent == 'positive':
            # CLIENTE ACEPTA CAMBIAR NÓMINA
            send_message(phone_number,
                "✅ *¡Excelente decisión!* Al cambiar tu nómina a Inbursa accederás a todos los beneficios adicionales.\n\n"
                "📞 *Christian te contactará en breve* para:\n"
                "• Confirmar los detalles de tu préstamo\n"
                "• Explicarte todos los beneficios de nómina Inbursa\n"
                "• Agendar el cambio de nómina si así lo decides\n\n"
                "¡Gracias por confiar en Inbursa! 🏦"
            )

            mensaje_asesor = (
                f"🔥 *NUEVO PROSPECTO IMSS LEY 73 - NÓMINA ACEPTADA*\n\n"
                f"📞 Número: {phone_number}\n"
                f"💰 Pensión mensual: ${pension:,.0f}\n"
                f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
                f"🏦 Nómina Inbursa: ✅ *ACEPTADA*\n"
                f"🎯 *Cliente interesado en beneficios adicionales*"
            )
            send_message(ADVISOR_NUMBER, mensaje_asesor)
            
        elif intent == 'negative':
            # CLIENTE NO ACEPTA NÓMINA PERO SIGUE EL PROCESO
            send_message(phone_number,
                "✅ *¡Perfecto!* Entiendo que por el momento prefieres mantener tu nómina actual.\n\n"
                "📞 *Christian te contactará en breve* para:\n"
                "• Confirmar los detalles de tu préstamo\n"
                "• Explicarte el proceso de desembolso\n\n"
                "💡 *Recuerda que en cualquier momento puedes cambiar tu nómina a Inbursa* "
                "para acceder a los beneficios adicionales cuando lo desees.\n\n"
                "¡Gracias por confiar en Inbursa! 🏦"
            )

            mensaje_asesor = (
                f"📋 *NUEVO PROSPECTO IMSS LEY 73*\n\n"
                f"📞 Número: {phone_number}\n"
                f"💰 Pensión mensual: ${pension:,.0f}\n"
                f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
                f"🏦 Nómina Inbursa: ❌ *No por ahora*\n"
                f"💡 *Cliente cumple requisitos - Contactar para préstamo básico*"
            )
            send_message(ADVISOR_NUMBER, mensaje_asesor)
        else:
            send_message(phone_number, 
                "Por favor responde *sí* o *no*:\n\n"
                "• *SÍ* - Para acceder a todos los beneficios adicionales con nómina Inbursa\n"
                "• *NO* - Para continuar con tu préstamo manteniendo tu nómina actual"
            )
            return True

        # LIMPIAR SESIÓN DESPUÉS DE NOTIFICAR
        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return True

    return False

# ---------------------------------------------------------------
# FLUJO PARA OPCIONES DEL MENÚ
# ---------------------------------------------------------------
def handle_menu_options(phone_number, user_message):
    """Maneja las opciones del menú principal."""
    msg = user_message.lower().strip()
    
    # Mapeo de opciones del menú
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
        'pyme': 'empresarial'
    }
    
    option = menu_options.get(msg)
    
    if option == 'imss':
        return handle_imss_flow(phone_number, "préstamo")
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
        send_message(phone_number,
            "🏢 *Financiamiento Empresarial Inbursa*\n\n"
            "Impulsa tu negocio con:\n\n"
            "✅ Créditos para capital de trabajo\n"
            "✅ Financiamiento para maquinaria\n"
            "✅ Líneas de crédito\n"
            "✅ Planes de inversión\n\n"
            "📞 Un asesor se comunicará contigo para analizar tu proyecto."
        )
        send_message(ADVISOR_NUMBER, f"🏢 NUEVO INTERESADO EN FINANCIAMIENTO EMPRESARIAL\n📞 {phone_number}")
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
# Endpoint principal para recepción de mensajes - CON DETECCIÓN DE AGRADECIMIENTOS
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

            # ✅ DETECCIÓN MEJORADA DE COMANDO MENU (CON TILDES Y VARIANTES)
            if user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
                handle_menu_command(phone_number)
                return jsonify({"status": "ok"}), 200

            # ✅ DETECCIÓN DE AGRADECIMIENTOS - ANTES DE LOS FLUJOS PRINCIPALES
            if is_thankyou_message(user_message):
                send_message(phone_number,
                    "¡De nada! 😊\n\n"
                    "Quedo a tus órdenes para cualquier otra cosa.\n\n"
                    "¿Hay algo más en lo que pueda ayudarte?"
                )
                return jsonify({"status": "ok"}), 200

            # ✅ PRIMERO: Procesar flujo IMSS si está activo
            if user_state.get(phone_number) in ["esperando_respuesta_imss", "esperando_monto_pension", 
                                              "esperando_monto_solicitado", "esperando_respuesta_nomina"]:
                if handle_imss_flow(phone_number, user_message):
                    return jsonify({"status": "ok"}), 200

            # ✅ SEGUNDO: Procesar opciones del menú principal
            if handle_menu_options(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            # ✅ TERCERO: Manejar saludos y mensajes no reconocidos
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
