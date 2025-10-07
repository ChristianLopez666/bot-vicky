from flask import Flask, request, jsonify
import os
import re
import logging
from datetime import datetime
import requests

app = Flask(__name__)

# ---------------------- Config & Logging ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("vicky-fsm-imss")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP")  # E.164 (e.g., 5216682478005)

# ---------------------- Messages (copy) ----------------------
START_MSG = """👋 ¡Hola! Soy Vicky.
¿Buscas un *préstamo para pensionados IMSS (Ley 73)*? Responde *sí* o *no*."""

REPROMPT_YES_NO = """Para continuar, responde *sí* si buscas préstamo IMSS o *no* si te interesa otro producto."""

IMSS_BENEFITS = """🏦 *Préstamos a Pensionados IMSS (Ley 73)*
• Monto desde *$40,000* hasta *$650,000*
• ✅ Sin aval
• ✅ Sin revisión en Buró
• ✅ Descuento directo de tu pensión

💚 *Beneficios adicionales por cambiar tu nómina a Inbursa*
• Rendimiento referenciado a CETES
• Seguro de vida incluido
• Servicio médico 24/7 (orientación)
• Anticipo de nómina en emergencias

ℹ️ Para activar *estos beneficios adicionales* es necesario *cambiar tu nómina a Inbursa*.

Dime tu *pensión mensual aproximada* (solo números, ej. 7500)."""

ASK_PENSION = """Por favor, comparte tu *pensión mensual aproximada* (solo números, ej. 7500):"""

ASK_LOAN = """Perfecto 👍 ¿Qué *monto de préstamo* deseas solicitar? (entre $40,000 y $650,000)"""

ASK_NOMINA_TEMPLATE = """Excelente ✅ para un préstamo de *${:,.0f}* es requisito *cambiar tu nómina a Inbursa*.
¿Aceptas cambiar tu nómina? (*sí/no*)"""

CONFIRM_OK = """✅ ¡Listo! Christian te contactará para confirmar tu préstamo y tus *beneficios de Nómina Inbursa*."""
CONFIRM_NO = """Perfecto 👍 registré tu interés. Christian te contactará con opciones (*IMSS básico*)."""

ASK_OTHER_PRODUCT = """¿Qué producto te interesa? (por ejemplo: seguros, tarjetas médicas, financiamiento empresarial)"""
CONFIRM_OTHER_TEMPLATE = """Gracias. Avisaré a Christian para que te contacte sobre: *{}*."""

RESTART_MSG = """Ocurrió un detalle. Escribe *hola* para comenzar de nuevo."""

# ---------------------- FSM ----------------------
# States: start, ask_yes_no_reprompt, imss_benefits, ask_pension, ask_loan, ask_nomina, ask_other_product, notify_yes, notify_no, notify_other, done
SESSIONS = {}

def reset_session(user_id: str):
    SESSIONS[user_id] = {
        "state": "start",
        "data": {},
        "timestamp": datetime.utcnow(),
    }

def ensure_session(user_id: str):
    if user_id not in SESSIONS:
        reset_session(user_id)
    return SESSIONS[user_id]

# ---------------------- Helpers ----------------------
YES_WORDS = {"sí", "si", "sip", "claro", "ok", "vale", "acepto", "afirmativo", "por supuesto"}
NO_WORDS = {"no", "nop", "negativo", "no acepto", "para nada"}

