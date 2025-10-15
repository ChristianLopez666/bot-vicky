import os
import json
import logging
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import openai

# ---------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

user_state = {}
user_data = {}

# ---------------------------------------------------------------
# FUNCIÓN SEND_MESSAGE MEJORADA - ÚNICA MODIFICACIÓN CRÍTICA
# ---------------------------------------------------------------
def send_message(to, text):
    """Envía mensajes de texto al usuario vía Meta Cloud API - VERSIÓN MEJORADA"""
    try:
        if not META_TOKEN:
            logging.error("❌ META_TOKEN no configurado - No se puede enviar mensaje")
            return False
        if not WABA_PHONE_ID:
            logging.error("❌ WABA_PHONE_ID no configurado - No se puede enviar mensaje")
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
        
        logging.info(f"📤 Enviando mensaje a {to}: {text[:50]}...")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code in (200, 201):
            logging.info(f"✅ Mensaje enviado CORRECTAMENTE a {to}")
            return True
        else:
            logging.error(f"❌ Error API Meta al enviar a {to}: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logging.exception(f"💥 Error en send_message para {to}: {e}")
        return False

def send_whatsapp_message(to, text):
    return send_message(to, text)

# ---------------------------------------------------------------
# FUNCIONES AUXILIARES
# ---------------------------------------------------------------
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
    positive = ['sí', 'si', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes']
    negative = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not']
    if any(k in text_lower for k in positive):
        return 'positive'
    if any(k in text_lower for k in negative):
        return 'negative'
    return 'neutral'

def is_valid_name(text):
    if not text or len(text.strip()) < 2:
        return False
    return bool(re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()))

def is_valid_phone(text):
    if not text:
        return False
    clean = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean) is not None

def send_main_menu(phone):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone, menu)

def ask_gpt(prompt, model="gpt-3.5-turbo", temperature=0.7):
    try:
        response = openai.ChatCompletion.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=400
        )
        return response.choices[0].message["content"].strip()
    except Exception as e:
        logging.exception(f"Error con OpenAI: {e}")
        return "Lo siento, ocurrió un error al consultar GPT."

def is_gpt_command(msg):
    return re.match(r'^\s*gpt\s*:', msg.lower())

# ---------------------------------------------------------------
# EMBUDO DE VENTA - CRÉDITO EMPRESARIAL
# ---------------------------------------------------------------
def funnel_credito_empresarial(user_id, user_message):
    state = user_state.get(user_id, "menu_mostrar_beneficios_empresarial")
    datos = user_data.get(user_id, {})

    # Paso 0 – Mostrar beneficios
    if state == "menu_mostrar_beneficios_empresarial":
        send_message(user_id,
            "💼 *Crédito Empresarial Inbursa*\n"
            "- Financiamiento desde $100,000 hasta $100,000,000\n"
            "- Tasas preferenciales y plazos flexibles\n"
            "- Sin aval con buen historial\n"
            "- Apoyo a PYMES, comercios y empresas consolidadas\n"
            "- Asesoría personalizada según tu giro"
        )
        send_message(user_id, "¿Eres empresario o representas una empresa?")
        user_state[user_id] = "pregunta_empresario"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 1 – Confirmar si es empresario
    if state == "pregunta_empresario":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_message(user_id, "Perfecto 😊, también tenemos otros servicios financieros que pueden interesarte:")
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        elif resp == "positive":
            send_message(user_id, "Excelente 👏 ¿A qué se dedica tu empresa?")
            user_state[user_id] = "pregunta_actividad_empresa"
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        else:
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 2 – Actividad de la empresa
    if state == "pregunta_actividad_empresa":
        user_data[user_id]["actividad_empresa"] = user_message.title()
        send_message(user_id, "¿Qué monto aproximado deseas solicitar? (mínimo $100,000)")
        user_state[user_id] = "pregunta_monto_solicitado_emp"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 3 – Monto solicitado
    if state == "pregunta_monto_solicitado_emp":
        monto = extract_number(user_message)
        if monto is None or monto < 100000:
            send_message(user_id, "Indica el monto deseado (mínimo $100,000), ejemplo: 250000")
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        user_data[user_id]["monto_solicitado"] = monto
        send_message(user_id, "¿Cuál es el nombre completo del titular o representante legal?")
        user_state[user_id] = "pregunta_nombre_emp"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 4 – Nombre
    if state == "pregunta_nombre_emp":
        if not is_valid_name(user_message):
            send_message(user_id, "Por favor escribe el nombre completo del titular o representante legal.")
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        user_data[user_id]["nombre"] = user_message.title()
        send_message(user_id, "¿Cuál es el número de contacto?")
        user_state[user_id] = "pregunta_telefono_emp"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 5 – Teléfono
    if state == "pregunta_telefono_emp":
        if not is_valid_phone(user_message):
            send_message(user_id, "Por favor escribe un número válido de 10 a 15 dígitos.")
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        user_data[user_id]["telefono_contacto"] = user_message
        send_message(user_id, "¿En qué ciudad se encuentra tu empresa?")
        user_state[user_id] = "pregunta_ciudad_emp"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 6 – Ciudad
    if state == "pregunta_ciudad_emp":
        user_data[user_id]["ciudad"] = user_message.title()
        datos = user_data.get(user_id, {})
        send_message(user_id,
            "🎯 Perfecto, hemos registrado tu solicitud.\n"
            "Un asesor financiero (Christian López) se comunicará contigo para ofrecerte la mejor propuesta.\n"
            "Gracias por confiar en Inbursa 🙌."
        )
        formatted = (
            f"🔔 NUEVO PROSPECTO – CRÉDITO EMPRESARIAL\n"
            f"Nombre: {datos.get('nombre','N/D')}\n"
            f"WhatsApp: {user_id}\n"
            f"Teléfono: {datos.get('telefono_contacto','N/D')}\n"
            f"Ciudad: {datos.get('ciudad','N/D')}\n"
            f"Giro: {datos.get('actividad_empresa','N/D')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado','N/D'):,.0f}"
        )
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
        send_message(user_id, "Además, tenemos otros servicios financieros disponibles 👇")
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    send_main_menu(user_id)
    return jsonify({"status": "ok", "funnel": "credito_empresarial"})

# ---------------------------------------------------------------
# RESTO DEL CÓDIGO ORIGINAL SIN CAMBIOS
# ---------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
