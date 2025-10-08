import os
import json
import logging
import requests
import re
import threading
import pytz
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
IMSS_PDF_DRIVE_ID = os.getenv("IMSS_PDF_DRIVE_ID")  # Para consultas documentación

# Google Drive base
def _drive_service():
    creds = Credentials.from_service_account_info(json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")))
    return build("drive", "v3", credentials=creds)

def save_file_to_drive(local_path, filename, folder_id):
    service = _drive_service()
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return uploaded.get("id")

# 🧠 Controles en memoria
PROCESSED_MESSAGE_IDS = {}
GREETED_USERS = {}
LAST_INTENT = {}
USER_CONTEXT = {}
USER_FLOWS = {}

MSG_TTL = 600
GREET_TTL = 24 * 3600
CTX_TTL = 4 * 3600

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
        logging.info(f"vx_wa_send_text {r.status_code} {r.text[:160]}")
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

def vx_wa_send_template(to, template, params=None):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    comps = []
    if params:
        comps = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in params.values()]
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": "es_MX"},
            **({"components": comps} if comps else {})
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        logging.info(f"vx_wa_send_template {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_template error: {e}")
        return False

# Helpers
def vx_last10(phone: str) -> str:
    if not phone:
        return ""
    p = re.sub(r"[^\d]", "", str(phone))
    p = re.sub(r"^(52|521)", "", p)
    return p[-10:] if len(p) >= 10 else p

def vx_sheet_find_by_phone(last10: str):
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        sheets_id = os.getenv("SHEETS_ID_LEADS")
        sheets_title = os.getenv("SHEETS_TITLE_LEADS")
        if not creds_json or not sheets_id or not sheets_title:
            return None
        creds = Credentials.from_service_account_info(json.loads(creds_json))
        client = gspread.authorize(creds)
        ws = client.open_by_key(sheets_id).worksheet(sheets_title)
        rows = ws.get_all_records()
        for row in rows:
            if vx_last10(row.get("WhatsApp", "")) == last10:
                return row
        return None
    except Exception as e:
        logging.error(f"vx_sheet_find_by_phone error: {e}")
        return None

def notify_advisor(prospect_data, flow_type):
    """Notifica al asesor sobre nuevo prospecto calificado"""
    if not ADVISOR_WHATSAPP:
        logging.warning("No hay número de asesor configurado")
        return False
    
    if flow_type == "imss":
        message = f"🎯 *NUEVO PROSPECTO CALIFICADO - CRÉDITO IMSS*\n\n"
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
# FLUJO CRÉDITO IMSS MEJORADO (Basado en documento oficial)
# =============================================================================

def start_imss_flow(phone, campaign_source="redes_sociales"):
    """Inicia flujo de Crédito IMSS con beneficios de nómina Inbursa"""
    USER_FLOWS[phone] = {
        "flow": "imss",
        "step": "welcome_benefits",
        "data": {
            "campaign": campaign_source,
            "timestamp": datetime.now()
        }
    }
    
    welcome_text = """🏥 *CRÉDITO IMSS - INBURSA*

¡Te damos la bienvenida! Somos tu entidad financiera autorizada por el IMSS.

*🌟 BENEFICIOS EXCLUSIVOS con nómina Inbursa:*

✓ *Tasa preferencial 30.9% CAT* (la más competitiva)
✓ *Seguro de vida incluido* sin costo adicional  
✓ *Sin comisiones* por apertura o manejo de cuenta
✓ *Plazos hasta 60 meses* con pagos fijos
✓ *Dinero en tu cuenta en 24-72 horas* después de aprobado
✓ *Sin penalización* por pagos adelantados

*📋 REQUISITOS IMSS:*
• Ser pensionado Ley 73, jubilado o activo IMSS
• Edad + plazo no mayor a 78 años
• Capacidad de crédito en portal IMSS

*¿Te interesa conocer tu crédito preaprobado?*"""
    
    return vx_wa_send_interactive(phone, welcome_text, 
                                ["Sí, quiero mi crédito", "Necesito más información"])

