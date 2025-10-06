# app.py — Vicky Campañas (menú fijo + opción 7 robusta)
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

# =========================
# Carga de variables y setup
# =========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__)

# ==============
# Utilidades WABA
# ==============
def send_message(to, text):
    """Envia un mensaje de texto por la API de WhatsApp Cloud."""
    try:
        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(to),
            "type": "text",
            "text": {"body": text},
        }
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        logging.info(f"WA send_message status={resp.status_code} body={resp.text}")
        resp.raise_for_status()
        return True
    except Exception as e:
        logging.exception(f"Error enviando mensaje a {to}: {e}")
        return False

def clean_option(text: str) -> str:
    """Normaliza la opción: elimina espacios, emojis de tecla y deja solo dígitos relevantes."""
    if not text:
        return ""
    t = text.strip()
    # eliminar variantes de 7️⃣ (u otros) y dejar solo dígitos + separadores básicos
    # Si hay varios dígitos, tomamos el primero (casos como "7) ...")
    digits = "".join(ch for ch in t if ch.isdigit())
    return digits[:1] if digits else t

def send_main_menu(phone):
    """Envía el menú principal (sin comillas sueltas)."""
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
    send_message(phone, menu)

def gpt_reply(prompt: str) -> str:
    """Respuesta breve con OpenAI si no se reconoce la opción. Compatible con openai==0.28.1"""
    try:
        if not OPENAI_API_KEY:
            return "Puedo ayudarte con cualquiera de nuestros servicios. Elige una opción del menú enviando solo el número."

        completion = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Responde breve, claro y en español neutro."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=150,
            temperature=0.4,
        )
        return completion["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.exception(f"Error en OpenAI: {e}")
        return "Ahora mismo no puedo consultar al asistente. Elige una opción del menú con el número."

# ==================
# Rutas de la aplicación
# ==================
@app.route("/", methods=["GET"])
def root_ok():
    return jsonify({"status": "ok", "service": "vicky-campaigns"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# Verificación de webhook (GET)
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("Webhook verificado correctamente.")
        return challenge, 200
    else:
        logging.warning("Intento de verificación con token inválido.")
        return "Verification token mismatch", 403

# Recepción de mensajes (POST)
@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json(force=True, silent=True) or {}
        logging.info(f"📩 Entrada webhook: {json.dumps(data)[:1500]}")

        entry = data.get("entry", [{}])[0]
        change = entry.get("changes", [{}])[0]
        value = change.get("value", {})

        # Validar mensajes (no "statuses")
        if "messages" not in value:
            return jsonify({"ignored": True}), 200

        message = value["messages"][0]
        contacts = value.get("contacts", [{}])
        contact = contacts[0] if contacts else {}
        profile_name = contact.get("profile", {}).get("name", "Cliente")
        phone_number = message.get("from") or contact.get("wa_id")

        msg_type = message.get("type")
        if msg_type != "text":
            # si no es texto, solo mostrar menú
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        user_text = (message.get("text", {}) or {}).get("body", "").strip()

        # Si el usuario escribe "menu" o similar, reenvía el menú
        if user_text.lower() in {"menu", "menú", "inicio", "hola"}:
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        # Normalizar opción y decidir flujo
        option = clean_option(user_text)

        # --- Opción 7: Contactar con Christian ---
        if option == "7" or "contactar con christian" in user_text.lower():
            try:
                # Confirmación al cliente
                msg_user = (
                    "✅ Gracias por tu interés.\n"
                    "📩 Un asesor se comunicará contigo en breve.\n"
                    "Mientras tanto, si necesitas algo más, dime *MENÚ*."
                )
                send_message(phone_number, msg_user)

                # Notificación interna al asesor
                notify_text = (
                    "📢 *Nuevo intento de contacto desde Vicky*\n\n"
                    f"👤 Nombre: {profile_name}\n"
                    f"📱 Número: {phone_number}\n"
                    "🧭 Opción: 7️⃣ Contactar con Christian"
                )
                send_message(ADVISOR_NUMBER, notify_text)
                logging.info("📨 Notificación enviada al asesor correctamente.")
            except Exception as e:
                logging.exception(f"❌ Error en notificación al asesor: {e}")
                # Aviso al usuario de que algo falló, pero sin detallar
                send_message(phone_number, "Hubo un detalle al notificar al asesor. Intentaré nuevamente.")
            return jsonify({"status": "ok"}), 200

        # Resto de opciones simples como placeholder (1-6)
        if option in {"1", "2", "3", "4", "5", "6"}:
            respuestas = {
                "1": "🚗 *Seguros de Auto*: Cotizamos tu póliza con beneficios preferentes. ¿Deseas continuar?",
                "2": "🧑‍⚕️ *Vida y Salud*: Te presento opciones de protección familiar. ¿Deseas continuar?",
                "3": "🩺 *Tarjetas Médicas VRIM*: Atención privada con costo accesible. ¿Deseas continuar?",
                "4": "💳 *Préstamos IMSS Ley 73*: Mínimo $40,000. Requisitos según manual oficial. ¿Deseas continuar?",
                "5": "🏢 *Financiamiento Empresarial*: Indícame giro y monto requerido. ¿Deseas continuar?",
                "6": "💼 *Nómina Empresarial*: Mejora tu banca de nómina con beneficios. ¿Deseas continuar?",
            }
            send_message(phone_number, respuestas.get(option, "¿Deseas continuar?"))
            return jsonify({"status": "ok"}), 200

        # Si no coincide con una opción 1-7, usar GPT como fallback breve
        reply = gpt_reply(user_text)
        send_message(phone_number, reply)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en webhook: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
