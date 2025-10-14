import os
import json
import logging
import requests
import re
import sqlite3
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
from datetime import datetime
import openai

# ---------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

user_state = {}
user_data = {}

# ---------------------------------------------------------------
# CONFIGURACIÓN DE LOGGING MEJORADA
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vicky_conversations.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# ---------------------------------------------------------------
# SISTEMA DE BASE DE DATOS PARA CONVERSACIONES
# ---------------------------------------------------------------
def init_database():
    """Inicializa la base de datos SQLite"""
    try:
        conn = sqlite3.connect('conversations.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                user_message TEXT,
                bot_response TEXT,
                funnel TEXT,
                message_type TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_phone ON conversations(phone)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON conversations(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_funnel ON conversations(funnel)')
        
        conn.commit()
        conn.close()
        logging.info("✅ Base de datos inicializada correctamente")
    except Exception as e:
        logging.error(f"❌ Error inicializando base de datos: {e}")

def save_conversation(phone, user_message, bot_response, funnel, message_type="text"):
    """Guarda una conversación en la base de datos"""
    try:
        conn = sqlite3.connect('conversations.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO conversations (phone, user_message, bot_response, funnel, message_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (phone, user_message, bot_response, funnel, message_type))
        
        conn.commit()
        conn.close()
        logging.info(f"💾 Conversación guardada: {phone} - Funnel: {funnel}")
        return True
    except Exception as e:
        logging.error(f"❌ Error guardando conversación: {e}")
        return False

# ---------------------------------------------------------------
# FUNCIONES AUXILIARES
# ---------------------------------------------------------------
def extract_number(text):
    if not text:
        return None
    clean = text.replace(',', '').replace('$', '')
    match = re.search(r'(\d{1,9})(?:\.\d+)?\b', clean)
    if match:
        try:
            if ':' in text:
                return None
            return float(match.group(1))
        except ValueError:
            return None
    return None

def interpret_response(text):
    text_lower = (text or '').lower()
    positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes']
    negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not']
    if any(k in text_lower for k in positive_keywords):
        return 'positive'
    if any(k in text_lower for k in negative_keywords):
        return 'negative'
    return 'neutral'

def send_main_menu(phone):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone, menu, "menu", "")

def send_message(to, text, funnel="menu", user_message=""):
    """Envía mensajes y guarda en base de datos"""
    try:
        if not META_TOKEN:
            logging.error("❌ META_TOKEN no configurado")
            return False
            
        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(to),
            "type": "text",
            "text": {"body": text}
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code in (200, 201):
            save_conversation(to, user_message, text, funnel, "text")
            logging.info(f"✅ Mensaje enviado y guardado: {to}")
            return True
        else:
            logging.error(f"❌ Error API Meta: {response.status_code}")
            return False
            
    except Exception as e:
        logging.exception(f"💥 Error en send_message: {e}")
        return False

def send_whatsapp_message(to, text):
    return send_message(to, text, "system", "")

# ---------------------------------------------------------------
# FLUJO PARA CRÉDITOS EMPRESARIALES (CORREGIDO)
# ---------------------------------------------------------------
def funnel_credito_empresarial(user_id, user_message):
    state = user_state.get(user_id, "menu_tipo_credito")
    datos = user_data.get(user_id, {})
    
    # Permitir salir en cualquier momento
    if user_message.lower() in ["menu", "menú", "salir", "volver", "atrás"]:
        send_message(user_id, "Volviendo al menú principal...", "credito_empresarial", user_message)
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "menu"})

    # Paso 1: Mostrar tipos de crédito
    if state == "menu_tipo_credito":
        menu_creditos = (
            "🏢 *CRÉDITOS EMPRESARIALES - OPCIONES DISPONIBLES*\n\n"
            "1️⃣ *Crédito Simple*\n"
            "   - Sin garantía\n"
            "   - Tasas desde 18% anual\n"
            "   - Hasta 3 años de plazo\n\n"
            "2️⃣ *Factoraje*\n"
            "   - Adelanta tus facturas por cobrar\n"
            "   - Tasas desde 1.8% mensual\n"
            "   - Hasta 130 días\n\n"
            "3️⃣ *Revolvente*\n"
            "   - Línea de crédito flexible\n"
            "   - Tasas 3% mensual\n"
            "   - Hasta 45 días\n\n"
            "💡 *Escribe 'menú' en cualquier momento para volver*\n\n"
            "Escribe el *número* del crédito que te interesa:"
        )
        send_message(user_id, menu_creditos, "credito_empresarial", user_message)
        user_state[user_id] = "pregunta_tipo_credito"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 2: Preguntar tipo de crédito seleccionado
    if state == "pregunta_tipo_credito":
        if user_message in ["1", "crédito simple", "credito simple"]:
            user_data[user_id] = {"tipo_credito": "Crédito Simple"}
            send_message(user_id,
                "💼 *CRÉDITO SIMPLE*\n\n"
                "¿Qué tipo de empresa tienes?\n\n"
                "1️⃣ Persona Física con Actividad Empresarial (PFAE)\n"
                "2️⃣ Persona Moral (PM)\n\n"
                "Responde con el número:"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_tipo_empresa"
        elif user_message in ["2", "factoraje"]:
            user_data[user_id] = {"tipo_credito": "Factoraje"}
            send_message(user_id,
                "📄 *FACTORAJE*\n\n"
                "¿Qué tipo de empresa tienes?\n\n"
                "1️⃣ Persona Física con Actividad Empresarial (PFAE)\n"
                "2️⃣ Persona Moral (PM)\n\n"
                "Responde con el número:"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_tipo_empresa"
        elif user_message in ["3", "revolvente"]:
            user_data[user_id] = {"tipo_credito": "Revolvente"}
            send_message(user_id,
                "🔄 *REVOLVENTE*\n\n"
                "Este producto está dirigido a Personas Morales (PM) con ventas mínimas de 50 millones de pesos.\n\n"
                "¿Tu empresa cumple con estos requisitos?"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_cumple_requisitos_revolvente"
        else:
            send_message(user_id, 
                "❌ Opción no válida. Por favor elige 1, 2 o 3:\n\n"
                "1️⃣ Crédito Simple\n2️⃣ Factoraje\n3️⃣ Revolvente\n\n"
                "O escribe 'menú' para volver."
            , "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 3: Para Revolvente - verificar requisitos
    if state == "pregunta_cumple_requisitos_revolvente":
        resp = interpret_response(user_message)
        if resp == "positive":
            send_message(user_id, "Excelente. Continuemos con tu solicitud:", "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_antiguedad_fiscal"
        elif resp == "negative":
            send_message(user_id, 
                "Entiendo. Te recomiendo revisar nuestras otras opciones.\n\n"
                "¿Te gustaría conocer más sobre Crédito Simple o Factoraje? (responde sí o no)"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_otras_opciones"
        else:
            send_message(user_id, "Por favor responde *sí* o *no*", "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 3b: Preguntar si quiere otras opciones
    if state == "pregunta_otras_opciones":
        resp = interpret_response(user_message)
        if resp == "positive":
            user_state[user_id] = "menu_tipo_credito"
            return funnel_credito_empresarial(user_id, "")
        else:
            send_message(user_id, "De acuerdo. Si cambias de opinión, escribe 'empresarial'.", "credito_empresarial", user_message)
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "menu"})

    # Paso 4: Preguntar tipo de empresa
    if state == "pregunta_tipo_empresa":
        if user_message in ["1", "pfae", "persona física"]:
            user_data[user_id]["tipo_empresa"] = "PFAE"
            send_message(user_id, "¿Cuánto tiempo tiene operando tu empresa? (antigüedad fiscal)", "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_antiguedad_fiscal"
        elif user_message in ["2", "pm", "persona moral"]:
            user_data[user_id]["tipo_empresa"] = "PM"
            send_message(user_id, "¿Cuánto tiempo tiene operando tu empresa? (antigüedad fiscal)", "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_antiguedad_fiscal"
        else:
            send_message(user_id, "Por favor responde con 1 (PFAE) o 2 (PM)", "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 5: Preguntar antigüedad fiscal
    if state == "pregunta_antiguedad_fiscal":
        if user_message.lower() in ["menu", "menú", "salir"]:
            send_message(user_id, "Volviendo al menú principal...", "credito_empresarial", user_message)
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "menu"})
            
        antiguedad = extract_number(user_message)
        if antiguedad is None:
            send_message(user_id, "Por favor indica el tiempo en meses o años. Ejemplo: '6 meses'", "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        
        user_data[user_id]["antiguedad_fiscal"] = antiguedad
        send_message(user_id, 
            "¿Cómo está tu historial en Buró de Crédito?\n\n"
            "1️⃣ Sin vencimientos\n"
            "2️⃣ Con vencimientos menores a 30 mil pesos\n"
            "3️⃣ Con vencimientos mayores\n\n"
            "Responde con el número:"
        , "credito_empresarial", user_message)
        user_state[user_id] = "pregunta_buro_credito"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 6: Preguntar situación en Buró de Crédito
    if state == "pregunta_buro_credito":
        if user_message in ["1", "sin vencimientos"]:
            user_data[user_id]["buro_credito"] = "Sin vencimientos"
        elif user_message in ["2", "vencimientos menores"]:
            user_data[user_id]["buro_credito"] = "Vencimientos menores a 30k"
        elif user_message in ["3", "vencimientos mayores"]:
            user_data[user_id]["buro_credito"] = "Vencimientos mayores"
        else:
            send_message(user_id, "Por favor elige una opción válida (1, 2 o 3)", "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        
        send_message(user_id, "¿Cuál es tu nombre completo?", "credito_empresarial", user_message)
        user_state[user_id] = "pregunta_nombre_empresarial"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 7: Preguntar nombre
    if state == "pregunta_nombre_empresarial":
        user_data[user_id]["nombre"] = user_message.title()
        send_message(user_id, "¿Cuál es tu teléfono de contacto?", "credito_empresarial", user_message)
        user_state[user_id] = "pregunta_telefono_empresarial"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 8: Preguntar teléfono
    if state == "pregunta_telefono_empresarial":
        user_data[user_id]["telefono"] = user_message
        send_message(user_id, "¿En qué ciudad se encuentra tu empresa?", "credito_empresarial", user_message)
        user_state[user_id] = "pregunta_ciudad_empresarial"
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 9: Preguntar ciudad
    if state == "pregunta_ciudad_empresarial":
        user_data[user_id]["ciudad"] = user_message.title()
        
        # Cierre y notificación al asesor
        datos = user_data.get(user_id, {})
        formatted = (
            f"🏢 *NUEVO PROSPECTO - CRÉDITO EMPRESARIAL*\n"
            f"Nombre: {datos.get('nombre', 'N/D')}\n"
            f"Tipo: {datos.get('tipo_credito', 'N/D')}\n"
            f"Empresa: {datos.get('tipo_empresa', 'N/D')}\n"
            f"Antigüedad: {datos.get('antiguedad_fiscal', 'N/D')} meses\n"
            f"Buró: {datos.get('buro_credito', 'N/D')}\n"
            f"Ciudad: {datos.get('ciudad', 'N/D')}\n"
            f"Teléfono: {datos.get('telefono', 'N/D')}\n"
            f"📞 WhatsApp: {user_id}"
        )
        
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
        
        send_message(user_id,
            "✅ *¡Excelente! Hemos recibido tu información*\n\n"
            "Un asesor especializado se pondrá en contacto contigo en menos de 24 horas.\n\n"
            "Gracias por confiar en nosotros! 🚀"
        , "credito_empresarial", user_message)
        
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Estado no reconocido - resetear
    send_message(user_id, "Volviendo al menú principal...", "credito_empresarial", user_message)
    send_main_menu(user_id)
    user_state.pop(user_id, None)
    user_data.pop(user_id, None)
    return jsonify({"status": "ok", "funnel": "menu"})

# ---------------------------------------------------------------
# FLUJO PARA PRÉSTAMOS IMSS (EXISTENTE)
# ---------------------------------------------------------------
def funnel_prestamo_imss(user_id, user_message):
    state = user_state.get(user_id, "menu_mostrar_beneficios")
    datos = user_data.get(user_id, {})

    # Permitir salir en cualquier momento
    if user_message.lower() in ["menu", "menú", "salir", "volver", "atrás"]:
        send_message(user_id, "Volviendo al menú principal...", "prestamo_imss", user_message)
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "menu"})

    # [MANTENER TODO EL CÓDIGO IMSS EXISTENTE...]
    # El resto del código IMSS se mantiene igual

# ---------------------------------------------------------------
# ENDPOINT PRINCIPAL CORREGIDO
# ---------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json()
        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")
        user_message = ""
        if message_type == "text":
            user_message = message["text"]["body"].strip()
        else:
            send_message(phone_number, "Por ahora solo puedo procesar mensajes de texto 📩", "system", user_message)
            return jsonify({"status": "ok"}), 200

        logging.info(f"📱 Mensaje de {phone_number}: '{user_message}'")

        menu_options = {
            "1": "prestamo_imss",
            "préstamo": "prestamo_imss",
            "prestamo": "prestamo_imss",
            "imss": "prestamo_imss",
            "ley 73": "prestamo_imss",
            "pension": "prestamo_imss",
            "pensión": "prestamo_imss",
            "2": "seguro_auto",
            "seguro auto": "seguro_auto",
            "seguros de auto": "seguro_auto",
            "auto": "seguro_auto",
            "3": "seguro_vida",
            "seguro vida": "seguro_vida",
            "seguros de vida": "seguro_vida",
            "seguro salud": "seguro_vida",
            "vida": "seguro_vida",
            "4": "vrim",
            "tarjetas médicas": "vrim",
            "tarjetas medicas": "vrim",
            "vrim": "vrim",
            "5": "empresarial",
            "financiamiento empresarial": "empresarial",
            "empresa": "empresarial",
            "negocio": "empresarial",
            "pyme": "empresarial",
            "crédito empresarial": "empresarial",
            "credito empresarial": "empresarial"
        }

        option = menu_options.get(user_message.lower())
        current_state = user_state.get(phone_number, "")

        # ✅ CORRECCIÓN CRÍTICA: DETECCIÓN MEJORADA DE ESTADOS
        # Si está en flujo empresarial, priorizar ese flujo
        estados_empresariales = [
            "menu_tipo_credito", "pregunta_tipo_credito", "pregunta_tipo_empresa", 
            "pregunta_antiguedad_fiscal", "pregunta_buro_credito", "pregunta_nombre_empresarial",
            "pregunta_telefono_empresarial", "pregunta_ciudad_empresarial", "pregunta_cumple_requisitos_revolvente",
            "pregunta_otras_opciones"
        ]
        
        if current_state and any(estado in current_state for estado in estados_empresariales):
            return funnel_credito_empresarial(phone_number, user_message)

        # FLUJO IMSS: Si está en embudo IMSS
        if current_state and ("prestamo_imss" in current_state or "pregunta_" in current_state):
            return funnel_prestamo_imss(phone_number, user_message)

        # Opción 1: Iniciar embudo IMSS
        if option == "prestamo_imss":
            user_state[phone_number] = "menu_mostrar_beneficios"
            return funnel_prestamo_imss(phone_number, user_message)

        # Opción 5: Iniciar embudo EMPRESARIAL
        if option == "empresarial":
            user_state[phone_number] = "menu_tipo_credito"
            return funnel_credito_empresarial(phone_number, user_message)

        # Otros servicios y comandos
        if option == "seguro_auto":
            send_message(phone_number, "🚗 *Seguros de Auto Inbursa* - Un asesor se comunicará contigo.", "seguro_auto", user_message)
            send_whatsapp_message(ADVISOR_NUMBER, f"🚗 NUEVO INTERESADO EN SEGURO DE AUTO\n📞 {phone_number}")
        elif option == "seguro_vida":
            send_message(phone_number, "🏥 *Seguros de Vida y Salud Inbursa* - Un asesor se comunicará contigo.", "seguro_vida", user_message)
            send_whatsapp_message(ADVISOR_NUMBER, f"🏥 NUEVO INTERESADO EN SEGURO VIDA/SALUD\n📞 {phone_number}")
        elif option == "vrim":
            send_message(phone_number, "💳 *Tarjetas Médicas VRIM* - Un asesor se comunicará contigo.", "vrim", user_message)
            send_whatsapp_message(ADVISOR_NUMBER, f"💳 NUEVO INTERESADO EN TARJETAS VRIM\n📞 {phone_number}")
        elif user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            send_main_menu(phone_number)
        elif user_message.lower() in ["hola", "hi", "hello", "buenas"]:
            send_main_menu(phone_number)
        else:
            send_main_menu(phone_number)

        return jsonify({"status": "ok", "funnel": "menu"})

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

# [MANTENER EL RESTO DE ENDPOINTS Y CONFIGURACIÓN...]

if __name__ == "__main__":
    init_database()
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot corregido en puerto {port}")
    app.run(host="0.0.0.0", port=port)
