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
        
        # Índices para búsquedas más rápidas
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

def get_conversations(limit=100, phone=None):
    """Obtiene conversaciones de la base de datos"""
    try:
        conn = sqlite3.connect('conversations.db')
        cursor = conn.cursor()
        
        if phone:
            cursor.execute('''
                SELECT phone, user_message, bot_response, funnel, message_type, timestamp 
                FROM conversations 
                WHERE phone = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (phone, limit))
        else:
            cursor.execute('''
                SELECT phone, user_message, bot_response, funnel, message_type, timestamp 
                FROM conversations 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (limit,))
        
        conversations = cursor.fetchall()
        conn.close()
        
        return conversations
    except Exception as e:
        logging.error(f"❌ Error obteniendo conversaciones: {e}")
        return []

def get_conversation_stats():
    """Obtiene estadísticas de conversaciones"""
    try:
        conn = sqlite3.connect('conversations.db')
        cursor = conn.cursor()
        
        # Total conversaciones
        cursor.execute('SELECT COUNT(*) FROM conversations')
        total_conv = cursor.fetchone()[0]
        
        # Conversaciones únicas
        cursor.execute('SELECT COUNT(DISTINCT phone) FROM conversations')
        unique_users = cursor.fetchone()[0]
        
        # Por funnel
        cursor.execute('SELECT funnel, COUNT(*) FROM conversations GROUP BY funnel')
        funnel_stats = cursor.fetchall()
        
        # Hoy
        cursor.execute('SELECT COUNT(*) FROM conversations WHERE DATE(timestamp) = DATE("now")')
        today_conv = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "total_conversations": total_conv,
            "unique_users": unique_users,
            "today_conversations": today_conv,
            "funnel_stats": dict(funnel_stats)
        }
    except Exception as e:
        logging.error(f"❌ Error obteniendo estadísticas: {e}")
        return {}

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

def is_valid_name(text):
    if not text or len(text.strip()) < 2:
        return False
    if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()):
        return True
    return False

def is_valid_phone(text):
    if not text:
        return False
    clean_phone = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean_phone) is not None

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

def ask_gpt(prompt, model="gpt-3.5-turbo", temperature=0.7):
    try:
        response = openai.ChatCompletion.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=400
        )
        return response.choices[0].message["content"].strip()
    except Exception as e:
        logging.exception(f"Error con OpenAI: {e}")
        return "Lo siento, ocurrió un error al consultar GPT."

def is_gpt_command(msg):
    return re.match(r'^\s*gpt\s*:', msg.lower())

