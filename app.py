# app.py — versión final (condición opción 7 corregida)
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# ---------------------- UTILIDADES ----------------------
def wa_send_text(to, text):
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
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        logging.info(f"[WA] status={resp.status_code} resp={resp.text[:400]}")
        resp.raise_for_status()
        return True
    except Exception as e:
        logging.exception(f"❌ Error enviando mensaje a {to}: {e}")
        return False

def clean_option(text):
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:1] if digits else text.lower()

def send_main_menu(phone):
    menu = (
        "📋 *Otros servicios disponibles:*\n"
        "1️⃣ Seguros de Auto\n"
        "2️⃣ Seguros de Vida y Salud\n"
        "3️⃣ Tarjetas Médicas VRIM\n"
        "4️⃣ Préstamos para Pensionados IMSS\n"
        "5️⃣ Financiamiento Empresarial\n"
        "6️⃣ Nómina Empresarial\n"
        "7️⃣ Contactar con Christian\n"
        "\n"
        "Escribe el número del servicio que te interese 👇"
    )
    wa_send_text(phone, menu)

def gpt_fallback(prompt):
    try:
        if not OPENAI_API_KEY:
            return "Escribe el número de la opción que deseas o 'MENÚ' para ver las opciones disponibles."
        completion = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Responde breve, claro y en español neutro."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=120,
            temperature=0.4
        )
        return completion["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.exception(f"❌ Error en GPT: {e}")
        return "Por ahora no puedo responder eso. Escribe 'MENÚ' para ver las opciones."

# ---------------------- ENDPOINTS ----------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification token mismatch", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json(force=True, silent=True) or {}
        logging.info(f"📩 Webhook body: {json.dumps(data)[:1500]}")

        entry = (data.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value") or {}

        if "messages" not in value:
            return jsonify({"ignored": True}), 200

        message = value["messages"][0]
        contact = (value.get("contacts") or [{}])[0]
        profile_name = contact.get("profile", {}).get("name", "Cliente")
        phone_number = message.get("from") or contact.get("wa_id")
        user_text = (message.get("text", {}) or {}).get("body", "").strip()

        if message.get("type") != "text":
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        if user_text.lower() in {"menu", "menú", "hola", "inicio"}:
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        option = clean_option(user_text)

        # ----------- Opción 7 corregida -----------
        if option == "7" or ("contactar con christian" in user_text.lower()):
            wa_send_text(phone_number, "✅ Gracias por tu interés. Un asesor se comunicará contigo en breve.")
            notify = (
                "📢 *Nuevo intento de contacto desde Vicky*\n\n"
                f"👤 Nombre: {profile_name}\n"
                f"📱 Número: {phone_number}\n"
                "🧭 Opción: 7️⃣ Contactar con Christian"
            )
            wa_send_text(ADVISOR_NUMBER, notify)
            logging.info("📨 Notificación enviada al asesor (opción 7).")
            return jsonify({"status": "ok"}), 200

        if option in {"1", "2", "3", "4", "5", "6"}:
            respuestas = {
                "1": "🚗 *Seguros de Auto*: cotizamos tu póliza con beneficios preferentes. ¿Deseas continuar?",
                "2": "🧑‍⚕️ *Vida y Salud*: opciones de protección familiar. ¿Deseas continuar?",
                "3": "🩺 *VRIM Médica*: atención privada accesible. ¿Deseas continuar?",
                "4": "💳 *Préstamos IMSS Ley 73*: mínimo $40,000. ¿Deseas continuar?",
                "5": "🏢 *Financiamiento Empresarial*: indica giro y monto. ¿Deseas continuar?",
                "6": "💼 *Nómina Empresarial*: beneficios y migración simple. ¿Deseas continuar?",
            }
            wa_send_text(phone_number, respuestas[option])
            return jsonify({"status": "ok"}), 200

        wa_send_text(phone_number, gpt_fallback(user_text))
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en webhook POST: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
