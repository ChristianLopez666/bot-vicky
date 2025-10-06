# ===============================================================
# VICKY CAMPAÑAS EN REDES – APP PRINCIPAL (CON GPT)
# Integración completa con Meta Cloud API + OpenAI GPT
# Flujos: Préstamos IMSS Ley 73 + Créditos Empresariales + GPT
# Autor: Christian López | Grupo Financiero Inbursa
# ===============================================================

import os
import json
import logging
import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import re

# ---------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Configurar OpenAI
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
    GPT_ENABLED = True
    logging.info("✅ GPT integrado y configurado")
else:
    GPT_ENABLED = False
    logging.warning("⚠️ OPENAI_API_KEY no configurada - Modo sin GPT")

# ---------------------------------------------------------------
# Configuración de logging
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)
user_state = {}
user_data = {}

# ---------------------------------------------------------------
# NUEVO: FUNCIÓN GPT PARA RESPUESTAS INTELIGENTES
# ---------------------------------------------------------------
def get_gpt_response(user_message, phone_number, context=None):
    """Obtiene respuesta contextual de GPT para conversaciones naturales."""
    if not GPT_ENABLED:
        return None

    try:
        # Contexto del sistema para Vicky
        system_prompt = """
        Eres Vicky, asistente virtual del Grupo Financiero Inbursa. 
        Eres profesional, amable y especializada en productos financieros.
        
        Productos que manejas:
        - Préstamos para pensionados IMSS (Ley 73)
        - Créditos empresariales
        - Seguros de auto, vida y salud
        - Tarjetas médicas VRIM
        - Financiamiento personal y empresarial
        
        Reglas importantes:
        - Siempre identifica si el usuario es prospecto para préstamos IMSS o créditos empresariales
        - Si no entiendes algo, pide clarificación amablemente
        - Mantén las conversaciones enfocadas en productos financieros
        - Deriva a flujos estructurados cuando detectes intención clara
        - Sé concisa pero útil
        - Usa emojis apropiados para hacer la conversación amigable
        """
        
        # Historial de conversación para contexto
        conversation_history = []
        if context:
            conversation_history.append({"role": "system", "content": context})
        
        # Llamada a la API de OpenAI
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                *conversation_history,
                {"role": "user", "content": user_message}
            ],
            max_tokens=150,
            temperature=0.7
        )
        
        gpt_response = response.choices[0].message.content.strip()
        logging.info(f"🤖 GPT respuesta: {gpt_response}")
        return gpt_response
        
    except Exception as e:
        logging.error(f"❌ Error en GPT: {e}")
        return None

# ---------------------------------------------------------------
# NUEVO: MANEJADOR DE CONVERSACIÓN NATURAL CON GPT
# ---------------------------------------------------------------
def handle_natural_conversation(phone_number, user_message):
    """Maneja conversaciones naturales usando GPT cuando no hay flujo específico."""
    
    # Contexto adicional basado en el estado del usuario
    user_context = ""
    if phone_number in user_data:
        if "pension_mensual" in user_data[phone_number]:
            user_context = "El usuario ya proporcionó datos de pensión para préstamo IMSS."
        elif "giro" in user_data[phone_number]:
            user_context = "El usuario ya proporcionó datos para crédito empresarial."
    
    # Obtener respuesta de GPT
    gpt_response = get_gpt_response(user_message, phone_number, user_context)
    
    if gpt_response:
        send_message(phone_number, gpt_response)
        
        # Después de respuesta GPT, verificar si debemos iniciar flujo estructurado
        msg_lower = user_message.lower()
        
        # Detectar intención de préstamo IMSS
        if any(word in msg_lower for word in ["pensión", "jubilado", "imss", "ley 73"]) and "préstamo" in msg_lower:
            send_message(phone_number, "🔍 Veo que te interesa un préstamo para pensionados. Déjame guiarte por el proceso...")
            return handle_imss_flow(phone_number, "préstamo imss")
        
        # Detectar intención de crédito empresarial
        elif any(word in msg_lower for word in ["empresa", "negocio", "crédito", "financiamiento"]):
            send_message(phone_number, "🏢 Entiendo que necesitas financiamiento empresarial. Te ayudo con el proceso...")
            return handle_empresa_flow(phone_number, "crédito empresarial")
        
        return True
    
    return False

# ---------------------------------------------------------------
# FUNCIÓN ORIGINAL: enviar mensaje por WhatsApp
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
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code not in (200, 201):
            logging.warning(f"⚠️ Error al enviar mensaje: {response.text}")
        else:
            logging.info(f"📩 Mensaje enviado correctamente a {to}")
    except Exception as e:
        logging.exception(f"❌ Error en send_message: {e}")

# ---------------------------------------------------------------
# [MANTENER TODAS LAS FUNCIONES ORIGINALES SIN CAMBIOS]
# handle_imss_flow(), handle_empresa_flow(), extract_number(), 
# send_main_menu(), verify_webhook(), etc.
# SE MANTIENEN EXACTAMENTE IGUAL
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Endpoint principal ACTUALIZADO con GPT
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

            # 1. Procesar flujo IMSS (prioridad alta)
            if handle_imss_flow(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            # 2. Procesar flujo empresarial (prioridad alta)
            if handle_empresa_flow(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            # 3. NUEVO: Si está en un flujo activo pero el mensaje no coincide, usar GPT
            current_state = user_state.get(phone_number)
            if current_state and current_state not in ["esperando_respuesta_imss", "esperando_tipo_credito"]:
                # El usuario está en medio de un flujo pero dio una respuesta no esperada
                gpt_response = get_gpt_response(
                    f"El usuario está en el estado '{current_state}' pero respondió: {user_message}. ¿Cómo debo proceder?",
                    phone_number
                )
                if gpt_response:
                    send_message(phone_number, gpt_response)
                return jsonify({"status": "ok"}), 200

            # 4. NUEVO: Conversación natural con GPT
            if GPT_ENABLED and handle_natural_conversation(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            # 5. Mensaje por defecto (sin GPT o fallback)
            send_message(phone_number,
                "👋 Hola, soy *Vicky*, asistente virtual de Inbursa.\n"
                "Te puedo ayudar con:\n\n"
                "• 🧓 *Préstamos IMSS* (si eres pensionado)\n" 
                "• 🏢 *Créditos empresariales*\n"
                "• 🚗 Seguros de Auto\n"
                "• 🏥 Seguros de Vida y Salud\n\n"
                "Escribe *préstamo IMSS* o *crédito empresarial* según te interese 👇"
            )
            return jsonify({"status": "ok"}), 200

        else:
            send_message(phone_number, "Por ahora solo puedo procesar mensajes de texto 📩")
            return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# Endpoint de diagnóstico MEJORADO con info GPT
# ---------------------------------------------------------------
@app.route("/debug", methods=["GET"])
def debug():
    return jsonify({
        "status": "online",
        "service": "Vicky Bot con GPT",
        "timestamp": datetime.now().isoformat(),
        "gpt_enabled": GPT_ENABLED,
        "openai_configured": bool(OPENAI_API_KEY),
        "active_users": len(user_state),
        "variables": {
            "META_TOKEN": bool(META_TOKEN),
            "WABA_PHONE_ID": bool(WABA_PHONE_ID),
            "VERIFY_TOKEN": bool(VERIFY_TOKEN),
            "OPENAI_API_KEY": bool(OPENAI_API_KEY)
        }
    }), 200

# ---------------------------------------------------------------
# [MANTENER health() y __main__ SIN CAMBIOS]
# ---------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
