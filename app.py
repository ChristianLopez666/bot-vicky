from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging

app = Flask(__name__)

# Configurar logging más detallado
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class VickyBot:
    def __init__(self):
        # Sesiones simples en memoria
        self.user_sessions = {}
        self.advisor_number = "6682478005"
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID')
        
        # Log de inicialización
        logger.info("🤖 VickyBot inicializado")
        logger.info(f"📱 Phone ID: {self.whatsapp_phone_id}")
        logger.info(f"🔑 Token: {'✅' if self.whatsapp_token else '❌'}")
        logger.info(f"👤 Asesor: {self.advisor_number}")

    # =====================
    # Detección de campaña
    # =====================
    def detect_campaign(self, initial_message=None):
        if not initial_message:
            return 'general'
        message_lower = initial_message.lower()
        imss_keywords = ['imss', 'pensionado', 'jubilado', 'ley 73', 'préstamo imss', 'prestamo imss', 'pensión', 'pension']
        business_keywords = ['empresarial', 'empresa', 'crédito empresarial', 'credito empresarial', 'negocio', 'pyme']
        
        if any(k in message_lower for k in imss_keywords):
            return 'imss'
        if any(k in message_lower for k in business_keywords):
            return 'business'
        # Si el mensaje contiene un número, es más probable que sea IMSS (pensión o monto)
        if self.extract_amount(message_lower) is not None:
            return 'imss'
        return 'general'

    # =====================
    # Inicio de conversación
    # =====================
    def start_conversation(self, user_id, initial_message=None):
        logger.info(f"🚀 Iniciando conversación con {user_id}: '{initial_message}'")
        
        if user_id not in self.user_sessions:
            campaign = self.detect_campaign(initial_message)
            self.user_sessions[user_id] = {
                'campaign': campaign,
                'state': 'welcome',
                'data': {},
                'timestamp': datetime.now()
            }
            logger.info(f"🎯 Nueva sesión {user_id} campaña={campaign}")
        
        session = self.user_sessions[user_id]

        # Inicio automático del embudo correspondiente
        if session['campaign'] == 'imss':
            return self.handle_imss_flow(user_id, "start")
        elif session['campaign'] == 'business':
            return self.handle_business_flow(user_id, "start")
        else:
            session['state'] = 'menu'
            return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"

    # =====================
    # Flujo general (menú)
    # =====================
    def handle_general_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return self.start_conversation(user_id, user_message)

        text = (user_message or '').strip().lower()
        logger.info(f"📝 Flujo general {user_id}: '{text}'")
        
        if text in ['1', '5', 'imss', 'pensión', 'pension', 'prestamo', 'préstamo']:
            session['campaign'] = 'imss'
            session['state'] = 'welcome'
            return self.handle_imss_flow(user_id, "start")
        if text in ['2', 'empresarial', 'empresa', 'negocio', 'pyme']:
            session['campaign'] = 'business'
            session['state'] = 'welcome'
            return self.handle_business_flow(user_id, "start")

        # Si escribe un número aquí, lo tratamos como entrada para IMSS
        amount = self.extract_amount(text)
        if amount is not None:
            session['campaign'] = 'imss'
            # Si aún no se ha hecho bienvenida, avanzamos directo a flujo IMSS
            if session.get('state') != 'ask_pension':
                session['state'] = 'ask_pension'
                # Guardamos como pensión si parece menor a 40k (rango mensual típico)
                if amount < 40000:
                    session['data']['pension'] = amount
                    session['state'] = 'ask_loan_amount'
                    return "Perfecto 👍 ¿Qué monto deseas solicitar? (entre $40,000 y $650,000)"
            return self.handle_imss_flow(user_id, user_message)

        return "Por favor selecciona:\n1. Préstamos IMSS\n2. Créditos empresariales"

    # =====================
    # Herramientas de parsing
    # =====================
    def extract_amount(self, message):
        """Extrae el primer número del mensaje (acepta $ y comas)."""
        if not message:
            return None
        clean = message.replace(',', '').replace('$', '')
        m = re.search(r'(\d{2,7})(?:\.\d+)?', clean)
        return float(m.group(1)) if m else None

    def extract_amounts(self, message):
        """Extrae todos los números presentes en el mensaje como floats."""
        if not message:
            return []
        clean = message.replace(',', '').replace('$', '')
        return [float(x) for x in re.findall(r'(\d{2,7})(?:\.\d+)?', clean)]

    def gpt_interpret(self, message):
        message_lower = (message or '').lower()
        positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto']
        negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto']
        if any(k in message_lower for k in positive_keywords):
            return 'positive'
        if any(k in message_lower for k in negative_keywords):
            return 'negative'
        return 'neutral'

    # =====================
    # Flujo IMSS (embudo)
    # =====================
    def handle_imss_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            session = self.user_sessions[user_id] = {
                'campaign': 'imss',
                'state': 'welcome',
                'data': {},
                'timestamp': datetime.now()
            }

        logger.info(f"💰 Flujo IMSS {user_id} estado={session['state']}: '{user_message}'")

        # Normalizamos mensaje y tratamos casos con 2 números (pensión y monto en un solo texto)
        nums = self.extract_amounts(user_message or '')
        if session['state'] in ['welcome', 'ask_pension'] and len(nums) >= 2:
            # Heurística: pensión suele ser menor que el préstamo deseado
            pension, loan_amount = sorted(nums)[:2]
            session['data']['pension'] = pension
            session['data']['loan_amount'] = max(40000, min(650000, loan_amount))
            session['state'] = 'ask_nomina'
            return (
                f"Gracias ✅ registré tu pensión *${pension:,.0f}* y monto deseado *${loan_amount:,.0f}*.\n\n"
                "Para continuar, este programa requiere cambiar tu nómina a Inbursa. ¿Aceptas el cambio? (sí/no)"
            )

        # Paso 1: Bienvenida
        if session['state'] == 'welcome':
            session['state'] = 'ask_pension'
            return (
                "💰 *Préstamos a Pensionados IMSS (Ley 73)*\n\n"
                "Monto desde *$40,000 hasta $650,000.*\n"
                "✅ Sin aval\n✅ Sin revisión en Buró\n✅ Descuento directo de tu pensión\n\n"
                "Dime tu *pensión mensual aproximada*. (Ej. 7500)"
            )

        # Paso 2: Captura pensión
        if session['state'] == 'ask_pension':
            pension = self.extract_amount(user_message or '')
            if pension is not None:
                session['data']['pension'] = pension
                session['state'] = 'ask_loan_amount'
                return "Perfecto 👍 ¿Qué *monto de préstamo* deseas solicitar? (entre $40,000 y $650,000)"
            return "Por favor ingresa tu pensión mensual (solo el monto numérico, ej. 7500):"

        # Paso 3: Monto solicitado
        if session['state'] == 'ask_loan_amount':
            loan = self.extract_amount(user_message or '')
            if loan is not None:
                if 40000 <= loan <= 650000:
                    session['data']['loan_amount'] = loan
                    session['state'] = 'ask_nomina'
                    return (
                        f"Excelente ✅ para un préstamo de *${loan:,.0f}* "
                        "es requisito cambiar tu nómina a Inbursa.\n\n"
                        "¿Aceptas cambiar tu nómina a Inbursa? (sí/no)"
                    )
                else:
                    return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"
            # Si no detectamos número pero ya viene con texto, pedimos de nuevo
            return "Por favor escribe solo el monto numérico que deseas solicitar (ej. 120000):"

        # Paso 4: Cambio de nómina
        if session['state'] == 'ask_nomina':
            intent = self.gpt_interpret(user_message or '')
            if intent == 'positive':
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss')
                session['state'] = 'completed'
                return (
                    "✅ ¡Listo! Registré tu solicitud.\n"
                    "Christian te contactará en breve para confirmar tu préstamo y los *beneficios de Nómina Inbursa*."
                )
            if intent == 'negative':
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_basic')
                session['state'] = 'completed'
                return "Perfecto 👍 hemos registrado tu interés. Christian te contactará con opciones alternativas."
            return "Por favor responde *sí* o *no*."

        # Estado desconocido → reinicio controlado
        logger.warning(f"🔄 Estado desconocido para {user_id}: {session.get('state')}")
        session['state'] = 'menu'
        return "Ocurrió un detalle. Escribe *menu* para reiniciar."

    # =====================
    # Flujo Empresarial
    # =====================
    def handle_business_flow(self, user_id, user_message):
        session = self.user_sessions.get(user_id)
        if not session:
            return "Error. Escribe 'menu' para reiniciar."

        logger.info(f"🏢 Flujo Business {user_id} estado={session['state']}: '{user_message}'")

        if session['state'] == 'welcome':
            session['state'] = 'ask_credit_type'
            return "¿Qué tipo de crédito necesitas (capital de trabajo, maquinaria, etc.)?"
        if session['state'] == 'ask_credit_type':
            session['data']['credit_type'] = user_message
            session['state'] = 'ask_business_type'
            return "¿A qué se dedica tu empresa?"
        if session['state'] == 'ask_business_type':
            session['data']['business_type'] = user_message
            session['state'] = 'ask_loan_amount'
            return "¿Qué monto de crédito necesitas?"
        if session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message or '')
            if amount is not None:
                session['data']['loan_amount'] = amount
                session['state'] = 'ask_schedule'
                return "¿Qué día y hora prefieres que te contactemos?"
            return "Por favor ingresa un monto válido (solo números)."
        if session['state'] == 'ask_schedule':
            session['data']['schedule'] = user_message
            self.notify_advisor(user_id, 'business')
            session['state'] = 'completed'
            return "✅ ¡Perfecto! Christian te contactará en el horario indicado."
        return "Error en el flujo. Escribe 'menu' para reiniciar."

    # =====================
    # Notificaciones y envío
    # =====================
    def notify_advisor(self, user_id, campaign_type):
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})

        if campaign_type == 'imss':
            message = (
                f"🔥 NUEVO PROSPECTO IMSS\n📞 {user_id}\n"
                f"💰 Pensión: ${data.get('pension', 0):,.0f}\n"
                f"💵 Préstamo: ${data.get('loan_amount', 0):,.0f}\n"
                f"🏦 Nómina: SÍ"
            )
        elif campaign_type == 'imss_basic':
            message = (
                f"📋 PROSPECTO IMSS BÁSICO\n📞 {user_id}\n"
                f"💰 Pensión: ${data.get('pension', 0):,.0f}\n"
                f"💵 Préstamo: ${data.get('loan_amount', 0):,.0f}"
            )
        elif campaign_type == 'business':
            message = (
                f"🏢 NUEVO PROSPECTO EMPRESARIAL\n📞 {user_id}\n"
                f"📊 Tipo: {data.get('credit_type', '')}\n"
                f"🏭 Giro: {data.get('business_type', '')}\n"
                f"💵 Monto: ${data.get('loan_amount', 0):,.0f}\n"
                f"📅 Horario: {data.get('schedule', '')}"
            )
        else:
            message = f"NUEVO CONTACTO {campaign_type}: {user_id}"

        logger.info(f"📤 Notificando asesor: {message}")
        success = self.send_whatsapp_message(self.advisor_number, message)
        if success:
            logger.info("✅ Notificación enviada al asesor")
        else:
            logger.error("❌ Error al enviar notificación al asesor")

    def send_whatsapp_message(self, number, message):
        try:
            if not self.whatsapp_token or not self.whatsapp_phone_id:
                logger.error("❌ Faltan credenciales de WhatsApp")
                return False

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
            
            logger.info(f"📤 Enviando mensaje a {number}: {message[:50]}...")
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            ok = 200 <= response.status_code < 300
            
            if ok:
                logger.info(f"✅ Mensaje enviado correctamente a {number}")
            else:
                logger.error(f"❌ Error WhatsApp ({response.status_code}): {response.text}")
            return ok
            
        except Exception as e:
            logger.error(f"❌ Error enviando WhatsApp: {e}")
            return False

