from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class IMSSBot:
    def __init__(self):
        self.user_sessions = {}
        self.advisor_number = "6682478005"
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID')

    def extract_amount(self, message):
        if not message:
            return None
        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2,})?|\d+(?:\.\d{2,})?)', message.strip())
        if amount_match:
            return float(amount_match.group().replace(',', ''))
        return None

    def process_message(self, user_id, user_message):
        # Siempre crear o obtener sesión
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                'step': 'ask_pension',
                'data': {},
                'timestamp': datetime.now()
            }

        session = self.user_sessions[user_id]
        logger.info(f"Processing - User: {user_id}, Step: {session['step']}, Message: '{user_message}'")

        if session['step'] == 'ask_pension':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['pension'] = amount
                session['step'] = 'ask_loan_amount'
                return "¿Qué monto de préstamo deseas? ($40,000 - $650,000)"
            else:
                return "Préstamos a pensionados IMSS. ¿Cuál es tu pensión mensual aproximada?"

        elif session['step'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount and 40000 <= amount <= 650000:
                session['data']['loan_amount'] = amount
                session['step'] = 'ask_nomina'
                return f"✅ Para un préstamo de ${amount:,.2f}, ¿aceptas cambiar tu nómina a Inbursa? (sí/no)"
            else:
                return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"

        elif session['step'] == 'ask_nomina':
            if user_message.lower() in ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale']:
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id)
                del self.user_sessions[user_id]
                return "✅ ¡Excelente! Christian te contactará con los detalles del préstamo y beneficios de nómina Inbursa."
            elif user_message.lower() in ['no', 'nop', 'negativo']:
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id)
                del self.user_sessions[user_id]
                return "📞 Hemos registrado tu solicitud. Christian te contactará pronto."
            else:
                return "Por favor responde con 'sí' o 'no': ¿aceptas cambiar tu nómina a Inbursa?"

        return "Error. Escribe 'menu' para reiniciar."

    def notify_advisor(self, user_id):
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        message = f"🔥 NUEVO PROSPECTO IMSS\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}\n🏦 Nómina: {'SÍ' if data.get('nomina_change') else 'NO'}"
        self.send_whatsapp_message(self.advisor_number, message)

    def send_whatsapp_message(self, number, message):
        try:
            url = f"https://graph.facebook.com/v17.0/{self.whatsapp_phone_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.whatsapp_token}",
                "Content-Type": "application/json"
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": number,
                "text": {"body": message}
            }
            response = requests.post(url, json=payload, headers=headers)
            logger.info(f"WhatsApp API response: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error WhatsApp: {e}")
            return False

bot = IMSSBot()

@app.route('/')
def home():
    return "IMSS Bot Running"

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv('VERIFY_TOKEN')
    
    if mode == "subscribe" and token == verify_token:
        return challenge
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        logger.info(f"Webhook received: {data}")
        
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        phone = msg["from"]
                        text = msg.get("text", {}).get("body", "").strip()
                        
                        logger.info(f"RAW MESSAGE - From: {phone}, Text: '{text}'")
                        
                        # Procesar TODOS los mensajes con el bot IMSS
                        response = bot.process_message(phone, text)
                        
                        logger.info(f"Sending response: {response}")
                        bot.send_whatsapp_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
