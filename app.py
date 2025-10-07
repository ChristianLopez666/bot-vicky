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

    def detect_campaign(self, initial_message=None):
        if not initial_message:
            return 'general'
        message_lower = initial_message.lower()
        imss_keywords = ['imss', 'pensionado', 'jubilado', 'ley 73', 'préstamo imss', 'pensión']
        business_keywords = ['empresarial', 'empresa', 'crédito empresarial', 'negocio', 'pyme']
        
        for keyword in imss_keywords:
            if keyword in message_lower:
                return 'imss'
        for keyword in business_keywords:
            if keyword in message_lower:
                return 'business'
        return 'general'

    def start_conversation(self, user_id, initial_message=None):
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                'campaign': self.detect_campaign(initial_message),
                'state': 'welcome',
                'data': {},
                'timestamp': datetime.now()
            }
        
        session = self.user_sessions[user_id]
        
        if session['campaign'] == 'imss':
            return self.handle_imss_flow(user_id, "start")
        elif session['campaign'] == 'business':
            return self.handle_business_flow(user_id, "start")
        else:
            return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales"

    def handle_imss_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Reinicia conversación."

        if session['state'] == 'welcome':
            session['state'] = 'confirm_pensionado'
            return "¿Eres pensionado IMSS Ley 73?"

        elif session['state'] == 'confirm_pensionado':
            if self.gpt_interpret(user_message) == 'positive':
                session['state'] = 'ask_pension'
                return "¿Cuál es tu pensión mensual?"
            else:
                return "Ofrecemos otros productos. ¿Te interesa?"

        elif session['state'] == 'ask_pension':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['pension'] = amount
                session['state'] = 'ask_loan_amount'
                return "¿Qué monto necesitas? ($40k-$650k)"
            return "Ingresa monto válido."

        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount and 40000 <= amount <= 650000:
                session['data']['loan_amount'] = amount
                session['state'] = 'ask_nomina_change'
                return "¿Aceptas cambiar nómina a Inbursa?"
            return "Monto entre $40k-$650k"

        elif session['state'] == 'ask_nomina_change':
            if self.gpt_interpret(user_message) == 'positive':
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss')
                return "✅ Aprobado. Christian te contactará."
            else:
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_basic')
                return "📞 Registrado. Te contactaremos."

        return "Error en el flujo."

    def handle_business_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Reinicia conversación."

        if session['state'] == 'welcome':
            session['state'] = 'ask_credit_type'
            return "¿Qué tipo de crédito necesitas?"

        elif session['state'] == 'ask_credit_type':
            session['data']['credit_type'] = user_message
            session['state'] = 'ask_business_type'
            return "¿A qué se dedica tu empresa?"

        elif session['state'] == 'ask_business_type':
            session['data']['business_type'] = user_message
            session['state'] = 'ask_loan_amount'
            return "¿Qué monto necesitas?"

        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['loan_amount'] = amount
                session['state'] = 'ask_schedule'
                return "¿Cuándo podemos llamarte?"
            return "Ingresa monto válido."

        elif session['state'] == 'ask_schedule':
            session['data']['schedule'] = user_message
            self.notify_advisor(user_id, 'business')
            return "✅ Agendado. Christian te contactará."

        return "Error en el flujo."

    def gpt_interpret(self, message):
        message_lower = message.lower()
        positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale']
        negative_keywords = ['no', 'nop', 'negativo', 'para nada']
        
        for keyword in positive_keywords:
            if keyword in message_lower:
                return 'positive'
        for keyword in negative_keywords:
            if keyword in message_lower:
                return 'negative'
        return 'neutral'

    def extract_amount(self, message):
        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)', message)
        if amount_match:
            return float(amount_match.group().replace(',', ''))
        return None

    def notify_advisor(self, user_id, campaign_type):
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        
        if campaign_type == 'imss':
            message = f"🔥 IMSS\n📞 {user_id}\n💰 ${data.get('pension', 0)}\n💵 ${data.get('loan_amount', 0)}\n🏦 {'SÍ' if data.get('nomina_change') else 'NO'}"
        elif campaign_type == 'business':
            message = f"🏢 EMPRESARIAL\n📞 {user_id}\n📊 {data.get('credit_type', '')}\n💵 ${data.get('loan_amount', 0)}\n📅 {data.get('schedule', '')}"
        
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
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        phone = msg["from"]
                        text = msg.get("text", {}).get("body", "")
                        
                        if phone not in vicky.user_sessions:
                            response = vicky.start_conversation(phone, text)
                        else:
                            session = vicky.user_sessions[phone]
                            if session['campaign'] == 'imss':
                                response = vicky.handle_imss_flow(phone, text)
                            elif session['campaign'] == 'business':
                                response = vicky.handle_business_flow(phone, text)
                            else:
                                response = "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales"
                        
                        vicky.send_whatsapp_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
