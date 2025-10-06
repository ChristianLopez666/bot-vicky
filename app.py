from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ✅ INICIALIZAR LA APLICACIÓN FLASK PRIMERO
app = Flask(__name__)

class VickyBot:
    def __init__(self):
        self.user_sessions = {}
        self.advisor_number = "6682478005"
        
        # Configuración de WhatsApp API
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN', 'tu_token_aqui')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID', 'tu_phone_id_aqui')
        
        # Beneficios de nómina Inbursa
        self.nomina_benefits = {
            'rendimiento': "• Rendimiento del 80% de CETES sin saldo mínimo requerido",
            'seguros': "• Seguro de vida con cobertura de $100,000 por muerte accidental",
            'servicio_medico': "• Servicio médico Medical Home con consultas 24/7, asesoría nutricional y emocional",
            'anticipo': "• Anticipo de nómina hasta 50% de sueldo neto mensual",
            'descuentos': "• 10% de descuento en restaurantes Sanborns y más de 6,000 establecimientos",
            'recompensas': "• Programa de recompensas con Puntos Inbursa redimibles en aerolíneas, hoteles y efectivo",
            'cajeros': "• Red de 11,000 cajeros sin comisión y retiros en tiendas Walmart"
        }

    def detect_campaign(self, initial_message=None, utm_source=None):
        """Detecta la campaña basado en UTM parameters o mensaje inicial"""
        if not initial_message:
            return 'general'
            
        campaign_keywords = {
            'imss': ['imss', 'pensionado', 'jubilado', 'ley 73', 'préstamo imss', 'pensión'],
            'business': ['empresarial', 'empresa', 'crédito empresarial', 'negocio', 'pyme']
        }
        
        message_lower = initial_message.lower()
        for keyword in campaign_keywords['imss']:
            if keyword in message_lower:
                return 'imss'
        for keyword in campaign_keywords['business']:
            if keyword in message_lower:
                return 'business'
        
        return 'general'

    def gpt_interpret(self, message, context):
        """Simula interpretación GPT para entender la intención del usuario"""
        message_lower = message.lower()
        
        # Detectar afirmaciones
        positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'correcto', 'acepto', 'aceptar']
        negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'nunca', 'no quiero', 'no acepto']
        
        if context == 'confirmacion':
            for keyword in positive_keywords:
                if keyword in message_lower:
                    return 'positive'
            for keyword in negative_keywords:
                if keyword in message_lower:
                    return 'negative'
        
        # Detectar números (montos)
        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)', message)
        if amount_match:
            return {'type': 'amount', 'value': float(amount_match.group().replace(',', ''))}
        
        # Detectar fechas/horas
        time_patterns = [
            r'(lunes|martes|miércoles|jueves|viernes|sábado|domingo)',
            r'\d{1,2}:\d{2}',
            r'\d{1,2}\s*(am|pm)',
            r'mañana|tarde|noche'
        ]
        for pattern in time_patterns:
            if re.search(pattern, message_lower):
                return {'type': 'schedule', 'value': message}
        
        return {'type': 'text', 'value': message}

    def start_conversation(self, user_id, campaign=None, initial_message=None):
        """Inicia la conversación basado en la campaña detectada"""
        logger.info(f"Iniciando conversación para {user_id}, campaña: {campaign}")
        
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
            return self.show_general_menu(user_id)

    def handle_imss_flow(self, user_id, user_message):
        """Maneja el flujo completo para préstamos IMSS"""
        session = self.user_sessions.get(user_id)
        if not session:
            return "Ocurrió un error. Por favor, inicia la conversación nuevamente."
        
        if session['state'] == 'welcome':
            session['state'] = 'confirm_pensionado'
            return "¡Hola! Bienvenido a Préstamos para Pensionados IMSS Ley 73. ¿Eres pensionado o jubilado del IMSS bajo la Ley 73?"
        
        elif session['state'] == 'confirm_pensionado':
            interpretation = self.gpt_interpret(user_message, 'confirmacion')
            if interpretation == 'positive':
                session['state'] = 'ask_pension'
                return "Excelente. Para calcular tu préstamo, necesito saber: ¿Cuál es tu monto de pensión mensual aproximado?"
            else:
                session['state'] = 'not_eligible'
                return self.show_alternative_products(user_id)
        
        elif session['state'] == 'ask_pension':
            interpretation = self.gpt_interpret(user_message, 'amount')
            if interpretation['type'] == 'amount':
                session['data']['pension'] = interpretation['value']
                session['state'] = 'ask_loan_amount'
                return "Gracias. ¿Qué monto de préstamo deseas solicitar? (Desde $40,000 hasta $650,000)"
            else:
                return "Por favor, ingresa un monto válido para tu pensión mensual."
        
        elif session['state'] == 'ask_loan_amount':
            interpretation = self.gpt_interpret(user_message, 'amount')
            if interpretation['type'] == 'amount':
                loan_amount = interpretation['value']
                if 40000 <= loan_amount <= 650000:
                    session['data']['loan_amount'] = loan_amount
                    session['state'] = 'ask_nomina_change'
                    return f"Perfecto. Para otorgarte las mejores condiciones con un monto de ${loan_amount:,.2f}, ¿estarías dispuesto a cambiar tu nómina a Inbursa?"
                else:
                    return "El monto debe estar entre $40,000 y $650,000. Por favor, ingresa un monto válido."
            else:
                return "Por favor, ingresa un monto válido para el préstamo."
        
        elif session['state'] == 'ask_nomina_change':
            interpretation = self.gpt_interpret(user_message, 'confirmacion')
            if interpretation == 'positive':
                session['data']['nomina_change'] = True
                session['state'] = 'show_benefits'
                benefits_message = self.show_nomina_benefits()
                self.notify_advisor(user_id, 'imss')
                return f"¡Excelente decisión! {benefits_message}\n\nHemos enviado tu información a nuestro asesor Christian, quien se pondrá en contacto contigo pronto."
            else:
                session['data']['nomina_change'] = False
                session['state'] = 'basic_loan'
                self.notify_advisor(user_id, 'imss_basic')
                return "Entendido. Hemos registrado tu solicitud de préstamo y nuestro asesor Christian te contactará pronto."
        
        return "Ocurrió un error en el flujo. Por favor, intenta nuevamente."

    def handle_business_flow(self, user_id, user_message):
        """Maneja el flujo completo para créditos empresariales"""
        session = self.user_sessions.get(user_id)
        if not session:
            return "Ocurrió un error. Por favor, inicia la conversación nuevamente."
        
        if session['state'] == 'welcome':
            session['state'] = 'ask_credit_type'
            return "¡Hola! Bienvenido a Créditos Empresariales Inbursa. ¿Qué tipo de crédito necesitas para tu empresa?"
        
        elif session['state'] == 'ask_credit_type':
            session['data']['credit_type'] = user_message
            session['state'] = 'ask_business_type'
            return "Entendido. ¿A qué se dedica tu empresa? (Por ejemplo: manufactura, servicios, comercio, etc.)"
        
        elif session['state'] == 'ask_business_type':
            session['data']['business_type'] = user_message
            session['state'] = 'ask_loan_amount'
            return "Gracias. ¿Qué monto de crédito necesitas para tu negocio?"
        
        elif session['state'] == 'ask_loan_amount':
            interpretation = self.gpt_interpret(user_message, 'amount')
            if interpretation['type'] == 'amount':
                session['data']['loan_amount'] = interpretation['value']
                session['state'] = 'ask_schedule'
                return "Perfecto. Para agendar una llamada personalizada, ¿en qué día y hora prefieres que te contactemos?"
            else:
                return "Por favor, ingresa un monto válido para el crédito."
        
        elif session['state'] == 'ask_schedule':
            interpretation = self.gpt_interpret(user_message, 'schedule')
            if interpretation['type'] == 'schedule':
                session['data']['schedule'] = interpretation['value']
                session['state'] = 'offer_nomina'
                nomina_offer = "¿Te interesaría conocer los beneficios de tener la nómina de tus empleados con Inbursa? Incluye rendimientos del 80% de CETES, seguros de vida y servicio médico para tu equipo."
                return f"Agendado: {interpretation['value']}. {nomina_offer}"
            else:
                return "Por favor, proporciona un día y hora específicos para contactarte."
        
        elif session['state'] == 'offer_nomina':
            interpretation = self.gpt_interpret(user_message, 'confirmacion')
            session['data']['nomina_interest'] = (interpretation == 'positive')
            self.notify_advisor(user_id, 'business')
            
            if interpretation == 'positive':
                benefits = self.show_nomina_benefits()
                return f"¡Excelente! {benefits}\n\nNuestro asesor Christian te llamará en el horario indicado para explicarte todos los beneficios."
            else:
                return "Entendido. Nuestro asesor Christian te contactará en el horario agendado para hablar sobre tu crédito empresarial."
        
        return "Ocurrió un error en el flujo. Por favor, intenta nuevamente."

    def show_nomina_benefits(self):
        """Muestra los beneficios de cambiar la nómina a Inbursa"""
        benefits_list = [
            "🌟 *BENEFICIOS EXCLUSIVOS AL CAMBIAR TU NÓMINA A INBURSA:* 🌟",
            self.nomina_benefits['rendimiento'],
            self.nomina_benefits['seguros'],
            self.nomina_benefits['servicio_medico'],
            self.nomina_benefits['anticipo'],
            self.nomina_benefits['descuentos'],
            self.nomina_benefits['recompensas'],
            self.nomina_benefits['cajeros'],
            "",
            "💡 *Todos estos beneficios son adicionales a tu préstamo/crédito*"
        ]
        return "\n".join(benefits_list)

    def show_alternative_products(self, user_id):
        """Muestra otros productos cuando el usuario no es elegible"""
        session = self.user_sessions[user_id]
        session['state'] = 'alternative_products'
        
        alternative_products = [
            "💼 *OTROS PRODUCTOS QUE PODRÍAN INTERESARTE:*",
            "1. Créditos personales",
            "2. Tarjetas de crédito", 
            "3. Seguros de vida y gastos médicos",
            "4. Inversiones a plazo fijo",
            "5. Cuentas de ahorro",
            "",
            "¿Te interesa alguno de estos productos?"
        ]
        return "\n".join(alternative_products)

    def show_general_menu(self, user_id):
        """Menú general cuando no se detecta campaña específica"""
        session = self.user_sessions[user_id]
        session['state'] = 'general_menu'
        
        menu = [
            "🏦 *BIENVENIDO A INBURSA* 🏦",
            "Selecciona una opción:",
            "1. Préstamos para pensionados IMSS",
            "2. Créditos empresariales", 
            "3. Tarjetas de crédito",
            "4. Seguros",
            "5. Inversiones",
            "6. Atención personalizada"
        ]
        return "\n".join(menu)

    def notify_advisor(self, user_id, campaign_type):
        """Envía notificación al asesor Christian"""
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        
        if campaign_type == 'imss':
            message = f"🔥 *NUEVO PROSPECTO PRÉSTAMO IMSS* 🔥\n"
            message += f"📞 Número: {user_id}\n"
            message += f"💰 Pensión: ${data.get('pension', 0):,.2f}\n"
            message += f"💵 Monto solicitado: ${data.get('loan_amount', 0):,.2f}\n"
            message += f"🏦 Acepta cambiar nómina: {'SÍ' if data.get('nomina_change') else 'NO'}\n"
            message += f"⏰ Hora: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            
        elif campaign_type == 'imss_basic':
            message = f"📋 *PROSPECTO PRÉSTAMO IMSS (SIN NÓMINA)* 📋\n"
            message += f"📞 Número: {user_id}\n"
            message += f"💰 Pensión: ${data.get('pension', 0):,.2f}\n"
            message += f"💵 Monto solicitado: ${data.get('loan_amount', 0):,.2f}\n"
            message += f"⏰ Hora: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            
        elif campaign_type == 'business':
            message = f"🏢 *NUEVO PROSPECTO CRÉDITO EMPRESARIAL* 🏢\n"
            message += f"📞 Número: {user_id}\n"
            message += f"📊 Tipo de crédito: {data.get('credit_type', 'No especificado')}\n"
            message += f"🏭 Giro empresarial: {data.get('business_type', 'No especificado')}\n"
            message += f"💵 Monto: ${data.get('loan_amount', 0):,.2f}\n"
            message += f"📅 Horario contacto: {data.get('schedule', 'No especificado')}\n"
            message += f"🏦 Interés en nómina: {'SÍ' if data.get('nomina_interest') else 'NO'}\n"
            message += f"⏰ Hora: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        # Enviar notificación real al asesor
        logger.info(f"Notificación para asesor: {message}")
        self.send_whatsapp_message(self.advisor_number, message)
        
        return True

    def send_whatsapp_message(self, number, message):
        """Envía mensaje via WhatsApp Business API - IMPLEMENTACIÓN REAL"""
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
            logger.info(f"Respuesta WhatsApp API: {response.status_code}")
            return response.status_code == 200
            
        except Exception as e:
            logger.error(f"Error enviando mensaje WhatsApp: {e}")
            return False

