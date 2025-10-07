import os
import logging
import re
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# Configuración logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Variables de entorno
WHATSAPP_TOKEN = os.environ.get('WHATSAPP_TOKEN')
WHATSAPP_PHONE_ID = os.environ.get('WHATSAPP_PHONE_ID')
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
ADVISOR_WHATSAPP = os.environ.get('ADVISOR_WHATSAPP')

# Estado en memoria
user_sessions = {}

# Endpoint Graph API
GRAPH_URL = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"

# Headers para requests
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

TIMEOUT = 15

def extract_amounts(text):
    """Extrae todos los números del texto, ignorando $ y comas"""
    numbers = []
    matches = re.findall(r'[\$]?[\d,]+\.?\d*', text)
    for match in matches:
        try:
            cleaned = re.sub(r'[^\d.]', '', match)
            if cleaned:
                num = float(cleaned) if '.' in cleaned else int(cleaned)
                numbers.append(num)
        except ValueError:
            continue
    return sorted(numbers)

def extract_amount(text):
    """Extrae el primer número del texto"""
    amounts = extract_amounts(text)
    return amounts[0] if amounts else None

def send_whatsapp_message(to, message):
    """Envía mensaje de texto por WhatsApp"""
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": message}
    }
    
    try:
        response = requests.post(GRAPH_URL, json=data, headers=HEADERS, timeout=TIMEOUT)
        if response.status_code != 200:
            logger.error(f"Error enviando mensaje: {response.status_code} - {response.text}")
        else:
            logger.info(f"Mensaje enviado a {to}")
    except Exception as e:
        logger.error(f"Excepción enviando mensaje: {str(e)}")

def get_user_session(user_id):
    """Obtiene o crea sesión del usuario"""
    now = datetime.now()
    
    # Limpieza de sesiones antiguas (más de 1 hora)
    expired_users = []
    for uid, session in user_sessions.items():
        if (now - session['timestamp']).total_seconds() > 3600:
            expired_users.append(uid)
    
    for uid in expired_users:
        del user_sessions[uid]
    
    # Crear nueva sesión si no existe
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            'campaign': 'imss',
            'state': 'init',
            'data': {},
            'timestamp': now
        }
        logger.info(f"Nueva sesión creada para {user_id}")
    
    user_sessions[user_id]['timestamp'] = now
    return user_sessions[user_id]

def send_initial_message(user_id):
    """Envía mensaje inicial con información del préstamo IMSS"""
    message = """👋 ¡Hola! Soy Vicky.

🏦 *Préstamos a Pensionados IMSS (Ley 73)*
• Monto desde *$40,000* hasta *$650,000*
• ✅ Sin aval
• ✅ Sin revisión en Buró
• ✅ Descuento directo de tu pensión

💚 *Beneficios adicionales por cambiar tu nómina a Inbursa*
• Rendimiento referenciado a CETES
• Seguro de vida incluido
• Servicio médico 24/7 (orientación)
• Anticipo de nómina en emergencias

ℹ️ Para activar *estos beneficios adicionales* es necesario *cambiar tu nómina a Inbursa*.

Para comenzar, dime tu *pensión mensual aproximada* (ej. 7500)."""
    
    send_whatsapp_message(user_id, message)
    
    # Actualizar estado
    session = get_user_session(user_id)
    session['state'] = 'ask_pension'
    logger.info(f"Usuario {user_id} en estado: ask_pension")

def handle_pension_response(user_id, text):
    """Procesa respuesta del usuario con su pensión"""
    session = get_user_session(user_id)
    amounts = extract_amounts(text)
    
    # Caso especial: dos números en el mismo mensaje
    if len(amounts) >= 2:
        pension = min(amounts)
        loan_amount = max(amounts)
        
        # Validar rango del préstamo
        loan_amount = max(40000, min(650000, loan_amount))
        
        session['data']['pension'] = pension
        session['data']['loan_amount'] = loan_amount
        session['state'] = 'ask_nomina'
        
        message = f"Perfecto 👍 Detecté pensión de ${pension:,.0f} y monto de préstamo de ${loan_amount:,.0f}. Para un préstamo de ${loan_amount:,.0f} es requisito cambiar tu nómina a Inbursa. ¿Aceptas cambiar tu nómina? (sí/no)"
        send_whatsapp_message(user_id, message)
        logger.info(f"Usuario {user_id} atajo - Pensión: {pension}, Préstamo: {loan_amount}")
        return
    
    # Caso normal: un solo número
    pension = extract_amount(text)
    
    if not pension or pension <= 0:
        message = "Por favor, ingresa un monto válido para tu pensión mensual (ej. 7500)."
        send_whatsapp_message(user_id, message)
        return
    
    session['data']['pension'] = pension
    session['state'] = 'ask_loan_amount'
    
    message = f"Perfecto 👍 ¿Qué monto de préstamo deseas? (entre $40,000 y $650,000)"
    send_whatsapp_message(user_id, message)
    logger.info(f"Usuario {user_id} pensión: {pension}")

