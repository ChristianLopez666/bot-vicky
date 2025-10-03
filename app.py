import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import pytz

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Zona horaria
tz = pytz.timezone('America/Mazatlan')

# Variables Meta API
META_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER")

# Variables Google Sheets
GOOGLE_CREDENTIALS_JSON = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
SHEET_ID = os.getenv("CAMPAIGN_SHEET_ID")
SHEET_NAME = os.getenv("CAMPAIGN_SHEET_NAME")

# --- Funciones auxiliares ---
def send_whatsapp_message(to_number, message):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=data)
    logging.info(f"Mensaje enviado a {to_number}: {message}")
    return response.json()

def notify_advisor(message):
    return send_whatsapp_message(ADVISOR_NUMBER, f"📣 *Nuevo prospecto desde campañas:*\n\n{message}")

# --- Webhook META WhatsApp ---
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("VERIFY_TOKEN"):
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    logging.info(f"📥 Mensaje recibido: {json.dumps(data)}")

    try:
        entry = data["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        if "messages" in value:
            message = value["messages"][0]
            user_number = message["from"]
            user_text = message["text"]["body"].strip().lower()

            # Entrada a campañas
            if "imss" in user_text:
                send_whatsapp_message(user_number, "👋 Hola, ¿Eres pensionado o jubilado del IMSS bajo la Ley 73?")
                # Se espera respuesta para continuar el embudo
            elif "crédito" in user_text or "empresarial" in user_text:
                send_whatsapp_message(user_number, "👋 Hola, dime por favor:\n1️⃣ ¿Qué tipo de crédito necesitas?\n2️⃣ ¿Eres empresario?\n3️⃣ ¿A qué se dedica tu empresa?\n4️⃣ ¿Qué monto necesitas?")
                # Se espera respuesta
            elif "hablar contigo" in user_text or "asesor" in user_text:
                notify_advisor(f"El cliente *{user_number}* solicita hablar contigo. 📞")
                send_whatsapp_message(user_number, "📨 Tu solicitud ha sido enviada. En breve te contactaremos.")

            # Filtrado directo (si responde no a Ley 73)
            elif "no" in user_text and "ley 73" in user_text:
                send_whatsapp_message(user_number, "🚫 Lo siento, este crédito es solo para pensionados bajo la Ley 73.\nTe presento otros servicios que ofrecemos:")
                send_whatsapp_message(user_number, "📋 *Menú principal:*\n1️⃣ Pensiones\n2️⃣ Seguros de auto\n3️⃣ Seguros de vida y salud\n4️⃣ Tarjetas VRIM\n5️⃣ Préstamos IMSS\n6️⃣ Financiamiento empresarial\n7️⃣ Hablar con Christian")

            # Si dice que sí es Ley 73
            elif "sí" in user_text or "soy pensionado" in user_text:
                send_whatsapp_message(user_number, "✅ Excelente. ¿Cuál es el monto que te interesa solicitar?")
                # Esperamos monto y notificamos si >= $40,000
            elif "$" in user_text or user_text.replace(",", "").isdigit():
                monto = int(''.join(filter(str.isdigit, user_text)))
                if monto >= 40000:
                    send_whatsapp_message(user_number, "Perfecto, puedes acceder a beneficios adicionales si cambias tu nómina a Inbursa. Este servicio *no tiene costo*.")
                    notify_advisor(f"✅ Prospecto IMSS Ley 73 con monto ${monto} desde WhatsApp: {user_number}")
                else:
                    send_whatsapp_message(user_number, "El monto mínimo para este tipo de préstamo es de $40,000. ¿Te interesa revisar otra opción?")

    except Exception as e:
        logging.error(f"❌ Error en webhook: {e}")
        return "error", 500

    return "ok", 200

# --- Endpoint de salud ---
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"}), 200