# ---------------------------------------------------------------
# FLUJO PARA CRÉDITOS EMPRESARIALES (CORREGIDO - SIN CICLOS)
# ---------------------------------------------------------------
def funnel_credito_empresarial(user_id, user_message):
    state = user_state.get(user_id, "menu_tipo_credito")
    datos = user_data.get(user_id, {})
    
    # Permitir salir en cualquier momento con "menú" o "salir"
    if user_message.lower() in ["menu", "menú", "salir", "volver", "atrás"]:
        send_message(user_id, "Volviendo al menú principal...", "credito_empresarial", user_message)
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "menu"})

    # Paso 1: Mostrar tipos de crédito disponibles
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
            "💡 *Escribe 'menú' en cualquier momento para volver al menú principal*\n\n"
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
            # CORRECCIÓN: Manejar opción no válida sin ciclarse
            send_message(user_id, 
                "❌ Opción no válida. Por favor elige 1, 2 o 3:\n\n"
                "1️⃣ Crédito Simple\n"
                "2️⃣ Factoraje\n" 
                "3️⃣ Revolvente\n\n"
                "O escribe 'menú' para volver al menú principal."
            , "credito_empresarial", user_message)
            # Mantener el mismo estado para reintentar
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
                "Entiendo. Te recomiendo revisar nuestras otras opciones de crédito que pueden adaptarse mejor a tus necesidades.\n\n"
                "¿Te gustaría conocer más sobre Crédito Simple o Factoraje? (responde sí o no)"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_otras_opciones"
        else:
            send_message(user_id, 
                "Por favor responde *sí* o *no*:\n\n"
                "¿Tu empresa cumple con los requisitos para Revolvente?"
            , "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 3b: Preguntar si quiere otras opciones
    if state == "pregunta_otras_opciones":
        resp = interpret_response(user_message)
        if resp == "positive":
            user_state[user_id] = "menu_tipo_credito"
            return funnel_credito_empresarial(user_id, "")  # Reiniciar el flujo
        else:
            send_message(user_id, 
                "De acuerdo. Si cambias de opinión, siempre puedes escribir 'empresarial' para volver a ver las opciones.\n\n"
                "¿Te interesa algún otro servicio?"
            , "credito_empresarial", user_message)
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "menu"})
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 4: Preguntar tipo de empresa (para Crédito Simple y Factoraje)
    if state == "pregunta_tipo_empresa":
        if user_message in ["1", "pfae", "persona física"]:
            user_data[user_id]["tipo_empresa"] = "PFAE"
            send_message(user_id, 
                "¿Cuánto tiempo tiene operando tu empresa? (antigüedad fiscal)"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_antiguedad_fiscal"
        elif user_message in ["2", "pm", "persona moral"]:
            user_data[user_id]["tipo_empresa"] = "PM"
            send_message(user_id, 
                "¿Cuánto tiempo tiene operando tu empresa? (antigüedad fiscal)"
            , "credito_empresarial", user_message)
            user_state[user_id] = "pregunta_antiguedad_fiscal"
        else:
            send_message(user_id, 
                "Por favor responde con 1 (PFAE) o 2 (PM):\n\n"
                "1️⃣ Persona Física con Actividad Empresarial (PFAE)\n"
                "2️⃣ Persona Moral (PM)"
            , "credito_empresarial", user_message)
            return jsonify({"status": "ok", "funnel": "credito_empresarial"})
        return jsonify({"status": "ok", "funnel": "credito_empresarial"})

    # Paso 5: Preguntar antigüedad fiscal
    if state == "pregunta_antiguedad_fiscal":
        # Permitir salir
        if user_message.lower() in ["menu", "menú", "salir"]:
            send_message(user_id, "Volviendo al menú principal...", "credito_empresarial", user_message)
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "menu"})
            
        # Extraer números del mensaje
        antiguedad = extract_number(user_message)
        if antiguedad is None:
            send_message(user_id, 
                "Por favor indica el tiempo en meses o años. Ejemplo: '6 meses' o '2 años'\n\n"
                "O escribe 'menú' para volver al menú principal."
            , "credito_empresarial", user_message)
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
            send_message(user_id, 
                "Por favor elige una opción válida (1, 2 o 3):\n\n"
                "1️⃣ Sin vencimientos\n"
                "2️⃣ Con vencimientos menores a 30 mil pesos\n"
                "3️⃣ Con vencimientos mayores"
            , "credito_empresarial", user_message)
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
            "Un asesor especializado en créditos empresariales se pondrá en contacto contigo en menos de 24 horas para analizar tu caso y ofrecerte las mejores opciones.\n\n"
            "Gracias por confiar en nosotros para impulsar tu negocio! 🚀"
        , "credito_empresarial", user_message)
        
        send_message(user_id, "¿Necesitas información sobre otros servicios financieros?")
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

    # Paso 0: Mostrar beneficios y preguntar si es pensionado
    if state == "menu_mostrar_beneficios":
        send_message(user_id,
            "💰 *Beneficios del Préstamo para Pensionados IMSS (Ley 73)*\n"
            "- Montos desde $40,000 hasta $650,000\n"
            "- Descuento vía pensión (sin buró de crédito)\n"
            "- Plazos de 12 a 60 meses\n"
            "- Depósito directo a tu cuenta\n"
            "- Sin aval ni garantía"
        , "prestamo_imss", user_message)
        send_message(user_id,
            "🏦 *Beneficios adicionales si recibes tu pensión en Inbursa:*\n"
            "- Tasas preferenciales y pagos más bajos\n"
            "- Acceso a seguro de vida sin costo\n"
            "- Anticipo de nómina disponible\n"
            "- Atención personalizada 24/7\n\n"
            "*(Estos beneficios son adicionales y no son obligatorios para obtener tu crédito.)*"
        , "prestamo_imss", user_message)
        send_message(user_id,
            "¿Eres pensionado o jubilado del IMSS bajo la Ley 73?"
        , "prestamo_imss", user_message)
        user_state[user_id] = "pregunta_pensionado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 1: Pregunta pensionado
    if state == "pregunta_pensionado":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_message(user_id, "Entiendo. Te muestro otros servicios que podrían interesarte:", "prestamo_imss", user_message)
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        elif resp == "positive":
            send_message(user_id,
                "¿Cuánto recibes aproximadamente al mes por concepto de pensión?"
            , "prestamo_imss", user_message)
            user_state[user_id] = "pregunta_monto_pension"
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        else:
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.", "prestamo_imss", user_message)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 2: Monto de pensión
    if state == "pregunta_monto_pension":
        monto_pension = extract_number(user_message)
        if monto_pension is None:
            send_message(user_id, "Indica el monto mensual que recibes por pensión, ejemplo: 6500", "prestamo_imss", user_message)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        if monto_pension < 5000:
            send_message(user_id,
                "Por ahora los créditos disponibles aplican a pensiones a partir de $5,000.\n"
                "Pero puedo notificar a nuestro asesor para ofrecerte otra opción sin compromiso. ¿Deseas que lo haga?"
            , "prestamo_imss", user_message)
            user_state[user_id] = "pregunta_ofrecer_asesor"
            user_data[user_id] = {"pension_mensual": monto_pension}
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        user_data[user_id] = {"pension_mensual": monto_pension}
        send_message(user_id,
            "Perfecto 👏 ¿Qué monto de préstamo te gustaría solicitar? (mínimo $40,000)"
        , "prestamo_imss", user_message)
        user_state[user_id] = "pregunta_monto_solicitado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 2b: Ofrecer asesor por pensión baja
    if state == "pregunta_ofrecer_asesor":
        resp = interpret_response(user_message)
        if resp == "positive":
            send_message(user_id,
                "¡Listo! Un asesor te contactará para ofrecerte opciones alternativas. Gracias por confiar en nosotros 🙌."
            , "prestamo_imss", user_message)
            datos = user_data.get(user_id, {})
            formatted = (
                f"🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"Nombre: {datos.get('nombre','N/D')}\n"
                f"Número WhatsApp: {user_id}\n"
                f"Pensión mensual: ${datos.get('pension_mensual','N/D'):,.0f}\n"
                f"Estatus: Pensión baja, requiere opciones alternativas"
            )
            send_whatsapp_message(ADVISOR_NUMBER, formatted)
            send_message(user_id, "¡Listo! Además, tenemos otros servicios financieros que podrían interesarte: 👇")
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        else:
            send_message(user_id, "Perfecto, si deseas podemos continuar con otros servicios.", "prestamo_imss", user_message)
            send_message(user_id, "¡Listo! Además, tenemos otros servicios financieros que podrían interesarte: 👇")
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 3: Monto solicitado
    if state == "pregunta_monto_solicitado":
        monto_solicitado = extract_number(user_message)
        if monto_solicitado is None or monto_solicitado < 40000:
            send_message(user_id, "Indica el monto que deseas solicitar (mínimo $40,000), ejemplo: 65000", "prestamo_imss", user_message)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        user_data[user_id]["monto_solicitado"] = monto_solicitado
        send_message(user_id,
            "¿Cuál es tu nombre completo?"
        , "prestamo_imss", user_message)
        user_state[user_id] = "pregunta_nombre"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 4: Pregunta nombre
    if state == "pregunta_nombre":
        user_data[user_id]["nombre"] = user_message.title()
        send_message(user_id,
            "¿Cuál es tu teléfono de contacto?"
        , "prestamo_imss", user_message)
        user_state[user_id] = "pregunta_telefono"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 5: Pregunta teléfono
    if state == "pregunta_telefono":
        user_data[user_id]["telefono_contacto"] = user_message
        send_message(user_id,
            "¿En qué ciudad vives?"
        , "prestamo_imss", user_message)
        user_state[user_id] = "pregunta_ciudad"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 6: Pregunta ciudad
    if state == "pregunta_ciudad":
        user_data[user_id]["ciudad"] = user_message.title()
        send_message(user_id,
            "¿Ya recibes tu pensión en Inbursa?"
        , "prestamo_imss", user_message)
        user_state[user_id] = "pregunta_nomina_inbursa"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 7: Nómina Inbursa
    if state == "pregunta_nomina_inbursa":
        resp = interpret_response(user_message)
        if resp == "positive":
            send_message(user_id,
                "Excelente, con Inbursa tendrás acceso a beneficios adicionales y atención prioritaria."
            , "prestamo_imss", user_message)
            user_data[user_id]["nomina_inbursa"] = "Sí"
        elif resp == "negative":
            send_message(user_id,
                "No hay problema 😊, los beneficios adicionales solo aplican si tienes la nómina con nosotros,\n"
                "pero puedes cambiarte cuando gustes, sin costo ni compromiso."
            , "prestamo_imss", user_message)
            user_data[user_id]["nomina_inbursa"] = "No"
        else:
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.", "prestamo_imss", user_message)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        send_message(user_id,
            "¡Listo! 🎉 Tu crédito ha sido preautorizado.\n"
            "Un asesor financiero (Christian López) se pondrá en contacto contigo para continuar con el trámite.\n"
            "Gracias por tu confianza 🙌."
        , "prestamo_imss", user_message)
        datos = user_data.get(user_id, {})
        formatted = (
            f"🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
            f"Nombre: {datos.get('nombre','N/D')}\n"
            f"Número WhatsApp: {user_id}\n"
            f"Teléfono contacto: {datos.get('telefono_contacto','N/D')}\n"
            f"Ciudad: {datos.get('ciudad','N/D')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado','N/D'):,.0f}\n"
            f"Estatus: Preautorizado\n"
            f"Observación: Nómina Inbursa: {datos.get('nomina_inbursa','N/D')}"
        )
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
        send_message(user_id, "¡Listo! Además, tenemos otros servicios financieros que podrían interesarte: 👇")
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    send_main_menu(user_id)
    return jsonify({"status": "ok", "funnel": "prestamo_imss"})

