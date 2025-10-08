import os
import json
import logging
import requests
import re
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP", "5216682478005")

# 🧠 Controles en memoria
PROCESSED_MESSAGE_IDS = {}
USER_FLOWS = {}

MSG_TTL = 600

# Funciones WhatsApp
def vx_wa_send_text(to, body):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=9)
        logging.info(f"vx_wa_send_text {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_text error: {e}")
        return False

def vx_wa_send_interactive(to, body, buttons):
    """Envía mensaje con botones interactivos"""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    button_items = []
    for i, button in enumerate(buttons):
        button_items.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{i+1}",
                "title": button
            }
        })
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": button_items
            }
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        logging.info(f"vx_wa_send_interactive {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_interactive error: {e}")
        return False

# Helpers
def vx_last10(phone: str) -> str:
    if not phone:
        return ""
    p = re.sub(r"[^\d]", "", str(phone))
    p = re.sub(r"^(52|521)", "", p)
    return p[-10:] if len(p) >= 10 else p

def notify_advisor(prospect_data, flow_type):
    """Notifica al asesor sobre nuevo prospecto calificado"""
    if not ADVISOR_WHATSAPP:
        logging.warning("No hay número de asesor configurado")
        return False
    
    if flow_type == "imss":
        message = f"🎯 *NUEVO PROSPECTO - CRÉDITO IMSS*\n\n"
        message += f"• 📞 Teléfono: {prospect_data.get('phone')}\n"
        message += f"• 👤 Nombre: {prospect_data.get('nombre', 'Por confirmar')}\n"
        message += f"• 🎂 Edad: {prospect_data.get('edad')} años\n"
        message += f"• 📊 Tipo: {prospect_data.get('tipo_cliente')}\n"
        message += f"• 💰 Capacidad pago: ${prospect_data.get('capacidad_pago', 'Por confirmar')}\n"
        message += f"• 🏦 Nómina Inbursa: {'Sí' if prospect_data.get('nomina_inbursa') else 'No'}\n"
        message += f"• ✅ Cumple requisitos: Sí\n\n"
        message += f"*ACCION REQUERIDA:* Contactar para proceder con trámite"
    
    elif flow_type == "empresarial":
        message = f"🏢 *NUEVO PROSPECTO - CRÉDITO EMPRESARIAL*\n\n"
        message += f"• 👤 Nombre: {prospect_data.get('nombre')}\n"
        message += f"• 🏢 Empresa: {prospect_data.get('empresa')}\n"
        message += f"• 📊 Giro: {prospect_data.get('giro')}\n"
        message += f"• 💰 Monto: ${prospect_data.get('monto')}\n"
        message += f"• ⏳ Tiempo operando: {prospect_data.get('tiempo_operacion')}\n"
        message += f"• 📞 Teléfono: {prospect_data.get('phone')}\n"
        message += f"• 📅 Cita: {prospect_data.get('cita')}\n\n"
        message += f"*ACCION REQUERIDA:* Contactar para reunión empresarial"
    
    return vx_wa_send_text(ADVISOR_WHATSAPP, message)

# =============================================================================
# FLUJO CRÉDITO IMSS
# =============================================================================

def start_imss_flow(phone, campaign_source="redes_sociales"):
    USER_FLOWS[phone] = {
        "flow": "imss",
        "step": "welcome_benefits",
        "data": {
            "campaign": campaign_source,
            "timestamp": datetime.now()
        }
    }
    
    welcome_text = """🏥 *CRÉDITO IMSS - INBURSA*

¡Te damos la bienvenida! Tenemos los *mejores beneficios* para ti:

✓ *Tasa preferencial 30.9% CAT*
✓ *Seguro de vida incluido* sin costo  
✓ *Hasta $650,000* y *60 meses*
✓ *Sin comisiones* por apertura
✓ *Proceso 100% digital*

*¿Te interesa conocer tu crédito preaprobado?*"""
    
    return vx_wa_send_interactive(phone, welcome_text, 
                                ["Sí, quiero mi crédito", "Necesito más información"])

