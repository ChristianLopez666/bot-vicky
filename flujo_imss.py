
import re
from typing import Tuple, Dict, Any, Optional, List

from registro_leads import registrar_lead
from notificar_asesor import notificar_asesor
from read_manual_imss import responder_con_manual

# --- Textos base ---
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

EMPATIA_NO_PROSPECTO = (
    "Gracias por tu interés 🙌. Por ahora *no cumples con los requisitos* para este préstamo.

"
    "Aun así, me encantaría apoyarte con otras opciones que podrían beneficiarte. "
    "Aquí tienes nuestro menú de servicios, elige el que te interese y sigo contigo:

"
    f"{MENU_PRINCIPAL}"
)

BENEFICIOS_INBURSA = (
    "🎁 *Beneficios adicionales con Nómina Inbursa:*
"
    "• Servicio de nómina *sin costo* ✅
"
    "• *Seguro de liberación de saldos por fallecimiento* ✅
"
    "• *Sin penalización* por pagos anticipados ✅"
)

# --- Utilidades ---
AFFIRM = {"si", "sí", "claro", "affirmative", "afirmativo", "soy pensionado", "pensionado", "jubilado", "correcto", "de acuerdo"}
NEGATE = {"no", "nop", "negativo", "no soy", "no pensionado", "no jubilado"}

def es_afirmativo(texto: str) -> bool:
    t = texto.strip().lower()
    return any(p in t for p in AFFIRM)

def es_negativo(texto: str) -> bool:
    t = texto.strip().lower()
    return any(p in t for p in NEGATE)

def extraer_monto(texto: str) -> Optional[int]:
    # Extrae números y los interpreta como monto
    # Acepta formatos con $ y comas/puntos
    numeros = re.findall(r"\d+", texto.replace(",", "").replace(".", ""))
    if not numeros:
        return None
    try:
        return int("".join(numeros))
    except Exception:
        return None

# --- Flujo IMSS ---
def iniciar_flujo_imss(wa_id: str) -> List[str]:
    """Primer mensaje del embudo IMSS."""
    return ["👋 Hola, ¿*eres pensionado o jubilado* del IMSS *bajo la Ley 73*?"]

def manejar_respuesta_ley73(wa_id: str, texto_usuario: str) -> Tuple[List[str], Optional[str]]:
    """Maneja la respuesta a la pregunta de Ley 73.
    Devuelve (mensajes_a_enviar, proxima_etapa) donde proxima_etapa puede ser 'monto' o None si se descarta.
    """
    if es_negativo(texto_usuario):
        # No es prospecto → respuesta empática + menú
        registrar_lead(whatsapp=wa_id, campaña="IMSS", producto="Préstamo Ley 73", monto="", solicita_contacto="No (no Ley 73)")
        return [EMPATIA_NO_PROSPECTO], None

    if es_afirmativo(texto_usuario):
        # Continuar con monto
        registrar_lead(whatsapp=wa_id, campaña="IMSS", producto="Préstamo Ley 73", monto="", solicita_contacto="Pendiente")
        return ["Perfecto ✅. ¿Qué *monto* te interesa solicitar? (mínimo $40,000)"], "monto"

    # Indeterminado → repreguntar
    return ["Solo para confirmar, ¿*eres pensionado o jubilado* del IMSS *bajo la Ley 73*?"], "ley73"

def manejar_respuesta_monto(wa_id: str, texto_usuario: str) -> Tuple[List[str], Optional[str]]:
    """Maneja la validación del monto y acciones posteriores."""
    monto = extraer_monto(texto_usuario)
    if monto is None:
        return ["¿Podrías indicarme el *monto* que necesitas? (ej. $40,000)"], "monto"

    if monto < 40000:
        registrar_lead(whatsapp=wa_id, campaña="IMSS", producto="Préstamo Ley 73", monto=str(monto), solicita_contacto="No (monto < 40k)")
        return [EMPATIA_NO_PROSPECTO], None

    # Prospecto válido (cumple Ley 73 y monto >= 40k)
    registrar_lead(whatsapp=wa_id, campaña="IMSS", producto="Préstamo Ley 73", monto=str(monto), solicita_contacto="Sí (prospecto válido)")
    notificar_asesor(f"✅ *Prospecto IMSS válido*.
📱 WA: {wa_id}
💵 Monto: ${monto:,}
➡️ Requisitos OK (Ley 73 y monto ≥ $40,000)")

    mensajes = [
        "Excelente, cumples con los requisitos iniciales ✅.",
        BENEFICIOS_INBURSA,
        "Te comento: *serás contactado por Christian* para continuar el proceso. "
        "Mientras tanto, si tienes *cualquier duda*, escríbela y te respondo *con base en el manual oficial del IMSS*."
    ]
    return mensajes, "manual"

def responder_desde_manual(pregunta: str) -> str:
    """Utiliza el manual del IMSS para responder estrictamente con base en su contenido."""
    respuesta = responder_con_manual(pregunta)
    return respuesta or "Por ahora no pude acceder al manual. Intenta nuevamente en unos minutos."