# ---------------------------------------------------------------
# FUNCIÓN SEND_MESSAGE ACTUALIZADA PARA GUARDAR CONVERSACIONES
# ---------------------------------------------------------------
def send_message(to, text, funnel="menu", user_message=""):
    """Envía mensajes y guarda en base de datos"""
    try:
        # Validación de variables críticas
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
            # GUARDAR CONVERSACIÓN EN BASE DE DATOS
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
# ENDPOINTS PARA VER CONVERSACIONES
# ---------------------------------------------------------------
@app.route("/conversations", methods=["GET"])
def view_conversations():
    """Página web para ver todas las conversaciones"""
    try:
        limit = request.args.get('limit', 100, type=int)
        phone_filter = request.args.get('phone', '')
        
        if phone_filter:
            conversations = get_conversations(limit=limit, phone=phone_filter)
        else:
            conversations = get_conversations(limit=limit)
        
        stats = get_conversation_stats()
        
        html_template = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Conversaciones de Vicky Bot</title>
            <meta charset="utf-8">
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
                .header { background: #2c3e50; color: white; padding: 20px; border-radius: 10px; }
                .stats { background: white; padding: 15px; margin: 10px 0; border-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
                .conversation { background: white; padding: 15px; margin: 10px 0; border-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
                .user { color: #e74c3c; font-weight: bold; }
                .bot { color: #27ae60; font-weight: bold; }
                .phone { background: #3498db; color: white; padding: 2px 8px; border-radius: 3px; font-size: 12px; }
                .funnel { background: #9b59b6; color: white; padding: 2px 8px; border-radius: 3px; font-size: 12px; }
                .filters { background: white; padding: 15px; margin: 10px 0; border-radius: 5px; }
                .timestamp { color: #7f8c8d; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="header">
                <h1>💬 Conversaciones de Vicky Bot</h1>
                <p>Total: {{ stats.total_conversations }} conversaciones | Usuarios únicos: {{ stats.unique_users }} | Hoy: {{ stats.today_conversations }}</p>
            </div>
            
            <div class="filters">
                <form method="GET">
                    <input type="text" name="phone" placeholder="Filtrar por teléfono" value="{{ phone_filter }}">
                    <input type="number" name="limit" value="{{ limit }}" min="1" max="1000">
                    <button type="submit">Filtrar</button>
                    <a href="/conversations">Ver todas</a>
                </form>
            </div>
            
            <div class="stats">
                <h3>📊 Estadísticas por Funnel:</h3>
                {% for funnel, count in stats.funnel_stats.items() %}
                <span class="funnel">{{ funnel }}: {{ count }}</span>
                {% endfor %}
            </div>
            
            {% for conv in conversations %}
            <div class="conversation">
                <div>
                    <span class="phone">{{ conv[0] }}</span>
                    <span class="funnel">{{ conv[3] }}</span>
                    <span class="timestamp">{{ conv[5] }}</span>
                </div>
                <p><span class="user">👤 Usuario:</span> {{ conv[1] }}</p>
                <p><span class="bot">🤖 Vicky Bot:</span> {{ conv[2] }}</p>
                <p><span class="timestamp">Tipo: {{ conv[4] }}</span></p>
            </div>
            {% endfor %}
            
            {% if not conversations %}
            <div class="conversation">
                <p>No hay conversaciones para mostrar</p>
            </div>
            {% endif %}
        </body>
        </html>
        """
        
        return render_template_string(html_template, 
                                   conversations=conversations, 
                                   stats=stats,
                                   phone_filter=phone_filter,
                                   limit=limit)
        
    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route("/api/conversations", methods=["GET"])
def api_conversations():
    """API JSON para obtener conversaciones"""
    try:
        limit = request.args.get('limit', 50, type=int)
        phone = request.args.get('phone', '')
        
        conversations = get_conversations(limit=limit, phone=phone)
        
        result = []
        for conv in conversations:
            result.append({
                "phone": conv[0],
                "user_message": conv[1],
                "bot_response": conv[2],
                "funnel": conv[3],
                "message_type": conv[4],
                "timestamp": conv[5]
            })
        
        return jsonify({
            "status": "success",
            "count": len(result),
            "conversations": result
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
def api_stats():
    """API para estadísticas"""
    try:
        stats = get_conversation_stats()
        return jsonify({"status": "success", "stats": stats}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------------------------------------------------------------
# ENDPOINTS PRINCIPALES
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    logging.warning("❌ Verificación de webhook fallida.")
    return "Forbidden", 403

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
            send_message(phone_number, 
                "Por ahora solo puedo procesar mensajes de texto 📩\n\n"
                "Escribe *menú* para ver los servicios disponibles."
            , "system", user_message)
            return jsonify({"status": "ok"}), 200

        logging.info(f"📱 Mensaje de {phone_number}: '{user_message}'")

        # GPT SOLO BAJO COMANDO (en cualquier parte del bot)
        if is_gpt_command(user_message):
            prompt = user_message.split(":",1)[1].strip()
            if not prompt:
                send_message(phone_number, "Para consultar GPT, escribe por ejemplo:\ngpt: ¿Qué ventajas tiene el crédito IMSS?", "gpt", user_message)
                return jsonify({"status": "ok", "source": "gpt"})
            gpt_reply = ask_gpt(prompt)
            send_message(phone_number, gpt_reply, "gpt", user_message)
            return jsonify({"status": "ok", "source": "gpt"})

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

        # FLUJO IMSS: Si está en embudo, seguir el estado
        current_state = user_state.get(phone_number)
        if current_state and ("prestamo_imss" in current_state or "pregunta_" in current_state):
            return funnel_prestamo_imss(phone_number, user_message)

        # FLUJO EMPRESARIAL: Si está en embudo, seguir el estado
        if current_state and ("credito_empresarial" in current_state or "menu_tipo_credito" in current_state):
            return funnel_credito_empresarial(phone_number, user_message)

        # Opción 1: Iniciar embudo IMSS
        if option == "prestamo_imss":
            user_state[phone_number] = "menu_mostrar_beneficios"
            return funnel_prestamo_imss(phone_number, user_message)

        # Opción 5: Iniciar embudo EMPRESARIAL
        if option == "empresarial":
            user_state[phone_number] = "menu_tipo_credito"
            return funnel_credito_empresarial(phone_number, user_message)

        # Otros servicios - menú estándar
        if option == "seguro_auto":
            send_message(phone_number,
                "🚗 *Seguros de Auto Inbursa*\n\n"
                "Protege tu auto con las mejores coberturas:\n\n"
                "✅ Cobertura amplia contra todo riesgo\n"
                "✅ Asistencia vial las 24 horas\n"
                "✅ Responsabilidad civil\n"
                "✅ Robo total y parcial\n\n"
                "📞 Un asesor se comunicará contigo para cotizar tu seguro."
            , "seguro_auto", user_message)
            send_whatsapp_message(ADVISOR_NUMBER, f"🚗 NUEVO INTERESADO EN SEGURO DE AUTO\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})
        if option == "seguro_vida":
            send_message(phone_number,
                "🏥 *Seguros de Vida y Salud Inbursa*\n\n"
                "Protege a tu familia y tu salud:\n\n"
                "✅ Seguro de vida\n"
                "✅ Gastos médicos mayores\n"
                "✅ Hospitalización\n"
                "✅ Atención médica las 24 horas\n\n"
                "📞 Un asesor se comunicará contigo para explicarte las coberturas."
            , "seguro_vida", user_message)
            send_whatsapp_message(ADVISOR_NUMBER, f"🏥 NUEVO INTERESADO EN SEGURO VIDA/SALUD\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})
        if option == "vrim":
            send_message(phone_number,
                "💳 *Tarjetas Médicas VRIM*\n\n"
                "Accede a la mejor atención médica:\n\n"
                "✅ Consultas médicas ilimitadas\n"
                "✅ Especialistas y estudios de laboratorio\n"
                "✅ Medicamentos con descuento\n"
                "✅ Atención dental y oftalmológica\n\n"
                "📞 Un asesor se comunicará contigo para explicarte los beneficios."
            , "vrim", user_message)
            send_whatsapp_message(ADVISOR_NUMBER, f"💳 NUEVO INTERESADO EN TARJETAS VRIM\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})

        # Comando de menú
        if user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            send_main_menu(phone_number)
            return jsonify({"status": "ok", "funnel": "menu"})

        if user_message.lower() in ["hola", "hi", "hello", "buenas", "buenos días", "buenas tardes"]:
            send_main_menu(phone_number)
            return jsonify({"status": "ok", "funnel": "menu"})

        send_main_menu(phone_number)
        return jsonify({"status": "ok", "funnel": "menu"})

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

# ---------------------------------------------------------------
# ENDPOINT DE DIAGNÓSTICO TEMPORAL
# ---------------------------------------------------------------
@app.route("/debug-notification", methods=["GET", "POST"])
def debug_notification():
    """Endpoint temporal para probar notificaciones al asesor"""
    if request.method == "GET":
        return jsonify({
            "service": "Debug Notificaciones Vicky",
            "advisor_number": ADVISOR_NUMBER,
            "variables_configuradas": {
                "META_TOKEN": bool(META_TOKEN),
                "WABA_PHONE_ID": bool(WABA_PHONE_ID),
                "ADVISOR_NUMBER": ADVISOR_NUMBER
            }
        }), 200
    
    # POST: Probar envío de notificación real
    try:
        test_message = (
            f"🔔 PRUEBA: Notificación de Vicky Bot\n"
            f"📞 Para: {ADVISOR_NUMBER}\n"
            f"🕐 Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"✅ Si recibes esto, las notificaciones funcionan"
        )
        
        success = send_message(ADVISOR_NUMBER, test_message)
        
        return jsonify({
            "notification_test": {
                "sent_to": ADVISOR_NUMBER,
                "success": success,
                "timestamp": datetime.now().isoformat(),
                "message_preview": test_message[:100] + "..."
            }
        }), 200
        
    except Exception as e:
        logging.error(f"❌ Error en debug-notification: {e}")
        return jsonify({"error": str(e)}), 500

def send_campaign_message(phone_number, nombre):
    """
    Envía un mensaje tipo plantilla promocional usando la API de WhatsApp Business.
    La plantilla se llama "credito_imss_promocion_1" en idioma "es_MX".
    El nombre del prospecto se incluye como parámetro {{1}}.
    """
    try:
        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(phone_number),
            "type": "template",
            "template": {
                "name": "credito_imss_promocion_1",
                "language": {"code": "es_MX"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(nombre)}
                        ]
                    }
                ]
            }
        }
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logging.info(f"✅ Mensaje campaña enviado a {phone_number} ({nombre})")
    except Exception as e:
        logging.exception(f"❌ Error en send_campaign_message: {e}")

# ---------------------------------------------------------------
# INICIALIZACIÓN
# ---------------------------------------------------------------
if __name__ == "__main__":
    # Inicializar base de datos al iniciar
    init_database()
    
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot con sistema de conversaciones en puerto {port}")
    logging.info(f"📊 Ver conversaciones en: http://localhost:{port}/conversations")
    app.run(host="0.0.0.0", port=port)