def handle_imss_response(phone, message, user_flow):
    """Maneja las respuestas del flujo IMSS"""
    step = user_flow["step"]
    data = user_flow["data"]
    
    if step == "welcome_benefits":
        if "sí" in message.lower() or "si" in message.lower() or "quiero" in message.lower():
            user_flow["step"] = "ask_nomina"
            nomina_text = """💳 *BENEFICIO NÓMINA INBURSA*

Al tener tu pensión/nómina con nosotros, obtienes:
• *Tasa 30.9% CAT* vs 36%-59% de otros bancos
• *Atención preferente* y procesos más rápidos
• *Seguros adicionales* sin costo

*¿Tienes tu pensión en Inbursa o te gustaría cambiarla?*

💡 *Ventajas del cambio:*
- No cierras tu cuenta actual
- Puedes regresar después de 3 meses si no estás conforme
- Acceso inmediato a mejores condiciones"""
            
            vx_wa_send_interactive(phone, nomina_text, 
                                 ["Sí, tengo Inbursa", "Quiero cambiarme", "Prefiero no cambiar"])
        
        else:
            user_flow["step"] = "more_info"
            info_text = """📚 *INFORMACIÓN CRÉDITO IMSS*

*Tipos de Crédito Disponibles:*
1. *Nuevos* - Si es tu primer crédito o tienes capacidad disponible
2. *Segundos créditos* - Si tienes crédito vigente pero capacidad disponible  
3. *Renovaciones* - Si tienes crédito Inbursa con +24 pagos
4. *Compras de cartera* - Si tienes crédito con otro banco

*Montos:* Desde $5,000 hasta $650,000
*Plazos:* 6, 12, 18, 24, 30, 36, 42, 48, 54, 60 meses

*¿Te interesa alguno de estos productos?*"""
            
            vx_wa_send_interactive(phone, info_text, 
                                 ["Crédito nuevo", "Compra de cartera", "Renovación"])
    
    elif step == "ask_nomina":
        if "sí" in message.lower() or "tengo" in message.lower() or "cambiarme" in message.lower():
            data["nomina_inbursa"] = True
            user_flow["step"] = "ask_client_type"
            vx_wa_send_text(phone, "✅ *Excelente elección* - Obtendrás la tasa preferencial 30.9% CAT")
            
            type_text = """👤 *¿A cuál de estos grupos perteneces?*

1. *Pensionado Ley 73* (vejez, viudez, cesantía)
2. *Jubilado del IMSS* (ex trabajador)  
3. *Activo IMSS* (Mando, Estatuto A, Confianza A)

Responde con el número de tu opción:"""
            vx_wa_send_text(phone, type_text)
        
        else:
            data["nomina_inbursa"] = False
            user_flow["step"] = "ask_client_type"
            vx_wa_send_text(phone, "Entendido. Continuemos con la evaluación...")
            
            type_text = """👤 *¿A cuál de estos grupos perteneces?*

1. *Pensionado Ley 73* (vejez, viudez, cesantía)
2. *Jubilado del IMSS* (ex trabajador)  
3. *Activo IMSS* (Mando, Estatuto A, Confianza A)

Responde con el número:"""
            vx_wa_send_text(phone, type_text)
    
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
                
                # Calcular plazo máximo según edad
                plazo_maximo = 78 - edad
                if plazo_maximo > 60:
                    plazo_maximo = 60
                
                data["plazo_maximo"] = plazo_maximo
                user_flow["step"] = "ask_capacity"
                
                capacity_text = f"""💳 *CAPACIDAD DE CRÉDITO*

Para verificar tu elegibilidad, necesito que consultes tu capacidad en el portal del IMSS:

*Portal según tu tipo:*
• *Pensionados Ley 73:* https://mc1.imss.gob.mx/mclpe/auth/login
• *Jubilados/Activos:* https://swap.imss.gob.mx/suap/auth/login

*¿Cuál es el monto máximo que te aprueba el sistema del IMSS?* (ingresa solo números)"""
                
                vx_wa_send_text(phone, capacity_text)
        except:
            vx_wa_send_text(phone, "Por favor, ingresa tu edad en números:")
    
    elif step == "ask_capacity":
        try:
            capacidad = float(message.replace("$", "").replace(",", ""))
            data["capacidad_pago"] = capacidad
            
            # Verificar requisitos mínimos
            if capacidad >= 5000:  # Monto mínimo según documento
                user_flow["step"] = "get_name"
                data["cumple_requisitos"] = True
                
                vx_wa_send_text(phone, f"✅ *¡Excelente! Tienes capacidad aprobada por: ${capacidad:,.2f}*")
                vx_wa_send_text(phone, "👤 *Para continuar, ingresa tu nombre completo:*")
            else:
                data["cumple_requisitos"] = False
                reject_text = f"""❌ *Capacidad insuficiente*

El monto mínimo requerido es $5,000. Tu capacidad actual es ${capacidad:,.2f}.

*Recomendaciones:*
• Esperar hasta día 15 del mes (actualización IMSS)
• Verificar que tu ingreso mensual sea mayor a $10,000
• Intentar nuevamente el próximo mes

¿Deseas que te contactemos cuando tengas mayor capacidad?"""
                
                vx_wa_send_interactive(phone, reject_text, 
                                      ["Sí, contactarme", "No, gracias"])
                
        except:
            vx_wa_send_text(phone, "Por favor, ingresa solo el monto en números:")
    
    elif step == "get_name":
        data["nombre"] = message
        data["phone"] = phone
        
        # PROSPECTO CALIFICADO - Notificar asesor
        notify_advisor(data, "imss")
        
        success_text = f"""🎉 *¡FELICIDADES! ESTÁS PRE-APROBADO*

*Resumen de tu crédito:*
• 👤 Nombre: {data['nombre']}
• 📞 Teléfono: {vx_last10(phone)}
• 🎂 Edad: {data['edad']} años ✓
• 📊 Tipo: {data['tipo_cliente']} ✓
• 💰 Capacidad: ${data['capacidad_pago']:,.2f} ✓
• 🏦 Nómina Inbursa: {'Sí' if data.get('nomina_inbursa') else 'No'} ✓
• 📅 Plazo máximo: {data['plazo_maximo']} meses ✓

*📍 PRÓXIMOS PASOS:*
1. *Notificaré inmediatamente a tu asesor*
2. *Te contactará en menos de 24 horas*
3. *Reunir documentación requerida*
4. *Firma digital y desembolso*

*📋 Documentación necesaria:*
• INE vigente
• Comprobante de domicilio 
• Estado de cuenta (donde recibes pensión)
• Video selfie testimonial

*Tu asesor se pondrá en contacto contigo pronto.*"""
        
        vx_wa_send_text(phone, success_text)
        USER_FLOWS.pop(phone, None)  # Finalizar flujo

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