def handle_imss_response(phone, message, user_flow):
    step = user_flow["step"]
    data = user_flow["data"]
    
    if step == "welcome_benefits":
        if "sí" in message.lower() or "si" in message.lower() or "quiero" in message.lower():
            user_flow["step"] = "ask_client_type"
            type_text = """👤 *¿A cuál de estos grupos perteneces?*

1. *Pensionado Ley 73* (vejez, viudez, cesantía)
2. *Jubilado del IMSS* (ex trabajador)  
3. *Activo IMSS* (Mando, Estatuto A, Confianza A)

Responde con el número:"""
            vx_wa_send_text(phone, type_text)
        
        else:
            info_text = """📚 *INFORMACIÓN CRÉDITO IMSS*

*Montos:* Desde $5,000 hasta $650,000
*Plazos:* 6 a 60 meses
*Tasa:* 30.9% CAT (la más competitiva)

*¿Te interesa proceder con tu solicitud?*"""
            vx_wa_send_interactive(phone, info_text, 
                                 ["Sí, continuar", "No, gracias"])
    
    elif step == "ask_client_type":
        tipo_map = {"1": "Pensionado Ley 73", "2": "Jubilado IMSS", "3": "Activo IMSS"}
        if message in ["1", "2", "3"]:
            data["tipo_cliente"] = tipo_map[message]
            user_flow["step"] = "ask_age"
            vx_wa_send_text(phone, "🎂 *¿Cuál es tu edad?*")
        else:
            vx_wa_send_text(phone, "Por favor, responde con 1, 2 o 3:")
    
    elif step == "ask_age":
        try:
            edad = int(message)
            if edad < 18 or edad > 78:
                vx_wa_send_text(phone, "❌ La edad debe estar entre 18 y 78 años. Ingresa tu edad nuevamente:")
            else:
                data["edad"] = edad
                user_flow["step"] = "get_name"
                vx_wa_send_text(phone, "👤 *Ingresa tu nombre completo:*")
        except:
            vx_wa_send_text(phone, "Por favor, ingresa tu edad en números:")
    
    elif step == "get_name":
        data["nombre"] = message
        data["phone"] = phone
        data["cumple_requisitos"] = True
        
        # PROSPECTO CALIFICADO - Notificar asesor
        notify_advisor(data, "imss")
        
        success_text = f"""🎉 *¡FELICIDADES! ESTÁS PRE-APROBADO*

*Resumen:*
• 👤 Nombre: {data['nombre']}
• 📞 Teléfono: {vx_last10(phone)}
• 🎂 Edad: {data['edad']} años ✓
• 📊 Tipo: {data['tipo_cliente']} ✓

*📍 PRÓXIMOS PASOS:*
1. *Notificaré a tu asesor*
2. *Te contactará en menos de 24 horas*
3. *Reunir documentación requerida*

*Tu asesor se pondrá en contacto contigo pronto.*"""
        
        vx_wa_send_text(phone, success_text)
        USER_FLOWS.pop(phone, None)

# =============================================================================
# FLUJO CRÉDITOS EMPRESARIALES
# =============================================================================

def start_empresarial_flow(phone, campaign_source="redes_sociales"):
    USER_FLOWS[phone] = {
        "flow": "empresarial", 
        "step": "get_name",
        "data": {"campaign": campaign_source},
        "timestamp": datetime.now()
    }
    
    welcome_text = """🏢 *CRÉDITOS EMPRESARIALES*

¡Excelente! Creamos planes *a la medida* para tu negocio.

*👤 Para empezar, ingresa tu nombre completo:*"""
    
    return vx_wa_send_text(phone, welcome_text)

