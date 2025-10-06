# app.py — Vicky (menú corregido + opción 7 robusta)
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv

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

# =================
# Utilidades comunes
# =================
def wa_send_text(to: str, body: str) -> bool:
    """Envía mensaje de texto por WhatsApp Cloud API."""
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
            "text": {"body": body},
        }
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        logging.info(f"[WA] status={resp.status_code} resp={resp.text[:400]}")
        resp.raise_for_status()
        return True
    except Exception as e:
        logging.exception(f"❌ Error en wa_send_text(to={to}): {e}")
        return False

def clean_option(text: str) -> str:
    """Normaliza la opción del usuario: extrae primer dígito relevante (maneja 7, 7️⃣, espacios, etc.)."""
    if not text:
        return ""
    t = text.strip()
    # Quitar emoji de keycap (ej. 7️⃣) y quedarse con el primer dígito
    digits = "".join(ch for ch in t if ch.isdigit())
    return digits[:1] if digits else t.lower()

def send_main_menu(phone: str) -> None:
    """Menú principal sin comillas sueltas (string multilínea válido)."""
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

def gpt_fallback(prompt: str) -> str:
    """Respuesta breve de respaldo cuando no coincide ninguna opción."""
    try:
        if not OPENAI_API_KEY:
            return "Elige una opción del menú enviando *solo el número*. Escribe *MENÚ* para verlo de nuevo."
        out = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Responde breve, claro y en español neutro."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=120,
            temperature=0.4,
        )
        return out["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.exception(f"❌ Error en gpt_fallback: {e}")
        return "Ahora mismo no puedo consultar al asistente. Escribe *MENÚ* para ver opciones."

# ================
# Endpoints básicos
# ================
@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "service": "vicky"}), 200

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
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    logging.warning("🔒 Intento de verificación con token inválido.")
    return "Verification token mismatch", 403

# Recepción de mensajes (POST)
@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json(force=True, silent=True) or {}
        logging.info(f"📩 Webhook body: {json.dumps(data)[:1500]}")

        entry = (data.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value") or {}

        # Ignorar status updates
        if "messages" not in value:
            return jsonify({"ignored": True}), 200

        message = value["messages"][0]
        contacts = value.get("contacts") or [{}]
        contact = contacts[0] if contacts else {}
        profile_name = contact.get("profile", {}).get("name", "Cliente")

        # Determinar número de usuario
        phone_number = message.get("from") or contact.get("wa_id")

        # Solo procesamos texto, para lo demás reenviamos menú
        if message.get("type") != "text":
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        user_text = (message.get("text", {}) or {}).get("body", "").strip()

        # Palabras clave para menú
        if user_text.lower() in {"menu", "menú", "hola", "inicio", "empezar"}:
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        # Normalizar opción
        option = clean_option(user_text)

        # -----------------------------
        # Opción 7: Contactar Christian
        # -----------------------------
        if option == "7" or "contactar con christian" in user_text.lower():
            # Confirmación al cliente
            wa_send_text(phone_number, "✅ Gracias. Un asesor se comunicará contigo en breve.")
            # Notificación al asesor
            notify = (
                "📢 *Nuevo intento de contacto desde Vicky*\n\n"
                f"👤 Nombre: {profile_name}\n"
                f"📱 Número: {phone_number}\n"
                "🧭 Opción: 7️⃣ Contactar con Christian"
            )
            wa_send_text(ADVISOR_NUMBER, notify)
            logging.info("📨 Notificación enviada al asesor (opción 7).")
            return jsonify({"status": "ok"}), 200

        # Opciones 1-6 (placeholders mínimos)
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

        # Fallback con GPT o mensaje guía
        wa_send_text(phone_number, gpt_fallback(user_text))
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en /webhook POST: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