# ✅ INICIALIZAR EL BOT DESPUÉS DE DEFINIR LA CLASE
vicky = VickyBot()

# ✅ WEBHOOK ENDPOINTS
@app.route('/')
def home():
    return jsonify({"status": "active", "message": "Vicky Bot is running!"})

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook de WhatsApp"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    verify_token = os.getenv('VERIFY_TOKEN', 'vicky_token_2024')
    
    if mode and token:
        if mode == "subscribe" and token == verify_token:
            logger.info("Webhook verificado exitosamente")
            return challenge
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Maneja los mensajes entrantes de WhatsApp"""
    try:
        data = request.get_json()
        logger.info(f"Datos recibidos: {data}")
        
        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        for msg in value["messages"]:
                            phone = msg["from"]
                            text = msg.get("text", {}).get("body", "").strip()
                            
                            if text:
                                # Si es el primer mensaje, iniciar conversación
                                if phone not in vicky.user_sessions:
                                    campaign = vicky.detect_campaign(initial_message=text)
                                    response = vicky.start_conversation(phone, campaign, text)
                                else:
                                    # Continuar la conversación existente
                                    session = vicky.user_sessions[phone]
                                    if session['campaign'] == 'imss':
                                        response = vicky.handle_imss_flow(phone, text)
                                    elif session['campaign'] == 'business':
                                        response = vicky.handle_business_flow(phone, text)
                                    else:
                                        response = vicky.show_general_menu(phone)
                                
                                # Enviar respuesta por WhatsApp
                                vicky.send_whatsapp_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return jsonify({"status": "error"}), 500

# ✅ EJECUCIÓN SOLO EN ENTORNO LOCAL
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
