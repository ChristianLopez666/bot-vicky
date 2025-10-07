from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class IMSSBotTester:
    def __init__(self):
        self.user_sessions = {}
        self.advisor_number = "6682478005"
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID')
        
        # Modo prueba - desactivar notificaciones reales
        self.test_mode = True

    def extract_amount(self, message):
        """Extrae montos numéricos del mensaje"""
        if not message:
            return None
        clean_message = message.strip()
        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2,})?|\d+(?:\.\d{2,})?)', clean_message)
        if amount_match:
            amount_str = amount_match.group().replace(',', '')
            try:
                return float(amount_str)
            except ValueError:
                return None
        return None

    def process_imss_flow(self, user_id, user_message):
        """Procesa exclusivamente el flujo IMSS de manera forzada"""
        logger.info(f"🔍 PROBANDO IMSS - Usuario: {user_id}, Mensaje: '{user_message}'")
        
        # Siempre crear sesión IMSS
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                'campaign': 'imss',
                'state': 'ask_pension',
                'data': {},
                'timestamp': datetime.now()
            }
            logger.info(f"🆕 NUEVA SESION IMSS CREADA para {user_id}")

        session = self.user_sessions[user_id]
        logger.info(f"📝 Estado actual: {session['state']}")

        # Estado: Preguntar pensión mensual
        if session['state'] == 'ask_pension':
            amount = self.extract_amount(user_message)
            if amount:
                session['data']['pension'] = amount
                session['state'] = 'ask_loan_amount'
                logger.info(f"💰 Pensión registrada: ${amount}")
                return "¿Qué monto de préstamo deseas? ($40,000 - $650,000)"
            else:
                return "Préstamos a pensionados IMSS. ¿Cuál es tu pensión mensual aproximada?"

        # Estado: Preguntar monto del préstamo
        elif session['state'] == 'ask_loan_amount':
            amount = self.extract_amount(user_message)
            if amount:
                if 40000 <= amount <= 650000:
                    session['data']['loan_amount'] = amount
                    session['state'] = 'ask_nomina_change'
                    logger.info(f"💵 Préstamo registrado: ${amount}")
                    return f"✅ Para un préstamo de ${amount:,.2f}, ¿aceptas cambiar tu nómina a Inbursa? (sí/no)"
                else:
                    return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"
            else:
                return "Por favor ingresa un monto válido para el préstamo ($40,000 - $650,000):"

        # Estado: Confirmar cambio de nómina
        elif session['state'] == 'ask_nomina_change':
            user_lower = user_message.lower().strip()
            if user_lower in ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto']:
                session['data']['nomina_change'] = True
                self.notify_advisor(user_id, 'imss')
                logger.info("✅ Usuario ACEPTA cambio de nómina")
                # Limpiar sesión
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
                return "✅ ¡Excelente! Christian te contactará con los detalles del préstamo y beneficios de nómina Inbursa."
            elif user_lower in ['no', 'nop', 'negativo', 'para nada', 'no acepto']:
                session['data']['nomina_change'] = False
                self.notify_advisor(user_id, 'imss_basic')
                logger.info("❌ Usuario RECHAZA cambio de nómina")
                # Limpiar sesión
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
                return "📞 Hemos registrado tu solicitud. Christian te contactará pronto."
            else:
                return "Por favor responde con 'sí' o 'no': ¿aceptas cambiar tu nómina a Inbursa?"

        # Fallback
        session['state'] = 'ask_pension'
        return "Préstamos a pensionados IMSS. ¿Cuál es tu pensión mensual aproximada?"

    def notify_advisor(self, user_id, campaign_type):
        """Notifica al asesor (en modo prueba solo log)"""
        session = self.user_sessions.get(user_id, {})
        data = session.get('data', {})
        
        if campaign_type == 'imss':
            message = f"🔥 [PRUEBA] NUEVO PROSPECTO IMSS\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}\n🏦 Nómina: SÍ"
        elif campaign_type == 'imss_basic':
            message = f"📋 [PRUEBA] PROSPECTO IMSS BÁSICO\n📞 {user_id}\n💰 Pensión: ${data.get('pension', 0):,.2f}\n💵 Préstamo: ${data.get('loan_amount', 0):,.2f}"
        
        logger.info(f"📤 NOTIFICACIÓN DE PRUEBA: {message}")
        
        # En modo prueba, solo enviar si test_mode es False
        if not self.test_mode:
            self.send_whatsapp_message(self.advisor_number, message)

    def send_whatsapp_message(self, number, message):
        """Envía mensaje por WhatsApp (opcional en pruebas)"""
        if self.test_mode:
            logger.info(f"🚫 MODO PRUEBA - No se envió: {message}")
            return True
            
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
            logger.info(f"📱 WhatsApp API response: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"❌ Error WhatsApp: {e}")
            return False

    def reset_user_session(self, user_id):
        """Resetea la sesión de un usuario para pruebas"""
        if user_id in self.user_sessions:
            del self.user_sessions[user_id]
            logger.info(f"🔄 Sesión reseteada para {user_id}")
            return True
        return False

    def get_session_info(self, user_id):
        """Obtiene información de la sesión para debugging"""
        return self.user_sessions.get(user_id, "No hay sesión activa")

# Instancia del bot de prueba
bot_tester = IMSSBotTester()

@app.route('/')
def home():
    return "🟢 IMSS Bot Tester - Modo Pruebas"

@app.route("/test/imss/<user_id>/<message>")
def test_imss_flow(user_id, message):
    """Endpoint para probar el flujo IMSS directamente"""
    response = bot_tester.process_imss_flow(user_id, message)
    session_info = bot_tester.get_session_info(user_id)
    
    return jsonify({
        "user_id": user_id,
        "message": message,
        "response": response,
        "session": session_info,
        "timestamp": datetime.now().isoformat()
    })

@app.route("/test/reset/<user_id>")
def reset_session(user_id):
    """Endpoint para resetear sesión de prueba"""
    result = bot_tester.reset_user_session(user_id)
    return jsonify({
        "user_id": user_id,
        "reset": result,
        "message": "Sesión reseteada" if result else "No había sesión activa"
    })

@app.route("/test/session/<user_id>")
def get_session(user_id):
    """Endpoint para ver el estado de la sesión"""
    session_info = bot_tester.get_session_info(user_id)
    return jsonify({
        "user_id": user_id,
        "session": session_info
    })

@app.route("/test/flow")
def test_complete_flow():
    """Endpoint que simula el flujo completo IMSS"""
    test_user = "test_user_" + datetime.now().strftime("%H%M%S")
    
    # Simular flujo completo
    steps = [
        ("1000", "Pensión mensual"),
        ("100000", "Monto de préstamo"),
        ("sí", "Confirmación nómina")
    ]
    
    results = []
    for message, description in steps:
        response = bot_tester.process_imss_flow(test_user, message)
        session_info = bot_tester.get_session_info(test_user)
        results.append({
            "step": description,
            "message": message,
            "response": response,
            "session_state": session_info.get('state', 'N/A') if isinstance(session_info, dict) else 'N/A'
        })
    
    return jsonify({
        "test_user": test_user,
        "flow_test": results
    })

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv('VERIFY_TOKEN')
    
    if mode == "subscribe" and token == verify_token:
        return challenge
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Webhook principal - FORZANDO solo flujo IMSS"""
    try:
        data = request.get_json()
        logger.info(f"🔔 Webhook recibido: {data}")
        
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        phone = msg["from"]
                        text = msg.get("text", {}).get("body", "").strip()
                        
                        logger.info(f"📨 Mensaje de {phone}: '{text}'")
                        
                        # FORZAR flujo IMSS sin importar el mensaje
                        response = bot_tester.process_imss_flow(phone, text)
                        
                        logger.info(f"📤 Respondiendo a {phone}: {response}")
                        
                        # Enviar respuesta
                        if not bot_tester.test_mode:
                            bot_tester.send_whatsapp_message(phone, response)
                        else:
                            logger.info(f"🚫 MODO PRUEBA - No se envió respuesta real a {phone}")
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    logger.info("🚀 Iniciando IMSS Bot Tester...")
    logger.info("📝 Endpoints de prueba disponibles:")
    logger.info("   GET /test/imss/<user_id>/<message> - Probar flujo IMSS")
    logger.info("   GET /test/reset/<user_id> - Resetear sesión")
    logger.info("   GET /test/session/<user_id> - Ver sesión")
    logger.info("   GET /test/flow - Probar flujo completo")
    logger.info("🔧 Modo prueba: ACTIVADO (no se envían mensajes reales)")
    
    app.run(host="0.0.0.0", port=5000, debug=False)