def is_yes(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(w in t for w in YES_WORDS)

def is_no(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(w in t for w in NO_WORDS)

def extract_numbers(text: str):
    if not text:
        return []
    clean = text.replace(",", "").replace("$", "")
    return [float(n) for n in re.findall(r"(\d{2,7})(?:\.\d+)?", clean)]

def send_whatsapp(to: str, body: str) -> bool:
    try:
        url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "text": {"body": body},
        }
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        ok = 200 <= r.status_code < 300
        if not ok:
            logger.error("WhatsApp send error %s: %s", r.status_code, r.text)
        return ok
    except Exception as e:
        logger.error("WhatsApp send exception: %s", e)
        return False

def notify_advisor_imss(pension: float, loan: float, nomina_yes: bool):
    if not ADVISOR_WHATSAPP:
        logger.warning("ADVISOR_WHATSAPP no configurado; se omite notificación.")
        return
    if nomina_yes:
        body = (
            "🔥 NUEVO PROSPECTO IMSS\n"
            f"💰 Pensión: ${pension:,.0f}\n"
            f"💵 Préstamo: ${loan:,.0f}\n"
            "🏦 Nómina: SÍ"
        )
    else:
        body = (
            "📋 PROSPECTO IMSS BÁSICO\n"
            f"💰 Pensión: ${pension:,.0f}\n"
            f"💵 Préstamo: ${loan:,.0f}\n"
            "🏦 Nómina: NO"
        )
    send_whatsapp(ADVISOR_WHATSAPP, body)

def notify_advisor_other(topic: str):
    if not ADVISOR_WHATSAPP:
        logger.warning("ADVISOR_WHATSAPP no configurado; se omite notificación.")
        return
    body = f"📌 Interés en otro producto: {topic}"
    send_whatsapp(ADVISOR_WHATSAPP, body)

# ---------------------- Core Handler ----------------------
def handle_user_message(user_id: str, text: str) -> str:
    s = ensure_session(user_id)
    t = (text or "").strip()

    # Comandos globales
    if t.lower() in {"hola", "menu"}:
        reset_session(user_id)
        return START_MSG

    if s["state"] == "start":
        if is_yes(t):
            s["state"] = "imss_benefits"
            return IMSS_BENEFITS
        if is_no(t):
            s["state"] = "ask_other_product"
            return ASK_OTHER_PRODUCT
        s["state"] = "ask_yes_no_reprompt"
        return REPROMPT_YES_NO

    if s["state"] == "ask_yes_no_reprompt":
        if is_yes(t):
            s["state"] = "imss_benefits"
            return IMSS_BENEFITS
        if is_no(t):
            s["state"] = "ask_other_product"
            return ASK_OTHER_PRODUCT
        return REPROMPT_YES_NO

    if s["state"] == "imss_benefits":
        s["state"] = "ask_pension"
        return ASK_PENSION

    if s["state"] == "ask_pension":
        nums = extract_numbers(t)
        if len(nums) >= 2:
            pension, loan = sorted(nums)[:2]
            # clamp loan to range
            loan = max(40000, min(650000, loan))
            s["data"]["pension"] = pension
            s["data"]["loan"] = loan
            s["state"] = "ask_nomina"
            return ASK_NOMINA_TEMPLATE.format(loan)
        if len(nums) == 1:
            s["data"]["pension"] = nums[0]
            s["state"] = "ask_loan"
            return ASK_LOAN
        return ASK_PENSION

    if s["state"] == "ask_loan":
        nums = extract_numbers(t)
        if len(nums) == 0:
            return "Por favor escribe solo el monto numérico que deseas solicitar (ej. 120000):"
        loan = nums[0]
        if not (40000 <= loan <= 650000):
            return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"
        s["data"]["loan"] = loan
        s["state"] = "ask_nomina"
        return ASK_NOMINA_TEMPLATE.format(loan)

    if s["state"] == "ask_nomina":
        if is_yes(t):
            s["state"] = "done"
            notify_advisor_imss(s["data"].get("pension", 0), s["data"].get("loan", 0), True)
            return CONFIRM_OK
        if is_no(t):
            s["state"] = "done"
            notify_advisor_imss(s["data"].get("pension", 0), s["data"].get("loan", 0), False)
            return CONFIRM_NO
        return "Por favor responde *sí* o *no*."

    if s["state"] == "ask_other_product":
        topic = t if t else "Sin detalle"
        s["data"]["topic"] = topic
        s["state"] = "done"
        notify_advisor_other(topic)
        return CONFIRM_OTHER_TEMPLATE.format(topic)

    if s["state"] == "done":
        if t.lower() == "hola":
            reset_session(user_id)
            return START_MSG
        return "¿Necesitas algo más? Escribe *hola* para comenzar de nuevo."

    # Fallback de seguridad
    reset_session(user_id)
    return RESTART_MSG

# ---------------------- Flask Routes ----------------------
@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

@app.post("/webhook")
def webhook():
    try:
        data = request.get_json() or {}
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    user_id = msg.get("from")
                    text = (msg.get("text", {}) or {}).get("body", "")
                    if not user_id:
                        continue
                    reply = handle_user_message(user_id, text)
                    send_whatsapp(user_id, reply)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Webhook error: %s", e)
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
