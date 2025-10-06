# app.py — Vicky Redes (versión DIAGNÓSTICO)
# Objetivo: no cambia la lógica; solo agrega logs detallados para aislar por qué la opción 7 no notifica.
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
def _wa_endpoint():
    return f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"

def wa_send_text(to, text):
    """Envía texto por WhatsApp Cloud API. Retorna (ok, status, body) y deja logs detallados."""
    url = _wa_endpoint()
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
    try:
        logging.info(f"[WA][REQ] to={to} payload={json.dumps(payload, ensure_ascii=False)[:400]}")
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        body = resp.text
        logging.info(f"[WA][RES] to={to} status={resp.status_code} body={body[:600]}")
        resp.raise_for_status()
        return True, resp.status_code, body
    except Exception as e:
        logging.exception(f"❌ [WA][ERR] to={to} exception={e}")
        try:
            return False, resp.status_code, resp.text
        except Exception:
            return False, None, str(e)

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
    logging.info(f"[VERIFY] mode={mode} token_match={token==VERIFY_TOKEN}")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification token mismatch", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json(force=True, silent=True) or {}
        logging.info(f"📩 Webhook body: {json.dumps(data, ensure_ascii=False)[:1500]}")

        entry = (data.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value") or {}

        if "messages" not in value:
            logging.info("ℹ️ Evento ignorado (no 'messages' en payload)")
            return jsonify({"ignored": True}), 200

        message = value["messages"][0]
        contact = (value.get("contacts") or [{}])[0]
        profile_name = contact.get("profile", {}).get("name", "Cliente")
        phone_number = message.get("from") or contact.get("wa_id")
        user_text = (message.get("text", {}) or {}).get("body", "").strip()
        msg_type = message.get("type")

        logging.info(f"[IN] from={phone_number} name={profile_name!r} type={msg_type} text={user_text!r}")

        if msg_type != "text":
            logging.info("[FLOW] Mensaje no-text → envío menú")
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        if user_text.lower() in {"menu", "menú", "hola", "inicio"}:
            logging.info("[FLOW] Solicitud de menú explícita")
            send_main_menu(phone_number)
            return jsonify({"status": "ok"}), 200

        option = clean_option(user_text)
        logging.info(f"[FLOW] option_normalized={option!r} raw={user_text!r}")

        # ----------- Opción 7 (misma lógica, más logs) -----------
        if option == "7" or ("contactar con christian" in user_text.lower()):
            logging.info(f"[FLOW] Entrando a opción 7 → advisor={ADVISOR_NUMBER}")
            user_msg = "✅ Gracias por tu interés. Un asesor se comunicará contigo en breve."
            ok_user, sc_user, body_user = wa_send_text(phone_number, user_msg)
            logging.info(f"[FLOW] user_ack ok={ok_user} status={sc_user} resp={body_user[:300] if body_user else body_user}")

            notify = (
                "📢 *Nuevo intento de contacto desde Vicky*\n\n"
                f"👤 Nombre: {profile_name}\n"
                f"📱 Número: {phone_number}\n"
                "🧭 Opción: 7️⃣ Contactar con Christian"
            )
            ok_adv, sc_adv, body_adv = wa_send_text(ADVISOR_NUMBER, notify)
            logging.info(f"[FLOW] advisor_notify ok={ok_adv} status={sc_adv} advisor={ADVISOR_NUMBER} resp={body_adv[:300] if body_adv else body_adv}")
            if ok_adv:
                logging.info("📨 Notificación enviada al asesor (opción 7).")
            else:
                logging.warning("⚠️ Falló el envío al asesor. Revisa status y resp en la línea anterior.")
            return jsonify({"status": "ok"}), 200

        if option in {"1", "2", "3", "4", "5", "6"}:
            logging.info(f"[FLOW] Opción {option} (placeholder)")
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

        logging.info("[FLOW] Fallback con GPT")
        wa_send_text(phone_number, gpt_fallback(user_text))
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en webhook POST: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

