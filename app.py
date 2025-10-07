import os
import logging
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

# Endpoint Graph API
GRAPH_URL = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"

# Headers para requests
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

def send_whatsapp_message(to, message):
    """Envía mensaje de texto por WhatsApp"""
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": message}
    }
    
    try:
        response = requests.post(GRAPH_URL, json=data, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            logger.error(f"Error enviando mensaje: {response.status_code} - {response.text}")
        else:
            logger.info(f"Mensaje enviado a {to}")
    except Exception as e:
        logger.error(f"Excepción enviando mensaje: {str(e)}")

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
                        
                        # Responder siempre con "Hola"
                        send_whatsapp_message(user_id, "Hola")
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"Error procesando webhook: {str(e)}")
        return 'OK', 200

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de health check"""
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
