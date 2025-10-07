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
        """Detección mejorada para anuncios de redes sociales"""
        if not initial_message:
            return 'general'
        
        message_lower = initial_message.lower().strip()
        
        # Detectar opción 5 o cualquier número (para anuncios)
        if message_lower == '5' or self.extract_amount(initial_message):
            return 'imss'
            
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
            campaign = self.detect_campaign(initial_message)
            self.user_sessions[user_id] = {
                'campaign': campaign,
                'state': 'welcome',
                'data': {},
                'timestamp': datetime.now()
            }
        
        session = self.user_sessions[user_id]
        
        if session['campaign'] == 'imss':
            return self.handle_imss_flow(user_id, initial_message)
        elif session['campaign'] == 'business':
            return self.handle_business_flow(user_id, "start")
        else:
            session['state'] = 'menu'
            return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"

    def handle_general_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return self.start_conversation(user_id, user_message)

        if user_message == '1' or user_message == '5':
            # Iniciar directamente el flujo IMSS
            session['campaign'] = 'imss'
            session['state'] = 'ask_pension'
            return "💳 Préstamos a pensionados IMSS\n\nPara poder cotizar tu préstamo, necesito algunos datos:\n\n¿Cuál es tu pensión mensual aproximada?"
        elif user_message == '2':
            session['campaign'] = 'business'
            session['state'] = 'welcome'
            return self.handle_business_flow(user_id, "start")
        elif user_message.lower() == 'menu':
            session['state'] = 'menu'
            return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
        else:
            # Si detecta un número, iniciar flujo IMSS directamente
            if self.extract_amount(user_message):
                session['campaign'] = 'imss'
                session['state'] = 'ask_pension'
                # Guardar el número como pensión si es razonable
                amount = self.extract_amount(user_message)
                if amount >= 1000:  # Si parece una pensión razonable
                    session['data']['pension'] = amount
                    session['state'] = 'ask_loan_amount'
                    return "¿Qué monto de préstamo deseas? ($40,000 - $650,000)"
                else:
                    return "💳 Préstamos a pensionados IMSS\n\nPara poder cotizar tu préstamo, necesito algunos datos:\n\n¿Cuál es tu pensión mensual aproximada?"
            return "Por favor selecciona:\n1. Préstamos IMSS\n2. Créditos empresariales"

    def handle_imss_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            session = self.user_sessions[user_id] = {
                'campaign': 'imss',
                'state': 'ask_pension',
                'data': {},
                'timestamp': datetime.now()
            }

        logger.info(f"IMSS Flow - User: {user_id}, State: {session['state']}, Message: '{user_message}'")

        # Si es el inicio del flujo con mensaje numérico, procesarlo
        if session['state'] == 'welcome' and user_message and user_message != "start":
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['pension'] = amount
                session['state'] = 'ask_loan_amount'
                return "¿Qué monto de préstamo deseas? ($40,000 - $650,000)"
            else:
                session['state'] = 'ask_pension'
                return "💳 Préstamos a pensionados IMSS\n\nPara poder cotizar tu préstamo, necesito algunos datos:\n\n¿Cuál es tu pensión mensual aproximada?"

        # Estado: Preguntar pensión mensual
        if session['state'] == 'ask_pension':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['pension'] = amount
                session['state'] = 'ask_loan_amount'
                return "¿Qué monto de préstamo deseas? ($40,000 - $650,000)"
            else:
                return "Por favor ingresa tu pensión mensual (solo el monto numérico):"

        # Estado: Preguntar monto del préstamo
        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount:
                # Validar requisitos
                pension = session['data'].get('pension', 0)
                meets_requirements = self.validate_loan_requirements(pension, amount)
                
                if meets_requirements:
                    session['data']['loan_amount'] = amount
                    session['data']['meets_requirements'] = True
                    session['state'] = 'ask_nomina_change'
                    return f"✅ ¡Excelente! Calificas para un préstamo de ${amount:,.2f}\n\n¿Aceptas cambiar tu nómina a Inbursa para obtener mejores condiciones? (sí/no)"
                else:
                    # No cumple requisitos
                    session['data']['meets_requirements'] = False
                    self.notify_advisor(user_id, 'imss_not_qualified')
                    # Limpiar sesión
                    if user_id in self.user_sessions:
                        del self.user_sessions[user_id]
                    return "❌ Desafortunadamente no cumples con los requisitos para el préstamo IMSS en este momento.\n\nPero tenemos otros servicios que te pueden interesar:\n\n🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\n\nEscribe el número de tu opción:"
            else:
                return "Por favor ingresa un monto válido para el préstamo ($40,000 - $650,000):"

        # Estado: Confirmar cambio de nómina
        elif session['state'] == 'ask_nomina_change':
            response_type = self.gpt_interpret(user_message)
            if response_type == 'positive':
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss_qualified')
                # Limpiar sesión
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
                return "✅ ¡Perfecto! Christian te contactará en los próximos minutos con los detalles de tu préstamo y los beneficios de nómina Inbursa.\n\n¡Gracias por tu interés!"
            elif response_type == 'negative':
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_qualified_no_nomina')
                # Limpiar sesión
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
                return "📞 Hemos registrado tu solicitud. Christian te contactará pronto con las opciones disponibles.\n\n¡Gracias por tu interés!"
            else:
                return "Por favor responde con 'sí' o 'no': ¿Aceptas cambiar tu nómina a Inbursa?"

        return "Error en el flujo. Escribe 'menu' para reiniciar."

    def validate_loan_requirements(self, pension, loan_amount):
        """
        Valida si el prospecto cumple con los requisitos para el préstamo IMSS
        """
        # Validar pensión mínima
        if pension < 4000:
            return False
            
        # Validar rango del préstamo
        if loan_amount < 40000 or loan_amount > 650000:
            return False
            
        # Validar relación préstamo/pensión (no más de 100 veces)
        if loan_amount > (pension * 100):
            return False
            
        return True

    def handle_business_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Escribe 'menu' para reiniciar."

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
            return "¿Qué monto de crédito necesitas?"

        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['loan_amount'] = amount
                session['state'] = 'ask_schedule'
                return "¿En qué día y hora prefieres que te contactemos?"
            return "Por favor ingresa un monto válido."

        elif session['state'] == 'ask_schedule':
            session['data']['schedule'] = user_message
            self.notify_advisor(user_id, 'business')
            return "✅ ¡Perfecto! Christian te contactará en el horario indicado."

        return "Error en el flujo. Escribe 'menu' para reiniciar."

    def gpt_interpret(self, message):
        message_lower = message.lower()
        positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto']
        negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto']
        
        for keyword in positive_keywords:
            if keyword in message_lower:
                return 'positive'
        for keyword in negative_keywords:
            if keyword in message_lower:
                return 'negative'
        return 'neutral'

    def extract_amount(self, message):
        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2,})?|\d+(?:\.\d{2,})?)', message)
        if amount_match:
            return float(amount_match.group().replace(',', ''))
        return None

    def notify_advisor(self, user_id, campaign_type):
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        
        if campaign_type == 'imss_qualified':
            message = f"🔥 NUEVO PROSPECTO IMSS CALIFICADO\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}\n🏦 Nómina: SÍ"
        elif campaign_type == 'imss_qualified_no_nomina':
            message = f"📋 PROSPECTO IMSS CALIFICADO\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}\n🏦 Nómina: NO"
        elif campaign_type == 'imss_not_qualified':
            message = f"❌ PROSPECTO IMSS NO CALIFICADO\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo solicitado: ${data.get('loan_amount', 0):,.2f}"
        elif campaign_type == 'business':
            message = f"🏢 NUEVO PROSPECTO EMPRESARIAL\n📞 {user_id}\n📊 Tipo: {data.get('credit_type', '')}\n🏭 Giro: {data.get('business_type', '')}\n💵 Monto: ${data.get('loan_amount', 0):,.2f}\n📅 Horario: {data.get('schedule', '')}"
        
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
    return "Vicky Bot Running - Flujo IMSS Directo"

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
                            vicky.user_sessions[phone] = {
                                'campaign': 'general',
                                'state': 'menu',
                                'data': {},
                                'timestamp': datetime.now()
                            }
                            response = "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
                        elif phone not in vicky.user_sessions:
                            response = vicky.start_conversation(phone, text)
                        else:
                            session = vicky.user_sessions[phone]
                            if session['campaign'] == 'imss':
                                response = vicky.handle_imss_flow(phone, text)
                            elif session['campaign'] == 'business':
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
