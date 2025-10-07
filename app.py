from flask import Flask, request, jsonify
import requests
import os
import re
import logging
from datetime import datetime

app = Flask(__name__)

# ---------------------- Logging ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("vicky-imss")

# ---------------------- Constants / Messages ----------------------
IMSS_GREETING_AND_BENEFITS = (
    "👋 ¡Hola! Soy *Vicky*.\n
"
    "🏦 *Préstamos a Pensionados IMSS (Ley 73)*
"
    "• Monto desde *$40,000* hasta *$650,000*
"
    "• ✅ Sin aval
"
    "• ✅ Sin revisión en Buró
"
    "• ✅ Descuento directo de tu pensión

"
    "💚 *Beneficios adicionales por cambiar tu nómina a Inbursa*
"
    "• Rendimiento referenciado a CETES
"
    "• Seguro de vida incluido
"
    "• Servicio médico 24/7 (orientación)
"
    "• Anticipo de nómina en emergencias

"
    "ℹ️ Para activar *estos beneficios adicionales* es necesario *cambiar tu nómina a Inbursa*.

"
    "Para comenzar, dime tu *pensión mensual aproximada* (ej. 7500)."
)

ASK_PENSION = "Por favor, comparte tu *pensión mensual aproximada* (solo números, ej. 7500):"
ASK_LOAN = "Perfecto 👍 ¿Qué *monto de préstamo* deseas solicitar? (entre $40,000 y $650,000)"
ASK_NOMINA = (
    "Excelente ✅ para un préstamo de *${:,.0f}* es requisito *cambiar tu nómina a Inbursa*.
"
    "¿Aceptas cambiar tu nómina? (sí/no)"
)
CONFIRM_OK = (
    "✅ ¡Listo! Christian te contactará en breve para confirmar tu préstamo y tus *beneficios de Nómina Inbursa*."
)
CONFIRM_NO = (
    "Perfecto 👍 registré tu interés. Christian te contactará con opciones (IMSS básico)."
)
RESTART_MSG = "Ocurrió un detalle. Escribe *hola* para comenzar de nuevo."

# ---------------------- Bot Core ----------------------
class VickyIMSSBot:
    def __init__(self):
        # Sesiones en memoria (suficiente para Fase 1)
        self.sessions = {}
        self.wh_token = os.getenv("WHATSAPP_TOKEN")
        self.wh_phone_id = os.getenv("WHATSAPP_PHONE_ID")
        self.advisor_phone = os.getenv("ADVISOR_WHATSAPP")

    # -------- Parsing helpers --------
    def extract_amounts(self, text: str):
        if not text:
            return []
        clean = text.replace(",", "").replace("$", "")
        return [float(n) for n in re.findall(r"(\d{2,7})(?:\.\d+)?", clean)]

    def extract_amount(self, text: str):
        nums = self.extract_amounts(text)
        return nums[0] if nums else None

    def intent_yes_no(self, text: str):
        t = (text or "").lower()
        yes = ["sí", "si", "claro", "ok", "vale", "acepto", "por supuesto", "afirmativo"]
        no = ["no", "nop", "negativo", "para nada", "no acepto"]
        if any(k in t for k in yes):
            return "yes"
        if any(k in t for k in no):
            return "no"
        return "unknown"

    # -------- Messaging --------
    def send_wh_message(self, to: str, body: str):
        try:
            url = f"https://graph.facebook.com/v17.0/{self.wh_phone_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.wh_token}",
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
                logger.error("WhatsApp error %s: %s", r.status_code, r.text)
            return ok
        except Exception as e:
            logger.error("WhatsApp exception: %s", e)
            return False

    def notify_advisor(self, data, accepted_nomina: bool):
        if not self.advisor_phone:
            logger.warning("ADVISOR_WHATSAPP no configurado; se omite notificación.")
            return
        if accepted_nomina:
            body = (
                "🔥 NUEVO PROSPECTO IMSS\n"
                f"💰 Pensión: ${data.get('pension', 0):,.0f}\n"
                f"💵 Préstamo: ${data.get('loan', 0):,.0f}\n"
                "🏦 Nómina: SÍ"
            )
        else:
            body = (
                "📋 PROSPECTO IMSS BÁSICO\n"
                f"💰 Pensión: ${data.get('pension', 0):,.0f}\n"
                f"💵 Préstamo: ${data.get('loan', 0):,.0f}\n"
                "🏦 Nómina: NO"
            )
        self.send_wh_message(self.advisor_phone, body)

    # -------- Flow engine --------
    def ensure_session(self, user_id: str):
        if user_id not in self.sessions:
            self.sessions[user_id] = {
                "state": "benefits",  # siempre iniciamos mostrando beneficios
                "data": {},
                "timestamp": datetime.utcnow(),
            }
        return self.sessions[user_id]

    def handle_message(self, user_id: str, text: str):
        s = self.ensure_session(user_id)
        t = (text or "").strip()

        # HARD OVERRIDE: mientras estemos en estos estados, nada externo interfiere
        if s["state"] == "benefits":
            s["state"] = "ask_pension"
            return IMSS_GREETING_AND_BENEFITS

        # Si el usuario manda 2 números en la primera respuesta (pensión + monto)
        nums = self.extract_amounts(t)
        if s["state"] in {"ask_pension", "benefits"} and len(nums) >= 2:
            pension, loan = sorted(nums)[:2]
            loan = max(40000, min(650000, loan))
            s["data"]["pension"] = pension
            s["data"]["loan"] = loan
            s["state"] = "ask_nomina"
            return ASK_NOMINA.format(loan)

        if s["state"] == "ask_pension":
            pension = self.extract_amount(t)
            if pension is None:
                return ASK_PENSION
            s["data"]["pension"] = pension
            s["state"] = "ask_loan"
            return ASK_LOAN

        if s["state"] == "ask_loan":
            loan = self.extract_amount(t)
            if loan is None:
                return "Por favor escribe solo el monto numérico que deseas solicitar (ej. 120000):"
            if not (40000 <= loan <= 650000):
                return "El monto debe estar entre $40,000 y $650,000. Ingresa un monto válido:"
            s["data"]["loan"] = loan
            s["state"] = "ask_nomina"
            return ASK_NOMINA.format(loan)

        if s["state"] == "ask_nomina":
            ans = self.intent_yes_no(t)
            if ans == "yes":
                s["state"] = "completed"
                self.notify_advisor(s["data"], True)
                return CONFIRM_OK
            if ans == "no":
                s["state"] = "completed"
                self.notify_advisor(s["data"], False)
                return CONFIRM_NO
            return "Por favor responde *sí* o *no*."

        # Estado desconocido → reinicio controlado
        self.sessions[user_id] = {"state": "benefits", "data": {}, "timestamp": datetime.utcnow()}
        return RESTART_MSG

bot = VickyIMSSBot()

# ---------------------- Flask Routes ----------------------
@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv("VERIFY_TOKEN")
    if mode == "subscribe" and token == verify_token:
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
                    reply = bot.handle_message(user_id, text)
                    bot.send_wh_message(user_id, reply)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Webhook error: %s", e)
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
