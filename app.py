from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime
import os
import logging

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WELCOME_ORIGINAL = "💵 Préstamos a pensionados IMSS. Monto a partir de $40,000 y hasta $650,000. Dime tu pensión aproximada y el monto deseado."

IMSS_BENEFITS_MSG = (
    "🏦 *Préstamos a Pensionados IMSS (Ley 73)*\n"
    "• Monto desde *$40,000* hasta *$650,000*\n"
    "• ✅ Sin aval\n"
    "• ✅ Sin revisión en Buró\n"
    "• ✅ Descuento directo de tu pensión\n\n"
    "💚 *Beneficios adicionales por cambiar tu nómina a Inbursa*\n"
    "• Rendimiento sobre tu dinero (referenciado a CETES)\n"
    "• Seguro de vida incluido\n"
    "• Servicio médico 24/7 (orientación)\n"
    "• Anticipo/adelanto de nómina en caso de emergencia\n\n"
    "ℹ️ Para activar *estos beneficios adicionales* es necesario *cambiar tu nómina a Inbursa*.\n\n"
    "Por favor dime tu *pensión mensual aproximada* para continuar (ej. 7500)."
)

class VickyBot:
    def __init__(self):
        self.user_sessions = {}
        self.advisor_number = "6682478005"
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID')

    def extract_amounts(self, message: str):
        if not message: return []
        clean = message.replace(',', '').replace('$', '')
        return [float(x) for x in re.findall(r'(\d{2,7})(?:\.\d+)?', clean)]

    def extract_amount(self, message: str):
        nums = self.extract_amounts(message)
        return nums[0] if nums else None

    def gpt_interpret(self, message: str):
        m = (message or '').lower()
        pos = ['sí','si','claro','ok','acepto','vale','afirmativo','por supuesto']
        neg = ['no','nop','negativo','para nada','no acepto']
        if any(k in m for k in pos): return 'positive'
        if any(k in m for k in neg): return 'negative'
        return 'neutral'

    def detect_campaign(self, initial_message=None):
        if not initial_message: return 'general'
        m = initial_message.lower()
        if any(k in m for k in ['imss','pensionado','jubilado','ley 73','pensión','pension','préstamo imss','prestamo imss']): return 'imss'
        if any(k in m for k in ['empresarial','empresa','crédito empresarial','credito empresarial','negocio','pyme']): return 'business'
        if self.extract_amount(initial_message) is not None: return 'imss'
        return 'general'

    def start_conversation(self, user_id, initial_message=None):
        if user_id not in self.user_sessions:
            camp = self.detect_campaign(initial_message)
            self.user_sessions[user_id] = {'campaign': camp, 'state': 'welcome', 'data': {}, 'timestamp': datetime.now()}
            logger.info(f"[start] {user_id=} {camp=}")
        s = self.user_sessions[user_id]
        if s['campaign']=='imss': return self.handle_imss_flow(user_id, "start")
        if s['campaign']=='business': return self.handle_business_flow(user_id, "start")
        s['state']='menu'
        return "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"

    def hard_override(self, user_id, text):
        s = self.user_sessions.get(user_id)
        if not s: return None
        if s['campaign']=='imss' and s['state'] in {'benefits','welcome','ask_pension','ask_loan_amount','ask_nomina'}:
            return self.handle_imss_flow(user_id, text)
        return None

    def handle_general_flow(self, user_id, text):
        s = self.user_sessions.get(user_id)
        if not s: return self.start_conversation(user_id, text)
        t = (text or '').strip().lower()
        if t in {'5'}:
            s['campaign']='imss'; s['state']='benefits'
            return IMSS_BENEFITS_MSG
        if t in {'1','imss','pensión','pension','prestamo','préstamo'}:
            s['campaign']='imss'; s['state']='welcome'
            return self.handle_imss_flow(user_id, "start")
        if t in {'2','empresarial','empresa','negocio','pyme'}:
            s['campaign']='business'; s['state']='welcome'
            return self.handle_business_flow(user_id, "start")
        a = self.extract_amount(t)
        if a is not None:
            s['campaign']='imss'
            if a < 40000:
                s['data']['pension']=a; s['state']='ask_loan_amount'
                return "Perfecto 👍 ¿Qué monto deseas solicitar? (entre $40,000 y $650,000)"
            s['state']='ask_pension'
            return self.handle_imss_flow(user_id, text)
        return "Por favor selecciona:\n1. Préstamos IMSS\n2. Créditos empresariales"

    def handle_imss_flow(self, user_id, text):
        s = self.user_sessions.get(user_id)
        if not s:
            s = self.user_sessions[user_id] = {'campaign':'imss','state':'welcome','data':{},'timestamp':datetime.now()}

        if s['state']=='benefits':
            s['state']='ask_pension'
            return IMSS_BENEFITS_MSG

        nums = self.extract_amounts(text or '')
        if s['state'] in {'welcome','ask_pension'} and len(nums) >= 2:
            pen, loan = sorted(nums)[:2]
            s['data']['pension']=pen
            s['data']['loan_amount']=max(40000, min(650000, loan))
            s['state']='ask_nomina'
            return (f"Gracias ✅ registré tu pensión *${pen:,.0f}* y tu monto *${loan:,.0f}*.\n"
                    "Para continuar, este programa requiere cambiar tu nómina a Inbursa. ¿Aceptas? (sí/no)")

        if s['state']=='welcome':
            s['state']='ask_pension'
            return WELCOME_ORIGINAL

        if s['state']=='ask_pension':
            pen = self.extract_amount(text or '')
            if pen is not None:
                s['data']['pension']=pen
                s['state']='ask_loan_amount'
                return "Perfecto 👍 ¿Qué monto deseas solicitar? (entre $40,000 y $650,000)"
            return "Por favor ingresa tu pensión mensual (solo el monto numérico, ej. 7500):"

        if s['state']=='ask_loan_amount':
            loan = self.extract_amount(text or '')
            if loan is not None:
                if 40000 <= loan <= 650000:
                    s['data']['loan_amount']=loan
                    s['state']='ask_nomina'
                    return (f"Excelente ✅ para un préstamo de *${loan:,.0f}* es requisito cambiar tu nómina a Inbursa.\n"
                            "¿Aceptas cambiar tu nómina a Inbursa? (sí/no)")
                return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"
            return "Por favor escribe solo el monto numérico que deseas solicitar (ej. 120000):"

        if s['state']=='ask_nomina':
            intent = self.gpt_interpret(text or '')
            if intent=='positive':
                s['data']['nomina_change']=True
                self.notify_advisor(user_id, 'imss')
                s['state']='completed'
                return "✅ ¡Listo! Christian te contactará para confirmar tu préstamo y beneficios de Nómina Inbursa."
            if intent=='negative':
                s['data']['nomina_change']=False
                self.notify_advisor(user_id, 'imss_basic')
                s['state']='completed'
                return "Perfecto 👍 registré tu interés. Christian te contactará con opciones."
            return "Por favor responde *sí* o *no*."

        s['state']='menu'
        return "Ocurrió un detalle. Escribe *menu* para reiniciar."

    def handle_business_flow(self, user_id, text):
        s = self.user_sessions.get(user_id)
        if not s: return "Error. Escribe 'menu' para reiniciar."
        if s['state']=='welcome':
            s['state']='ask_credit_type'; return "¿Qué tipo de crédito necesitas (capital de trabajo, maquinaria, etc.)?"
        if s['state']=='ask_credit_type':
            s['data']['credit_type']=text; s['state']='ask_business_type'; return "¿A qué se dedica tu empresa?"
        if s['state']=='ask_business_type':
            s['data']['business_type']=text; s['state']='ask_loan_amount'; return "¿Qué monto de crédito necesitas?"
        if s['state']=='ask_loan_amount':
            a = self.extract_amount(text or '')
            if a is not None:
                s['data']['loan_amount']=a; s['state']='ask_schedule'; return "¿Qué día y hora prefieres que te contactemos?"
            return "Por favor ingresa un monto válido (solo números)."
        if s['state']=='ask_schedule':
            s['data']['schedule']=text; self.notify_advisor(user_id, 'business'); s['state']='completed'
            return "✅ ¡Perfecto! Christian te contactará en el horario indicado."
        return "Error en el flujo. Escribe 'menu' para reiniciar."

    def notify_advisor(self, user_id, campaign_type):
        s = self.user_sessions.get(user_id, {})
        d = s.get('data', {})
        if campaign_type=='imss':
            body = f"🔥 NUEVO PROSPECTO IMSS\n📞 {user_id}\n💰 Pensión: ${d.get('pension',0):,.0f}\n💵 Préstamo: ${d.get('loan_amount',0):,.0f}\n🏦 Nómina: SÍ"
        elif campaign_type=='imss_basic':
            body = f"📋 PROSPECTO IMSS BÁSICO\n📞 {user_id}\n💰 Pensión: ${d.get('pension',0):,.0f}\n💵 Préstamo: ${d.get('loan_amount',0):,.0f}"
        else:
            body = f"🏢 NUEVO PROSPECTO EMPRESARIAL\n📞 {user_id}\n📊 Tipo: {d.get('credit_type','')}\n🏭 Giro: {d.get('business_type','')}\n💵 Monto: ${d.get('loan_amount',0):,.0f}\n📅 Horario: {d.get('schedule','')}"
        self.send_whatsapp_message(self.advisor_number, body)

    def send_whatsapp_message(self, number, message):
        try:
            url = f"https://graph.facebook.com/v17.0/{self.whatsapp_phone_id}/messages"
            headers = {"Authorization": f"Bearer {self.whatsapp_token}", "Content-Type": "application/json"}
            payload = {"messaging_product":"whatsapp","to":number,"text":{"body":message}}
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            ok = 200 <= resp.status_code < 300
            if not ok:
                logger.error(f"Error WhatsApp ({resp.status_code}): {resp.text}")
            return ok
        except Exception as e:
            logger.error(f"Error WhatsApp: {e}")
            return False

vicky = VickyBot()

@app.route('/')
def home():
    return "Vicky Bot Running"

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv('VERIFY_TOKEN')
    if mode == "subscribe" and token == verify_token:
        return challenge
    return "Verification failed", 403

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        data = request.get_json() or {}
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    phone = msg.get("from")
                    text = (msg.get("text", {}) or {}).get("body", "")

                    if not phone:
                        continue

                    if (text or '').strip().lower() == 'menu':
                        vicky.user_sessions[phone] = {'campaign':'general','state':'menu','data':{},'timestamp':datetime.now()}
                        response = "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
                    elif phone not in vicky.user_sessions:
                        response = vicky.start_conversation(phone, text)
                    else:
                        forced = vicky.hard_override(phone, text)
                        if forced is not None:
                            response = forced
                        else:
                            s = vicky.user_sessions[phone]
                            if s['campaign']=='imss':
                                response = vicky.handle_imss_flow(phone, text)
                            elif s['campaign']=='business':
                                response = vicky.handle_business_flow(phone, text)
                            else:
                                response = vicky.handle_general_flow(phone, text)

                    vicky.send_whatsapp_message(phone, response)

        return jsonify({"status":"ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status":"error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
