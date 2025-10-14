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
# FUNCIÓN SEND_MESSAGE MEJORADA - MANTIENE TODO LO DEMÁS INTACTO
# ---------------------------------------------------------------
def send_message(to, text):
    """Envía mensajes de texto al usuario vía Meta Cloud API - VERSIÓN MEJORADA"""
    try:
        # Validación de variables críticas
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
        
        logging.info(f"📤 Intentando enviar mensaje a {to}: {text[:50]}...")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code in (200, 201):
            logging.info(f"✅ Mensaje enviado CORRECTAMENTE a {to}")
            return True
        else:
            logging.error(f"❌ Error API Meta al enviar a {to}: {response.status_code} - {response.text}")
            # Log adicional para debugging
            logging.debug(f"URL: {url}")
            logging.debug(f"Headers: {headers}")
            return False
            
    except Exception as e:
        logging.exception(f"💥 Error CRÍTICO en send_message para {to}: {e}")
        return False

# MANTENER send_whatsapp_message EXACTAMENTE IGUAL
def send_whatsapp_message(to, text):
    return send_message(to, text)

# ---------------------------------------------------------------
# ENDPOINT DE DIAGNÓSTICO TEMPORAL - SOLO PARA DEBUGGING
# ---------------------------------------------------------------
@app.route("/debug-notification", methods=["GET", "POST"])
def debug_notification():
    """Endpoint temporal para probar notificaciones al asesor"""
    if request.method == "GET":
        return jsonify({
            "service": "Debug Notificaciones Vicky",
            "advisor_number": ADVISOR_NUMBER,
            "variables_configuradas": {
                "META_TOKEN": bool(META_TOKEN),
                "WABA_PHONE_ID": bool(WABA_PHONE_ID),
                "ADVISOR_NUMBER": ADVISOR_NUMBER
            }
        }), 200
    
    # POST: Probar envío de notificación real
    try:
        test_message = (
            f"🔔 PRUEBA: Notificación de Vicky Bot\n"
            f"📞 Para: {ADVISOR_NUMBER}\n"
            f"🕐 Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"✅ Si recibes esto, las notificaciones funcionan"
        )
        
        success = send_message(ADVISOR_NUMBER, test_message)
        
        return jsonify({
            "notification_test": {
                "sent_to": ADVISOR_NUMBER,
                "success": success,
                "timestamp": datetime.now().isoformat(),
                "message_preview": test_message[:100] + "..."
            }
        }), 200
        
    except Exception as e:
        logging.error(f"❌ Error en debug-notification: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# MANTENER TODO EL RESTO DEL CÓDIGO EXACTAMENTE IGUAL
# NO SE MODIFICA NADA MÁS
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
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone, menu)

# ---------------------------------------------------------------
# GPT SOLO BAJO COMANDO (NO SUGERIR)
# ---------------------------------------------------------------
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
# [MANTENER TODAS LAS DEMÁS FUNCIONES EXACTAMENTE IGUAL]
# funnel_prestamo_imss, verify_webhook, receive_message, health, etc.
# NO SE MODIFICA NADA MÁS
# ---------------------------------------------------------------

# ... [TODO EL RESTO DEL CÓDIGO PERMANECE EXACTAMENTE IGUAL] ...

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