def handle_loan_amount_response(user_id, text):
    """Procesa respuesta del usuario con el monto del préstamo"""
    session = get_user_session(user_id)
    loan_amount = extract_amount(text)
    
    if not loan_amount or loan_amount < 40000 or loan_amount > 650000:
        message = "Por favor, ingresa un monto válido entre $40,000 y $650,000."
        send_whatsapp_message(user_id, message)
        return
    
    session['data']['loan_amount'] = loan_amount
    session['state'] = 'ask_nomina'
    
    message = f"Excelente ✅ para un préstamo de ${loan_amount:,.0f} es requisito cambiar tu nómina a Inbursa. ¿Aceptas cambiar tu nómina? (sí/no)"
    send_whatsapp_message(user_id, message)
    logger.info(f"Usuario {user_id} préstamo: {loan_amount}")

def handle_nomina_response(user_id, text):
    """Procesa respuesta sobre cambio de nómina"""
    session = get_user_session(user_id)
    text_lower = text.lower().strip()
    
    # Detectar sí
    positive_responses = ['sí', 'si', 'sip', 'yes', 'y', 'claro', 'acepto', 'ok', 'dale', 'por supuesto']
    negative_responses = ['no', 'nop', 'nope', 'negativo', 'na', 'non']
    
    if text_lower in positive_responses:
        session['data']['nomina_change'] = True
        session['state'] = 'completed'
        
        message = "✅ ¡Listo! Christian te contactará para confirmar tu préstamo y tus beneficios de Nómina Inbursa."
        send_whatsapp_message(user_id, message)
        notify_advisor(user_id, True)
        logger.info(f"Usuario {user_id} ACEPTA nómina")
        
    elif text_lower in negative_responses:
        session['data']['nomina_change'] = False
        session['state'] = 'completed'
        
        message = "Perfecto 👍 registré tu interés. Christian te contactará con opciones."
        send_whatsapp_message(user_id, message)
        notify_advisor(user_id, False)
        logger.info(f"Usuario {user_id} RECHAZA nómina")
        
    else:
        message = "Por favor responde con sí o no. ¿Aceptas cambiar tu nómina a Inbursa?"
        send_whatsapp_message(user_id, message)

def notify_advisor(user_id, accepts_nomina):
    """Envía notificación al asesor"""
    session = get_user_session(user_id)
    pension = session['data'].get('pension', 0)
    loan_amount = session['data'].get('loan_amount', 0)
    
    if accepts_nomina:
        message = f"""🔥 NUEVO PROSPECTO IMSS
📞 {user_id}
💰 Pensión: ${pension:,.0f}
💵 Préstamo: ${loan_amount:,.0f}
🏦 Nómina: SÍ"""
    else:
        message = f"""📋 PROSPECTO IMSS BÁSICO
📞 {user_id}
💰 Pensión: ${pension:,.0f}
💵 Préstamo: ${loan_amount:,.0f}
🏦 Nómina: NO"""
    
    send_whatsapp_message(ADVISOR_WHATSAPP, message)
    logger.info(f"Notificación enviada al asesor para {user_id}")

def handle_imss_flow(user_id, message_text):
    """Maneja el flujo completo IMSS"""
    session = get_user_session(user_id)
    
    if session['state'] == 'init':
        send_initial_message(user_id)
        
    elif session['state'] == 'ask_pension':
        handle_pension_response(user_id, message_text)
        
    elif session['state'] == 'ask_loan_amount':
        handle_loan_amount_response(user_id, message_text)
        
    elif session['state'] == 'ask_nomina':
        handle_nomina_response(user_id, message_text)

@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Verificación webhook Meta"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode and token:
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            logger.info("Webhook verificado exitosamente")
            return challenge
        else:
            logger.warning("Token de verificación inválido")
            return 'Forbidden', 403
    
    return 'Bad Request', 400

@app.route('/webhook', methods=['POST'])
def webhook_events():
    """Maneja eventos entrantes de WhatsApp"""
    try:
        data = request.get_json()
        logger.info(f"Evento recibido: {data}")
        
        if not data or 'object' not in data or data['object'] != 'whatsapp_business_account':
            return 'Not Found', 404
        
        entries = data.get('entry', [])
        for entry in entries:
            changes = entry.get('changes', [])
            for change in changes:
                value = change.get('value', {})
                messages = value.get('messages', [])
                
                for message in messages:
                    if message.get('type') == 'text':
                        user_id = message['from']
                        message_text = message['text']['body']
                        
                        logger.info(f"Mensaje de {user_id}: {message_text}")
                        
                        # Siempre iniciar flujo IMSS para cualquier mensaje
                        handle_imss_flow(user_id, message_text)
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"Error procesando webhook: {str(e)}")
        return 'OK', 200  # Siempre retornar 200 a Meta

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de health check"""
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