def handle_empresarial_response(phone, message, user_flow):
    step = user_flow["step"]
    data = user_flow["data"]
    
    if step == "get_name":
        data["nombre"] = message
        user_flow["step"] = "get_company"
        vx_wa_send_text(phone, "🏢 *¿Cuál es el nombre de tu empresa?*")
    
    elif step == "get_company":
        data["empresa"] = message
        user_flow["step"] = "get_industry" 
        vx_wa_send_text(phone, "📊 *¿A qué giro se dedica tu negocio?*")
    
    elif step == "get_industry":
        data["giro"] = message
        user_flow["step"] = "get_amount"
        vx_wa_send_text(phone, "💰 *¿Qué monto aproximado requieres?*")
    
    elif step == "get_amount":
        data["monto"] = message
        user_flow["step"] = "get_experience"
        vx_wa_send_text(phone, "⏳ *¿Cuánto tiempo tiene operando tu negocio (en años)?*")
    
    elif step == "get_experience":
        data["tiempo_operacion"] = message
        data["phone"] = phone
        
        # Notificar al asesor
        notify_advisor(data, "empresarial")
        
        confirmation_text = f"""✅ *INFORMACIÓN REGISTRADA*

Hemos registrado tu solicitud de crédito empresarial.

*Resumen:*
• 👤 Nombre: {data['nombre']}
• 🏢 Empresa: {data['empresa']}
• 📊 Giro: {data['giro']}
• 💰 Monto: ${data['monto']}
• ⏳ Tiempo: {data['tiempo_operacion']} años
• 📞 Contacto: {vx_last10(phone)}

*Un asesor especializado se contactará contigo* en menos de 24 horas para:
• Analizar tu caso específico
• Diseñar plan financiero personalizado
• Explicarte todas las opciones

¡Gracias por confiar en Inbursa! 🚀"""
        
        vx_wa_send_text(phone, confirmation_text)
        USER_FLOWS.pop(phone, None)

# =============================================================================
# MANEJADOR PRINCIPAL DE MENSAJES
# =============================================================================

def handle_incoming_message(phone, message):
    message_lower = message.lower().strip()
    
    # Detectar campañas desde redes sociales
    if "préstamoimss" in message_lower or "prestamoimss" in message_lower:
        return start_imss_flow(phone, "redes_sociales")
    
    elif "créditoempresarial" in message_lower or "creditoempresarial" in message_lower:
        return start_empresarial_flow(phone, "redes_sociales")
    
    # Verificar si el usuario está en un flujo activo
    if phone in USER_FLOWS:
        user_flow = USER_FLOWS[phone]
        
        if user_flow["flow"] == "imss":
            handle_imss_response(phone, message, user_flow)
        elif user_flow["flow"] == "empresarial":
            handle_empresarial_response(phone, message, user_flow)
        return
    
    # Menú principal
    menu_text = """👋 ¡Hola! Soy tu asistente de *Inbursa*

Estamos aquí para ayudarte con:

🏥 *CRÉDITO IMSS*
- Pensionados, Jubilados y Activos IMSS
- Tasas preferenciales 30.9% CAT
- Hasta $650,000 y 60 meses

🏢 *CRÉDITO EMPRESARIAL*  
- Capital de trabajo y expansión
- Planes a la medida
- Asesoría especializada

*Responde con:*
1. Crédito IMSS
2. Crédito Empresarial
3. Otros productos"""
    
    vx_wa_send_text(phone, menu_text)

# =============================================================================
# ENDPOINTS FLASK
# =============================================================================

@app.route("/")
def home():
    return jsonify({"status": "active", "service": "Vicky Bot"})

@app.route("/ext/health")
def ext_health():
    return jsonify({"status": "ok"})

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado")
        return challenge
    else:
        logging.warning("❌ Verificación fallida")
        return "Verificación fallida", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    logging.info(f"📩 Mensaje recibido: {json.dumps(data)[:300]}")

    if data and "entry" in data:
        for entry in data["entry"]:
            for change in entry.get("changes", []):
                if change.get("field") == "messages":
                    value = change.get("value", {})
                    if "messages" in value:
                        for msg in value["messages"]:
                            if msg.get("type") == "text":
                                phone = msg.get("from")
                                message = msg.get("text", {}).get("body", "").strip()
                                
                                # Evitar procesar duplicados
                                msg_id = msg.get("id")
                                if msg_id in PROCESSED_MESSAGE_IDS:
                                    continue
                                PROCESSED_MESSAGE_IDS[msg_id] = datetime.now()
                                
                                # Manejar el mensaje
                                handle_incoming_message(phone, message)
    
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