# =====================
# Flask Routes
# =====================
vicky = VickyBot()

@app.route('/')
def home():
    return "✅ Vicky Bot Running - Inbursa"

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv('VERIFY_TOKEN')

    logger.info(f"🔐 Verificando webhook: mode={mode}, token={token}")

    if mode == "subscribe" and token == verify_token:
        logger.info("✅ Webhook verificado correctamente")
        return challenge
    logger.warning("❌ Falló verificación de webhook")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json() or {}
        logger.info(f"📨 Webhook recibido: {data}")
        
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        phone = msg.get("from")
                        text = (msg.get("text", {}) or {}).get("body", "")
                        
                        logger.info(f"📱 Mensaje de {phone}: '{text}'")
                        
                        if not phone:
                            logger.warning("❌ Mensaje sin número de teléfono")
                            continue

                        # Reset por 'menu'
                        if text.strip().lower() == 'menu':
                            vicky.user_sessions[phone] = {
                                'campaign': 'general',
                                'state': 'menu',
                                'data': {},
                                'timestamp': datetime.now()
                            }
                            response = "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
                            logger.info(f"🔄 Reinicio por menu para {phone}")
                            
                        # Inicio de sesión
                        elif phone not in vicky.user_sessions:
                            logger.info(f"🎯 Nueva sesión para {phone}")
                            response = vicky.start_conversation(phone, text)
                        else:
                            session = vicky.user_sessions[phone]
                            logger.info(f"🔄 Sesión existente {phone}: campaña={session['campaign']}, estado={session['state']}")
                            
                            if session['campaign'] == 'imss':
                                response = vicky.handle_imss_flow(phone, text)
                            elif session['campaign'] == 'business':
                                response = vicky.handle_business_flow(phone, text)
                            else:
                                response = vicky.handle_general_flow(phone, text)

                        # Enviar respuesta
                        logger.info(f"📤 Respondiendo a {phone}: {response[:50]}...")
                        success = vicky.send_whatsapp_message(phone, response)
                        if not success:
                            logger.error(f"❌ Error al enviar respuesta a {phone}")

        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"💥 Error en webhook: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    logger.info("🚀 Iniciando servidor Flask...")
    app.run(host="0.0.0.0", port=5000, debug=False)
