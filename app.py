from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class VickyBot:
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
            amount_str = amount_match.group().replace(',', '')
            try:
                return float(amount_str)
            except ValueError:
                return None
        return None

    def gpt_interpret(self, message):
        if not message:
            return 'neutral'
        message_lower = message.lower().strip()
        positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes', 'yeah']
        negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'nope']
        
        for keyword in positive_keywords:
            if keyword in message_lower:
                return 'positive'
        for keyword in negative_keywords:
            if keyword in message_lower:
                return 'negative'
        return 'neutral'

    def handle_imss_flow(self, user_id, user_message):
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                'campaign': 'imss',
                'state': 'ask_pension',
                'data': {},
                'timestamp': datetime.now()
            }

        session = self.user_sessions[user_id]
        logger.info(f"IMSS Flow - User: {user_id}, State: {session['state']}, Message: {user_message}")

        if session['state'] == 'ask_pension':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['pension'] = amount
                session['state'] = 'ask_loan_amount'
                return "¿Qué monto de préstamo deseas? ($40,000 - $650,000)"
            else:
                return "Préstamos a pensionados IMSS. Monto a partir de $40,000 y hasta $650,000. ¿Cuál es tu pensión mensual aproximada?"

        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount:
                if 40000 <= amount <= 650000:
                    session['data']['loan_amount'] = amount
                    session['state'] = 'ask_nomina_change'
                    return f"✅ Para un préstamo de ${amount:,.2f}, ¿aceptas cambiar tu nómina a Inbursa? (sí/no)"
                else:
                    return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"
            else:
                return "Por favor ingresa un monto válido para el préstamo ($40,000 - $650,000):"

        elif session['state'] == 'ask_nomina_change':
            response_type = self.gpt_interpret(user_message)
            if response_type == 'positive':
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss')
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
                return "✅ ¡Excelente! Christian te contactará con los detalles del préstamo y beneficios de nómina Inbursa."
            elif response_type == 'negative':
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_basic')
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
                return "📞 Hemos registrado tu solicitud. Christian te contactará pronto."
            else:
                return "Por favor responde con 'sí' o 'no': ¿aceptas cambiar tu nómina a Inbursa?"

        session['state'] = 'ask_pension'
        return "Préstamos a pensionados IMSS. ¿Cuál es tu pensión mensual aproximada?"

    def notify_advisor(self, user_id, campaign_type):
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        
        if campaign_type == 'imss':
            message = f"🔥 NUEVO PROSPECTO IMSS\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}\n🏦 Nómina: {'SÍ' if data.get('nomina_change') else 'NO'}"
        elif campaign_type == 'imss_basic':
            message = f"📋 PROSPECTO IMSS BÁSICO\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}"
        
        logger.info(f"Notifying advisor: {message}")
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

vicky = VickyBot()

@app.route('/')
def home():
    return "Vicky Bot Running"

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
                        
                        logger.info(f"Received message from {phone}: '{text}'")
                        
                        # Forzar el flujo IMSS para cualquier mensaje
                        response = vicky.handle_imss_flow(phone, text)
                        
                        logger.info(f"Sending response to {phone}: {response}")
                        vicky.send_whatsapp_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