*Líneas disponibles:*
• Capital de trabajo
• Maquinaria y equipo  
• Remodelación y expansión
• Tecnología e innovación

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
        user_flow["step"] = "schedule_appointment"
        
        schedule_text = """📅 *AGENDEMOS TU CITA CON ESPECIALISTA*

Nuestro asesor analizará tu caso y diseñará un plan financiero personalizado.

*Horarios disponibles:*
1. Lunes - 10:00 AM
2. Martes - 2:00 PM  
3. Miércoles - 4:00 PM
4. Jueves - 11:00 AM
5. Viernes - 3:00 PM

*Responde con el número de tu horario preferido:*"""
        
        vx_wa_send_text(phone, schedule_text)
    
    elif step == "schedule_appointment":
        time_slots = {
            "1": "Lunes - 10:00 AM",
            "2": "Martes - 2:00 PM", 
            "3": "Miércoles - 4:00 PM",
            "4": "Jueves - 11:00 AM",
            "5": "Viernes - 3:00 PM"
        }
        
        if message in time_slots:
            data["cita"] = time_slots[message]
            data["phone"] = phone
            
            # Notificar al asesor
            notify_advisor(data, "empresarial")
            
            confirmation_text = f"""✅ *CITA CONFIRMADA*

📅 *Fecha:* {data['cita']}
👨‍💼 *Especialista:* Asesor Empresarial
📞 *Contacto:* {vx_last10(phone)}

*Tu asesor se contactará contigo* para:
• Analizar tu caso específico
• Diseñar plan financiero personalizado
• Explicarte todas las opciones

💼 *Recomendación:* Ten a la mano documentación de tu empresa."""
            
            vx_wa_send_text(phone, confirmation_text)
            USER_FLOWS.pop(phone, None)  # Finalizar flujo
        else:
            vx_wa_send_text(phone, "Por favor, elige una opción del 1 al 5:")

