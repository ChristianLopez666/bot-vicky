
import logging
from flask import request
from registro_leads import registrar_lead
from notificar_asesor import notificar_asesor
from read_manual_imss import responder_con_manual

# Estructura base del webhook para campañas
def manejar_webhook():
    try:
        data = request.get_json()
        logging.info(f"📥 Mensaje recibido: {data}")

        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
        contacts = value.get("contacts", [])

        if not messages or not contacts:
            return "No message", 200

        message = messages[0]
        contact = contacts[0]
        wa_id = contact["wa_id"]
        user_message = message.get("text", {}).get("body", "").strip().lower()

        # 🎯 FLUJO: Detectar campaña
        if "imss" in user_message:
            notificar_asesor(f"🧓 Prospecto interesado en *Préstamos IMSS*. Número: {wa_id}")
            registrar_lead(wa_id, campaña="IMSS", producto="Préstamo Ley 73")
            return "IMSS flow started", 200

        elif "empresarial" in user_message or "negocio" in user_message:
            notificar_asesor(f"🏢 Prospecto interesado en *Crédito Empresarial*. Número: {wa_id}")
            registrar_lead(wa_id, campaña="Empresarial", producto="Crédito empresarial")
            return "Empresarial flow started", 200

        elif "hablar" in user_message or "asesor" in user_message:
            notificar_asesor(f"📞 El cliente {wa_id} solicitó contacto directo.")
            return "Contacto directo", 200

        # 🎯 FLUJO: Usar el manual si ya es prospecto IMSS calificado
        elif "como funciona" in user_message or "qué necesito" in user_message:
            respuesta = responder_con_manual(user_message)
            return respuesta, 200

        else:
            return "Mensaje no procesado", 200

    except Exception as e:
        logging.error(f"❌ Error en webhook: {e}")
        return "error", 500
