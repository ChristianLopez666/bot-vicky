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

    # --- Detección de campaña inicial ---
    def detect_campaign(self, initial_message=None):
        if not initial_message:
            return 'general'
        message_lower = initial_message.lower()
        imss_keywords = ['imss', 'pensionado', 'jubilado', 'ley 73', 'préstamo', 'pensión']
        business_keywords = ['empresarial', 'empresa', 'crédito', 'negocio', 'pyme']
        for kw in imss_keywords:
            if kw in message_lower:
                return 'imss'
        for kw in business_keywords:
            if kw in message_lower:
                return 'business'
        return 'general'

    # --- Inicio de conversación ---
    def start_conversation(self, user_id, initial_message=None):
        if user_id not in self.user_sessions:
            campaign = self.detect_campaign(initial_message)
            self.user_sessions[user_id] = {
                'campaign': campaign,
                'state': 'welcome',
                'data': {},
                'timestamp': datetime.now()
            }
        session = self.user_sessions[user_id]

        # Inicio automático del embudo
        if session['campaign'] == 'imss':
            return self.handle_imss_flow(user_id, "start")
        elif session['campaign'] == 'business':
            return self.handle_business_flow(user_id, "start")
        else:
            session['state'] = 'menu'
            return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"

    # --- Flujo general ---
    def handle_general_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return self.start_conversation(user_id, user_message)

        text = user_message.strip().lower()
        if text in ['1', '5', 'imss', 'pensión', 'préstamo']:
            session['campaign'] = 'imss'
            session['state'] = 'welcome'
            return self.handle_imss_flow(user_id, "start")
        elif text in ['2', 'empresarial', 'empresa', 'negocio']:
            session['campaign'] = 'business'
            session['state'] = 'welcome'
            return self.handle_business_flow(user_id, "start")
        elif text == 'menu':
            session['state'] = 'menu'
            return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
        else:
            if self.extract_amount(user_message):
                session['campaign'] = 'imss'
                session['state'] = 'welcome'
                return self.handle_imss_flow(user_id, "start")
            return "Por favor selecciona:\n1. Préstamos IMSS\n2. Créditos empresariales"

    # --- Embudo de venta IMSS ---
    def handle_imss_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            session = self.user_sessions[user_id] = {
                'campaign': 'imss',
                'state': 'welcome',
                'data': {},
                'timestamp': datetime.now()
            }

        # Paso 1: Bienvenida
        if session['state'] == 'welcome':
            session['state'] = 'ask_pension'
            return (
                "💰 *Préstamos a Pensionados IMSS (Ley 73)*\n\n"
                "Monto desde *$40,000 hasta $650,000.*\n"
                "✅ Sin aval\n✅ Sin revisión en Buró\n✅ Descuento directo de tu pensión\n\n"
                "¿Cuál es tu pensión mensual aproximada?"
            )

        # Paso 2: Captura pensión
        elif session['state'] == 'ask_pension':
            pension = self.extract_amount(user_message)
            if pension:
                session['data']['pension'] = pension
                session['state'] = 'ask_loan_amount'
                return "Perfecto 👍 ¿Qué monto deseas solicitar? (entre $40,000 y $650,000)"
            return "Por favor ingresa tu pensión mensual (solo el monto numérico):"

        # Paso 3: Monto solicitado
        elif session['state'] == 'ask_loan_amount':
            loan = self.extract_amount(user_message)
            if loan and 40000 <= loan <= 650000:
                session['data']['loan_amount'] = loan
                session['state'] = 'ask_nomina'
                return (
                    f"Excelente ✅ para un préstamo de *${loan:,.0f}* "
                    "es necesario cambiar tu nómina a Inbursa (requisito del programa).\n\n"
                    "¿Aceptas cambiar tu nómina a Inbursa? (sí/no)"
                )
            return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"

        # Paso 4: Cambio de nómina
        elif session['state'] == 'ask_nomina':
            intent = self.gpt_interpret(user_message)
            if intent == 'positive':
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss')
                return (
                    "✅ ¡Excelente! Has completado el registro.\n"
                    "Christian te contactará en breve para confirmar tu préstamo y explicarte "
                    "los *beneficios adicionales de Nómina Inbursa*."
                )
            elif intent == 'negative':
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_basic')
                return (
                    "Perfecto 👍 hemos registrado tu interés. "
                    "Christian te contactará para ofrecerte una opción alternativa."
                )
            else:
                return "Por favor responde *sí* o *no*."

        return "Error en el flujo. Escribe 'menu' para reiniciar."

    # --- Embudo Empresarial ---
    def handle_business_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Escribe 'menu' para reiniciar."

        if session['state'] == 'welcome':
            session['state'] = 'ask_credit_type'
            return "¿Qué tipo de crédito necesitas (capital de trabajo, maquinaria, etc.)?"
        elif session['state'] == 'ask_credit_type':
            session['data']['credit_type'] = user_message
            session['state'] = 'ask_business_type'
            return "¿A qué se dedica tu empresa?"
        elif session['state'] == 'ask_business_type':
            session['data']['business_type'] = user_message
            session['state'] = 'ask_loan_amount'
            return "¿Qué monto de crédito necesitas?"
        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['loan_amount'] = amount
                session['state'] = 'ask_schedule'
                return "¿Qué día y hora prefieres que te contactemos?"
            return "Por favor ingresa un monto válido."
        elif session['state'] == 'ask_schedule':
            session['data']['schedule'] = user_message
            self.notify_advisor(user_id, 'business')
            return "✅ ¡Perfecto! Christian te contactará en el horario indicado."
        return "Error en el flujo. Escribe 'menu' para reiniciar."

    # --- Utilidades ---
    def gpt_interpret(self, msg):
        msg = msg.lower()
        pos = ['sí', 'si', 'claro', 'acepto', 'por supuesto', 'ok']
        neg = ['no', 'nop', 'negativo']
        if any(k in msg for k in pos): return 'positive'
        if any(k in msg for k in neg): return 'negative'
        return 'neutral'

    def extract_amount(self, msg):
        m = re.search(r'\d{2,7}', msg.replace(',', '').replace('$', ''))
        return float(m.group()) if m else None

    def notify_advisor(self, user_id, campaign):
        data = self.user_sessions.get(user_id, {}).get('data', {})
        if campaign == 'imss':
            body = (
                f"🔥 NUEVO PROSPECTO IMSS\n📞 {user_id}\n"
                f"💰 Pensión: ${data.get('pension',0):,.0f}\n"
                f"💵 Préstamo: ${data.get('loan_amount',0):,.0f}\n"
                f"🏦 Nómina: SÍ"
            )
        elif campaign == 'imss_basic':
            body = (
                f"📋 PROSPECTO IMSS BÁSICO\n📞 {user_id}\n"
                f"💰 Pensión: ${data.get('pension',0):,.0f}\n"
                f"💵 Préstamo: ${data.get('loan_amount',0):,.0f}"
            )
        else:
            body = (
                f"🏢 NUEVO PROSPECTO EMPRESARIAL\n📞 {user_id}\n"
                f"📊 Tipo: {data.get('credit_type','')}\n"
                f"🏭 Giro: {data.get('business_type','')}\n"
                f"💵 Monto: ${data.get('loan_amount',0):,.0f}\n"
                f"📅 Horario: {data.get('schedule','')}"
            )
        self.send_whatsapp_message(self.advisor_number, body)

    def send_whatsapp_message(self, number, msg):
        try:
            url = f"https://graph.facebook.com/v17.0/{self.whatsapp_phone_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.whatsapp_token}",
                "Content-Type": "application/json"
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": number,
                "text": {"body": msg}
            }
            r = requests.post(url, json=payload, headers=headers)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Error WhatsApp: {e}")
            return False

# --- Flask Routes ---
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
                        text = msg.get("text", {}).get("body", "").strip()
                        if text.lower() == 'menu':
                            vicky.user_sessions[phone] = {'campaign':'general','state':'menu','data':{}}
                            response = "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
                        elif phone not in vicky.user_sessions:
                            response = vicky.start_conversation(phone, text)
                        else:
                            s = vicky.user_sessions[phone]
                            if s['campaign'] == 'imss':
                                response = vicky.handle_imss_flow(phone, text)
                            elif s['campaign'] == 'business':
                                response = vicky.handle_business_flow(phone, text)
                            else:
                                response = vicky.handle_general_flow(phone, text)
                        vicky.send_whatsapp_message(phone, response)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