# =============================================================================
# MANEJADOR PRINCIPAL DE MENSAJES
# =============================================================================

def handle_incoming_message(phone, message):
    """Maneja todos los mensajes entrantes"""
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
    
    # Menú principal para mensajes no dirigidos
    menu_text = """👋 ¡Hola! Soy tu asistente de *Inbursa*

Estamos aquí para ayudarte con:

🏥 *CRÉDITO IMSS*
- Pensionados Ley 73, Jubilados y Activos
- Tasas preferenciales 30.9% CAT
- Hasta $650,000 y 60 meses

🏢 *CRÉDITO EMPRESARIAL*  
- Capital de trabajo y expansión
- Planes a la medida de tu negocio
- Asesoría especializada

💳 *OTROS PRODUCTOS*
- Terminales punto de venta
- Seguros y tarjetas
- Inversiones

*Responde con el número de tu interés:*
1. Crédito IMSS
2. Crédito Empresarial
3. Otros productos"""
    
    vx_wa_send_text(phone, menu_text)

# =============================================================================
# ENDPOINTS FLASK
# =============================================================================

@app.route("/ext/health")
def ext_health():
    return jsonify({"status": "ok"})

@app.route("/ext/send-promo", methods=["POST"])
def ext_send_promo():
    data = request.get_json(force=True, silent=True) or {}
    to = data.get("to")
    text = data.get("text")
    template = data.get("template")
    params = data.get("params", {})
    use_secom = data.get("secom", False)

    targets = []
    if isinstance(to, str):
        targets = [to]
    elif isinstance(to, list):
        targets = [str(x) for x in to if str(x).strip()]

    if use_secom:
        try:
            creds = Credentials.from_service_account_info(json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")))
            gs = gspread.authorize(creds)
            sh = gs.open_by_key(os.getenv("SHEETS_ID_LEADS"))
            ws = sh.worksheet(os.getenv("SHEETS_TITLE_LEADS"))
            numbers = [str(r.get("WhatsApp", "")) for r in ws.get_all_records() if r.get("WhatsApp")]
            targets.extend(numbers)
        except Exception as e:
            logging.error(f"Error leyendo SECOM en send-promo: {e}")

    targets = list(set(targets))

    def _worker():
        results = []
        for num in targets:
            ok = False
            try:
                if template:
                    ok = vx_wa_send_template(num, template, params)
                elif text:
                    ok = vx_wa_send_text(num, text)
                results.append({"to": num, "sent": ok})
            except Exception as e:
                logging.error(f"send_promo worker error: {e}")
                results.append({"to": num, "sent": False, "error": str(e)})
        logging.info(f"send_promo done: {results}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"accepted": True, "count": len(targets)}), 202

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

# Limpiar mensajes procesados antiguos
def cleanup_processed_messages():
    now = datetime.now()
    expired = [msg_id for msg_id, timestamp in PROCESSED_MESSAGE_IDS.items() 
               if (now - timestamp).total_seconds() > MSG_TTL]
    for msg_id in expired:
        PROCESSED_MESSAGE_IDS.pop(msg_id, None)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
