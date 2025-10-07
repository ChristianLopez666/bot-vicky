from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging
import openai

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class VickyBot:
    def __init__(self):
        self.user_sessions = {}
        self.advisor_number = "6682478005"
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID')
        openai.api_key = os.getenv('OPENAI_API_KEY')

    def gpt_short_response(self, user_message, context):
        prompt = f"Contexto: {context}. Mensaje: {user_message}. Responde máximo 2 líneas."
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100
        )
        return response.choices[0].message.content.strip()

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

    def start_conversation(self, user_id, campaign=None, initial_message=None):
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                'campaign': campaign or self.detect_campaign(initial_message),
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
            return self.show_general_menu()

    def handle_imss_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Reinicia la conversación."

        if session['state'] == 'welcome':
            session['state'] = 'confirm_pensionado'
            return "¿Eres pensionado IMSS Ley 73?"

        elif session['state'] == 'confirm_pensionado':
            if self.gpt_interpret(user_message, 'confirmacion') == 'positive':
                session['state'] = 'ask_pension'
                return "¿Cuál es tu pensión mensual?"
            else:
                return self.show_alternative_products()

        elif session['state'] == 'ask_pension':
            interpretation = self.gpt_interpret(user_message, 'amount')
            if interpretation['type'] == 'amount':
                session['data']['pension'] = interpretation['value']
                session['state'] = 'ask_loan_amount'
                return "¿Qué monto necesitas? ($40,000 - $650,000)"
            else:
                return "Ingresa un monto válido."

        elif session['state'] == 'ask_loan_amount':
            interpretation = self.gpt_interpret(user_message, 'amount')
            if interpretation['type'] == 'amount':
                loan_amount = interpretation['value']
                if 40000 <= loan_amount <= 650000:
                    session['data']['loan_amount'] = loan_amount
                    session['state'] = 'ask_nomina_change'
                    return "¿Aceptas cambiar tu nómina a Inbursa?"
                else:
                    return "Monto fuera de rango."
            else:
                return "Ingresa un monto válido."

        elif session['state'] == 'ask_nomina_change':
            if self.gpt_interpret(user_message, 'confirmacion') == 'positive':
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss')
                return "✅ Préstamo aprobado. Christian te contactará."
            else:
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_basic')
                return "📞 Hemos registrado tu solicitud."

        return "Error en el flujo."

    def handle_business_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Reinicia la conversación."

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
            interpretation = self.gpt_interpret(user_message, 'amount')
            if interpretation['type'] == 'amount':
                session['data']['loan_amount'] = interpretation['value']
                session['state'] = 'ask_schedule'
                return "¿Cuándo podemos llamarte?"
            else:
                return "Ingresa un monto válido."

        elif session['state'] == 'ask_schedule':
            session['data']['schedule'] = user_message
            session['state'] = 'complete'
            self.notify_advisor(user_id, 'business')
            return "✅ Agendado. Christian te contactará."

        return "Error en el flujo."

    def gpt_interpret(self, message, context):
        message_lower = message.lower()
        positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale']
        negative_keywords = ['no', 'nop', 'negativo', 'para nada']
        
        if context == 'confirmacion':
            for keyword in positive_keywords:
                if keyword in message_lower:
                    return 'positive'
            for keyword in negative_keywords:
                if keyword in message_lower:
                    return 'negative'
        
        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)', message)
        if amount_match:
            return {'type': 'amount', 'value': float(amount_match.group().replace(',', ''))}
        
        return {'type': 'text', 'value': message}

    def show_general_menu(self):
        return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\n3. Tarjetas\n4. Seguros"

    def show_alternative_products(self):
        return "💼 Otros productos:\n1. Créditos personales\n2. Tarjetas\n3. Seguros\n4. Inversiones"

    def notify_advisor(self, user_id, campaign_type):
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        
        if campaign_type == 'imss':
            message = f"🔥 NUEVO PROSPECTO IMSS\n📞 {user_id}\n💰 ${data.get('pension', 0)}\n💵 ${data.get('loan_amount', 0)}\n🏦 {'SÍ' if data.get('nomina_change') else 'NO'}"
        elif campaign_type == 'business':
            message = f"🏢 CRÉDITO EMPRESARIAL\n📞 {user_id}\n📊 {data.get('credit_type', '')}\n🏭 {data.get('business_type', '')}\n💵 ${data.get('loan_amount', 0)}\n📅 {data.get('schedule', '')}"
        
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
            logger.error(f"Error: {e}")
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
                            response = vicky.start_conversation(phone, initial_message=text)
                        else:
                            session = vicky.user_sessions[phone]
                            if session['campaign'] == 'imss':
                                response = vicky.handle_imss_flow(phone, text)
                            elif session['campaign'] == 'business':
                                response = vicky.handle_business_flow(phone, text)
                            else:
                                response = vicky.gpt_short_response(text, "consulta general")
                        
                        vicky.send_whatsapp_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
