import os
import json
import logging
import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

# Cargar variables de entorno
load_dotenv()
META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
openai.api_key = os.getenv("OPENAI_API_KEY")

# Configuración de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
user_state = {}
user_data = {}

def send_message(to, text):
    try:
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
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in (200, 201):
            logging.warning(f"⚠️ Error al enviar mensaje: {response.text}")
    except Exception as e:
        logging.exception(f"❌ Error en send_message: {e}")

def extract_number(text):
    import re
    match = re.search(r"\d+", text.replace(",", "").replace(".", ""))
    return int(match.group()) if match else None

def send_main_menu(phone):
    menu = (
        "📋 *Otros servicios disponibles:*"
"
        "1️⃣ Seguros de Auto
"
        "2️⃣ Seguros de Vida y Salud
"
        "3️⃣ Tarjetas Médicas VRIM
"
        "4️⃣ Financiamiento Empresarial
"
        "5️⃣ Préstamos Personales
"
        "7️⃣ Contactar con Christian

"
        "Escribe el número del servicio que te interese 👇"
    )
    send_message(phone, menu)

def consultar_gpt_respuesta(texto_usuario):
    try:
        respuesta = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Responde como una asesora profesional llamada Vicky, experta en préstamos y seguros. "
                        "Debes sonar clara, amable, empática y directa. Responde en máximo 3 líneas. "
                        "Estás atendiendo a un prospecto desde una campaña de redes sociales."
                    )
                },
                {"role": "user", "content": texto_usuario}
            ],
            temperature=0.7,
            max_tokens=200
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"⚠️ Error al consultar GPT: {e}")
        return "Estoy aquí para ayudarte. ¿Podrías darme más detalles sobre lo que necesitas?"

def handle_imss_flow(phone_number, user_message):
    msg = user_message.lower()
    if any(x in msg for x in ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73"]):
        send_message(phone_number, "👋 ¿Eres pensionado del IMSS bajo la Ley 73? (Responde *sí* o *no*)")
        user_state[phone_number] = "esperando_respuesta_imss"
        return True

    if user_state.get(phone_number) == "esperando_respuesta_imss":
        if "no" in msg:
            send_message(phone_number, "Este préstamo es exclusivo para pensionados IMSS Ley 73. Te muestro otras opciones 👇")
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
        elif "sí" in msg or "si" in msg:
            send_message(phone_number, "¿Cuánto recibes al mes por pensión?")
            user_state[phone_number] = "esperando_monto_pension"
        else:
            send_message(phone_number, "Por favor responde *sí* o *no*.")
        return True

    if user_state.get(phone_number) == "esperando_monto_pension":
        monto = extract_number(msg)
        if monto:
            user_data[phone_number] = {"pension_mensual": monto}
            send_message(phone_number, "¿Qué monto deseas solicitar? (mínimo $40,000)")
            user_state[phone_number] = "esperando_monto_solicitado"
        else:
            send_message(phone_number, "Por favor ingresa una cantidad válida. Ejemplo: 8500")
        return True

    if user_state.get(phone_number) == "esperando_monto_solicitado":
        monto = extract_number(msg)
        if monto:
            if monto < 40000:
                send_message(phone_number, "El mínimo para aplicar es de $40,000. Te muestro otras opciones 👇")
                send_main_menu(phone_number)
                user_state.pop(phone_number, None)
            else:
                user_data[phone_number]["monto_solicitado"] = monto
                send_message(phone_number, "¿Tienes tu pensión en Inbursa o estás dispuesto a cambiarla?")
                user_state[phone_number] = "esperando_respuesta_nomina"
        else:
            send_message(phone_number, "Por favor indica el monto deseado. Ejemplo: 65000")
        return True

    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        if "sí" in msg or "si" in msg or "dispuesto" in msg:
            send_message(phone_number, "🌟 Cambiar tu nómina a Inbursa te da beneficios exclusivos...")
            send_message(phone_number, "💰 Rendimientos de Cetes, préstamos, devolución de intereses, seguro y más.\n👉 Un asesor te contactará pronto.")
            data = user_data.get(phone_number, {})
            mensaje = (
                f"📢 *Nuevo prospecto IMSS Ley 73*\n"
                f"📞 Número: {phone_number}\n"
                f"💰 Pensión mensual: ${data.get('pension_mensual', 'N/D')}\n"
                f"💵 Monto solicitado: ${data.get('monto_solicitado', 'N/D')}\n"
                f"🏦 Acepta cambiar nómina a Inbursa ✅"
            )
            send_message(ADVISOR_NUMBER, mensaje)
            user_state.pop(phone_number, None)
        else:
            send_message(phone_number, "Sin cambiar la nómina no es posible otorgar este préstamo. Te muestro otras opciones 👇")
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
        return True

    return False

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        value = data.get("entry", [])[0].get("changes", [])[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        phone_number = message.get("from")
        user_message = message.get("text", {}).get("body", "").strip()

        # --- Opción 7: contactar con Christian ---
        if user_message.strip() == "7":
            send_message(phone_number, "📞 ¡Listo! He notificado a Christian para que te contacte y te dé seguimiento.")
            mensaje_asesor = f"📢 *Nuevo contacto desde campañas*:\n📱 Número: {phone_number}\n💬 Solicitó contacto directo (opción 7)."
            send_message(ADVISOR_NUMBER, mensaje_asesor)
            return jsonify({"status": "ok"}), 200

        if handle_imss_flow(phone_number, user_message):
            return jsonify({"status": "ok"}), 200

        respuesta = consultar_gpt_respuesta(user_message)
        send_message(phone_number, respuesta)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en webhook: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
