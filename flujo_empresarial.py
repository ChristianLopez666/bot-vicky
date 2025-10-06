
from registro_leads import registrar_lead
from notificar_asesor import notificar_asesor

MENU_PRINCIPAL = (
    "📋 *Menú de servicios COHFIS:*
"
    "1️⃣ Asesoría en pensiones
"
    "2️⃣ Seguros de auto (Amplia PLUS, Amplia y Limitada)
"
    "3️⃣ Seguros de vida y salud
"
    "4️⃣ Tarjetas médicas VRIM
"
    "5️⃣ Préstamos IMSS Ley 73
"
    "6️⃣ Financiamiento empresarial
"
    "7️⃣ Hablar con Christian"
)

RESPUESTA_NO_EMPRESARIO = (
    "Gracias por tu interés 🙌. Este servicio está enfocado en empresarios o negocios formales.

"
    "Pero también ofrecemos otras opciones que podrían interesarte:

"
    f"{MENU_PRINCIPAL}"
)

# Flujo empresarial paso a paso
def iniciar_flujo_empresarial():
    return [
        "👋 Hola, ¡gracias por tu interés en nuestros *créditos empresariales*!",
        "Para darte un mejor servicio, dime por favor:

"
        "1️⃣ ¿Qué tipo de crédito necesitas?
"
        "2️⃣ ¿Eres empresario o tienes un negocio?
"
        "3️⃣ ¿A qué se dedica tu empresa?
"
        "4️⃣ ¿Qué monto necesitas aproximadamente?"
    ]

# Procesar respuestas capturadas
def procesar_respuestas_empresarial(wa_id: str, respuestas: dict) -> list:
    tipo = respuestas.get("tipo_credito", "")
    empresario = respuestas.get("es_empresario", "").strip().lower()
    giro = respuestas.get("giro", "")
    monto = respuestas.get("monto", "")

    if "no" in empresario or "no soy" in empresario:
        registrar_lead(whatsapp=wa_id, campaña="Empresarial", producto="Crédito Empresarial", monto=monto, solicita_contacto="No (no empresario)")
        return [RESPUESTA_NO_EMPRESARIO]

    # Si sí es empresario → solicitar datos de contacto
    registrar_lead(whatsapp=wa_id, campaña="Empresarial", producto=tipo, monto=monto, solicita_contacto="Pendiente")

    notificar_asesor(
        f"🏢 *Nuevo prospecto empresarial:*
"
        f"📱 WA: {wa_id}
"
        f"📌 Giro: {giro}
💵 Monto: {monto}
📄 Tipo: {tipo}
➡️ Esperando datos de contacto."
    )

    return [
        "✅ Gracias por la información.",
        "📞 Un asesor se comunicará contigo muy pronto.",
        "Para agendar correctamente la llamada, por favor proporcióname:
"
        "1️⃣ Tu *nombre completo*
"
        "2️⃣ Número telefónico (si es distinto al de este chat)
"
        "3️⃣ Fecha y hora en la que prefieres ser contactado"
    ]

# Confirmación final al recibir los datos de contacto
def procesar_datos_contacto(wa_id: str, nombre: str, numero: str, fecha: str) -> str:
    notificar_asesor(
        f"📞 *Prospecto empresarial listo para contactar:*
"
        f"📱 WA: {wa_id}
👤 Nombre: {nombre}
📆 Fecha/hora: {fecha}
📞 Teléfono: {numero}"
    )
    return "¡Perfecto! Hemos registrado tus datos. 📌 El asesor te llamará en el horario indicado. ¡Gracias!"

