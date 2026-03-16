import os
import json
import logging
import re
import hmac
import hashlib
import threading
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ---------------------------------------------------------------
# LOGGING — configurado primero para que todos los warnings posteriores lo usen
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Zona horaria operativa — Ciudad de México (UTC-6 invierno / UTC-5 verano)
try:
    import pytz as _pytz
    _TZ_MX = _pytz.timezone("America/Mexico_City")
    def _now_mx() -> str:
        return datetime.now(_TZ_MX).strftime("%Y-%m-%d %H:%M:%S")
except ImportError:
    _TZ_MX = timezone(timedelta(hours=-6))
    def _now_mx() -> str:
        return datetime.now(_TZ_MX).strftime("%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------
# Librerías Google — importación condicional
# ---------------------------------------------------------------
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    _google_libs_ok = True
except ImportError:
    _google_libs_ok = False
    log.warning("⚠️ Librerías Google no instaladas. Sheets deshabilitado. "
                "Agrega google-api-python-client google-auth a requirements.txt")

# ---------------------------------------------------------------
# Variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN             = os.getenv("META_TOKEN")
WABA_PHONE_ID          = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN           = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER         = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY         = os.getenv("OPENAI_API_KEY")
META_APP_SECRET        = os.getenv("META_APP_SECRET", "").strip()

# Template aprobado para notificar al asesor fuera de la ventana de 24h.
# Configura ADVISOR_TEMPLATE_NAME con el nombre exacto de tu template aprobado en Meta.
# El template debe tener UN parámetro {{1}} en el cuerpo con el texto de la notificación.
# Ejemplo: nombre del template = "notificacion_asesor" con cuerpo: {{1}}
# Si no está configurado, solo se intenta texto libre (puede fallar fuera de ventana 24h).
ADVISOR_TEMPLATE_NAME  = os.getenv("ADVISOR_TEMPLATE_NAME", "").strip()
ADVISOR_TEMPLATE_LANG  = os.getenv("ADVISOR_TEMPLATE_LANG", "es_MX").strip()

# Google Sheets — bitácora de conversaciones
GOOGLE_CREDENTIALS_JSON      = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEETS_ID_CONVERSACIONES     = os.getenv("SHEETS_ID_CONVERSACIONES", "").strip()
SHEETS_TAB_CONVERSACIONES    = os.getenv("SHEETS_TAB_CONVERSACIONES", "Conversaciones").strip()

# ---------------------------------------------------------------
# Cliente OpenAI — SDK >= 1.0 requerido (agrega openai>=1.0.0 a requirements.txt)
# ---------------------------------------------------------------
_openai_client = None
if OPENAI_API_KEY:
    try:
        _openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        log.info("✅ Cliente OpenAI inicializado (SDK >= 1.0).")
    except Exception:
        log.exception("❌ No se pudo inicializar cliente OpenAI. Verifica openai>=1.0.0.")
else:
    log.warning("⚠️ OPENAI_API_KEY no configurado. GPT deshabilitado.")

# ---------------------------------------------------------------
# Flask app + estado de usuarios
# ---------------------------------------------------------------
app = Flask(__name__)
user_state: dict = {}
user_data: dict  = {}

# ---------------------------------------------------------------
# Idempotencia de mensajes entrantes (Meta puede reenviar eventos)
# ---------------------------------------------------------------
_processed_ids: set      = set()
_processed_deque: deque  = deque(maxlen=3000)
_idempotency_lock        = threading.Lock()

# ---------------------------------------------------------------
# GOOGLE SHEETS — Inicialización y helpers
# ---------------------------------------------------------------
_sheets_service  = None
_sheets_ready    = False

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_SHEET_HEADERS = [
    "Phone", "Nombre", "Mensaje", "Fecha", "Tipo",
    "Origen", "Servicio", "EstadoEmbudo",
    "ResultadoEnvio", "DetalleError", "MessageID",
]


def _sheets_init() -> bool:
    """
    Inicializa el cliente de Google Sheets API desde la variable de entorno
    GOOGLE_CREDENTIALS_JSON. Retorna True si fue exitoso.
    """
    global _sheets_service, _sheets_ready

    if not _google_libs_ok:
        return False
    if not GOOGLE_CREDENTIALS_JSON:
        log.warning("⚠️ GOOGLE_CREDENTIALS_JSON no configurado. Sheets deshabilitado.")
        return False
    if not SHEETS_ID_CONVERSACIONES:
        log.warning("⚠️ SHEETS_ID_CONVERSACIONES no configurado. Sheets deshabilitado.")
        return False

    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=_SHEETS_SCOPES)
        _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _sheets_ready = True
        log.info("✅ Google Sheets API inicializado correctamente.")
        _sheets_ensure_headers()
        return True
    except json.JSONDecodeError:
        log.exception("❌ GOOGLE_CREDENTIALS_JSON no es JSON válido.")
    except Exception:
        log.exception("❌ Error inicializando Google Sheets API.")
    return False


def _sheets_ensure_headers() -> None:
    """
    Verifica que la fila 1 del tab contenga los encabezados correctos.
    Si está vacía, los escribe.
    """
    if not _sheets_ready or not _sheets_service:
        return
    try:
        rng = f"{SHEETS_TAB_CONVERSACIONES}!A1:K1"
        result = _sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_CONVERSACIONES,
            range=rng,
        ).execute()
        existing = result.get("values", [[]])[0] if result.get("values") else []

        if not existing:
            _sheets_service.spreadsheets().values().update(
                spreadsheetId=SHEETS_ID_CONVERSACIONES,
                range=rng,
                valueInputOption="RAW",
                body={"values": [_SHEET_HEADERS]},
            ).execute()
            log.info("✅ Encabezados escritos en Sheets (fila 1).")
        elif existing != _SHEET_HEADERS:
            log.warning(
                f"⚠️ Fila 1 del tab '{SHEETS_TAB_CONVERSACIONES}' tiene contenido diferente al esperado. "
                f"No se sobreescribió. Encabezados esperados: {_SHEET_HEADERS}"
            )
        else:
            log.info("✅ Encabezados en Sheets verificados correctamente.")
    except Exception:
        log.exception("❌ Error verificando/escribiendo encabezados en Sheets.")


def _safe_str(value, max_len: int = 500) -> str:
    """Convierte cualquier valor a string limpio, truncado y sin None."""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def _normalize_phone(phone) -> str:
    """Devuelve el teléfono como string limpio de dígitos."""
    if not phone:
        return ""
    return re.sub(r"\D", "", str(phone))


# Thread-local para propagar msg_id activo a send_message/notify_advisor
_tl = threading.local()


def _active_msg_id() -> str:
    """Retorna el message_id activo en el hilo actual."""
    return getattr(_tl, "msg_id", "")


_last_service: dict = {}


def _detect_service(phone: str) -> str:
    """Infiere el servicio del usuario a partir de su estado en el embudo."""
    state = user_state.get(phone, "")
    if state.startswith("imss_"):
        return "imss"
    if state.startswith("emp_"):
        return "empresarial"
    if state.startswith("fp_"):
        return "financiamiento_practico"
    return _last_service.get(phone, "desconocido")


def _sheets_append_row(
    phone: str,
    nombre: str,
    mensaje: str,
    tipo: str,
    origen: str,
    servicio: str = "",
    resultado: str = "",
    detalle_error: str = "",
    message_id: str = "",
) -> None:
    """
    Appends una fila de bitácora en la hoja de Google Sheets.
    Si Sheets no está disponible o falla, solo registra en log.
    """
    if not _sheets_ready or not _sheets_service:
        return

    try:
        estado_embudo = _safe_str(user_state.get(_normalize_phone(phone), ""), 100)
        if not servicio:
            servicio = _detect_service(_normalize_phone(phone))

        row = [
            _normalize_phone(phone),
            _safe_str(nombre, 100),
            _safe_str(mensaje, 500),
            _now_mx(),
            _safe_str(tipo, 20),
            _safe_str(origen, 20),
            _safe_str(servicio, 50),
            estado_embudo,
            _safe_str(resultado, 20),
            _safe_str(detalle_error, 300),
            _safe_str(message_id, 100),
        ]

        rng = f"{SHEETS_TAB_CONVERSACIONES}!A:K"
        body = {"values": [row]}
        _sheets_service.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_CONVERSACIONES,
            range=rng,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()

    except HttpError as e:
        log.exception(f"❌ HttpError al escribir en Sheets (phone={phone}): {e}")
    except Exception:
        log.exception(f"❌ Error inesperado al escribir en Sheets (phone={phone})")


def _get_nombre(phone: str) -> str:
    """Obtiene el nombre del usuario desde user_data si existe."""
    datos = user_data.get(phone) or user_data.get(_normalize_phone(phone)) or {}
    return _safe_str(datos.get("nombre", ""), 100)


# ---------------------------------------------------------------
# ENVÍO DE MENSAJES WHATSAPP
# ---------------------------------------------------------------
def send_message(to: str, text: str, _message_id: str = "") -> bool:
    """
    Envía un mensaje de texto por WhatsApp Cloud API.
    Registra el intento en Google Sheets (saliente / bot).
    """
    if not _message_id:
        _message_id = _active_msg_id()
    resultado    = ""
    detalle_err  = ""
    success      = False

    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            log.error("❌ Falta META_TOKEN o WABA_PHONE_ID.")
            resultado   = "error"
            detalle_err = "META_TOKEN o WABA_PHONE_ID no configurados"
            return False

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

        log.info(f"📤 Enviando mensaje a {to}: {text[:60]}...")
        resp = requests.post(url, headers=headers, json=payload, timeout=15)

        if resp.status_code in (200, 201):
            log.info(f"✅ Mensaje enviado a {to}")
            resultado = "ok"
            success   = True
        else:
            log.error(f"❌ Error WhatsApp API {resp.status_code}: {resp.text}")
            resultado   = "error"
            detalle_err = f"HTTP {resp.status_code}: {resp.text[:200]}"

    except Exception as e:
        log.exception(f"💥 Excepción en send_message a {to}: {e}")
        resultado   = "error"
        detalle_err = str(e)[:300]

    finally:
        try:
            _sheets_append_row(
                phone         = str(to),
                nombre        = _get_nombre(str(to)),
                mensaje       = text,
                tipo          = "saliente",
                origen        = "bot",
                resultado     = resultado,
                detalle_error = detalle_err,
                message_id    = _message_id,
            )
        except Exception:
            log.exception("❌ Error registrando send_message en Sheets")

    return success


# ---------------------------------------------------------------
# NOTIFICAR AL ASESOR
# Estrategia robusta de dos niveles:
#   Nivel 1 — mensaje de texto libre (funciona dentro de la ventana 24h).
#   Nivel 2 — template aprobada (para fuera de ventana 24h).
#             Requiere ADVISOR_TEMPLATE_NAME configurado.
# ---------------------------------------------------------------
def _send_advisor_template(message: str) -> tuple:
    """
    Envía notificación al asesor usando template aprobada de Meta.
    Necesaria cuando el asesor no ha escrito en las últimas 24h.

    El template debe tener UN parámetro {{1}} en el cuerpo con el texto de la notificación.
    Configura ADVISOR_TEMPLATE_NAME y ADVISOR_TEMPLATE_LANG en las variables de entorno.

    Retorna (success: bool, resultado: str, detalle_error: str).
    """
    if not ADVISOR_TEMPLATE_NAME:
        return (
            False, "error",
            "ADVISOR_TEMPLATE_NAME no configurado — define esta variable con el nombre "
            "de tu template aprobado en Meta Business Manager para notificaciones fuera de ventana 24h."
        )
    if not META_TOKEN or not WABA_PHONE_ID:
        return (False, "error", "META_TOKEN o WABA_PHONE_ID no configurados")

    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }
    # Los parámetros de template tienen límite de 1024 caracteres
    msg_truncated = message[:1024]
    payload = {
        "messaging_product": "whatsapp",
        "to": str(ADVISOR_NUMBER),
        "type": "template",
        "template": {
            "name": ADVISOR_TEMPLATE_NAME,
            "language": {"code": ADVISOR_TEMPLATE_LANG or "es_MX"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": msg_truncated}
                    ]
                }
            ]
        }
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            log.info("✅ Notificación enviada al asesor vía template aprobada")
            return (True, "ok", "")
        else:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            log.error(f"❌ Template al asesor falló: {err}")
            return (False, "error", err)
    except Exception as e:
        return (False, "error", str(e)[:300])


def notify_advisor(message: str, _message_id: str = "") -> bool:
    """
    Envía una notificación al asesor.

    Estrategia:
    1. Intenta mensaje de texto libre (funciona dentro de ventana 24h).
    2. Si falla, reintenta con template aprobada (ADVISOR_TEMPLATE_NAME).
    3. Si template no está configurada, registra instrucciones claras en log.

    Registra en Sheets con Origen=asesor y Tipo=saliente.
    """
    if not _message_id:
        _message_id = _active_msg_id()
    if not ADVISOR_NUMBER:
        log.error("❌ ADVISOR_NUMBER no configurado")
        return False

    log.info(f"📨 Notificando al asesor: {message[:100]}...")

    resultado   = ""
    detalle_err = ""
    success     = False

    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            resultado   = "error"
            detalle_err = "META_TOKEN o WABA_PHONE_ID no configurados"
            return False

        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(ADVISOR_NUMBER),
            "type": "text",
            "text": {"body": message},
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=15)

        if resp.status_code in (200, 201):
            log.info("✅ Notificación enviada al asesor (texto libre)")
            resultado = "ok"
            success   = True
        else:
            texto_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            log.warning(
                f"⚠️ Texto libre al asesor falló ({texto_err}). "
                "Probable causa: ventana 24h expirada. Reintentando con template..."
            )
            ok_t, res_t, err_t = _send_advisor_template(message)
            if ok_t:
                resultado = "ok"
                success   = True
            else:
                resultado   = "error"
                detalle_err = f"texto: {texto_err} | template: {err_t}"
                if not ADVISOR_TEMPLATE_NAME:
                    log.warning(
                        "⚠️ Para garantizar la notificación al asesor fuera de la ventana de 24h:\n"
                        "   1. Crea un template aprobado en Meta Business Manager con un parámetro {{1}}.\n"
                        "   2. Agrega ADVISOR_TEMPLATE_NAME=nombre_del_template a tu .env.\n"
                        "   3. Opcionalmente: ADVISOR_TEMPLATE_LANG=es_MX (o el idioma del template)."
                    )

    except Exception as e:
        log.exception(f"💥 Excepción en notify_advisor: {e}")
        resultado   = "error"
        detalle_err = str(e)[:300]

    finally:
        try:
            _sheets_append_row(
                phone         = str(ADVISOR_NUMBER),
                nombre        = "Asesor",
                mensaje       = message,
                tipo          = "saliente",
                origen        = "asesor",
                resultado     = resultado,
                detalle_error = detalle_err,
                message_id    = _message_id,
            )
        except Exception:
            log.exception("❌ Error registrando notify_advisor en Sheets")

    return success


# ---------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------
_RESP_POSITIVE_WORDS   = {"si", "sip", "claro", "ok", "vale", "afirmativo",
                           "yes", "correcto", "exacto", "andale", "dale"}
_RESP_POSITIVE_PHRASES = {"por supuesto", "desde luego", "claro que si",
                           "claro que sí", "con gusto"}
_RESP_STRONG_NEG_WORDS = {"no", "nel", "nop", "negativo", "tampoco",
                           "nunca", "jamas"}


def interpret_response(text: str) -> str:
    """
    Interpreta si el texto es una respuesta positiva, negativa o neutral.
    Prioridad: negativa fuerte > positiva > neutral.
    """
    if not text:
        return "neutral"

    norm = normalize_text(text)
    tokens = set(norm.split())

    if tokens & _RESP_STRONG_NEG_WORDS:
        return "negative"
    if tokens & _RESP_POSITIVE_WORDS:
        return "positive"
    if any(phrase in norm for phrase in _RESP_POSITIVE_PHRASES):
        return "positive"

    return "neutral"


def extract_number(text: str):
    """Extrae el primer número del texto. Retorna float o None."""
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "").replace(" ", "")
    m = re.search(r"(\d{1,12})(\.\d+)?", clean)
    if not m:
        return None
    try:
        return float(m.group(1) + (m.group(2) or ""))
    except Exception:
        return None


def send_main_menu(phone: str):
    """Menú principal — claro, jerarquizado y comercial."""
    menu = (
        "🏦 *Servicios Financieros Inbursa*\n"
        "─────────────────────────────\n"
        "1️⃣  *Préstamo IMSS Pensionados* _(Ley 73)_\n"
        "     💰 $40K–$650K · Sin aval · Vía pensión\n\n"
        "2️⃣  *Seguro de Auto*\n"
        "     🚗 Cobertura amplia · Asistencia 24/7\n\n"
        "3️⃣  *Seguro de Vida y Salud*\n"
        "     🏥 Vida · GMM · Hospitalización\n\n"
        "4️⃣  *Tarjeta Médica VRIM*\n"
        "     💳 Consultas ilimitadas · Labs · Medicamentos\n\n"
        "5️⃣  *Financiamiento Empresarial*\n"
        "     🏢 $100K–$100M · PYMES y empresas\n\n"
        "6️⃣  *Financiamiento Práctico Empresarial*\n"
        "     ⚡ Aprobación desde 24 hrs · Sin garantía\n"
        "─────────────────────────────\n"
        "Escribe el *número* o el nombre del servicio que te interesa. 😊"
    )
    send_message(phone, menu)


# ---------------------------------------------------------------
# GPT — helpers
# ---------------------------------------------------------------
def ask_gpt(prompt: str, model: str = "gpt-4o-mini", temperature: float = 0.3) -> str:
    """
    Llama a OpenAI usando el cliente instanciado explícitamente.
    SDK >= 1.0. Temperature 0.3 — preciso para contexto financiero.
    """
    if not _openai_client:
        return "Lo siento, el servicio de consulta GPT no está configurado."
    try:
        resp = _openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception(f"Error OpenAI: {e}")
        return "Lo siento, ocurrió un error al consultar GPT."


def ask_gpt_hybrid(user_prompt: str, system_prompt: str = "",
                   model: str = "gpt-4o-mini", temperature: float = 0.4) -> str:
    """
    Llamada a OpenAI con system_prompt separado para la capa híbrida.
    max_tokens=250 para respuestas breves y comerciales.
    """
    if not _openai_client:
        return "Lo siento, el servicio de consulta no está disponible en este momento."

    if not system_prompt:
        system_prompt = _HYBRID_SYSTEM_PROMPT

    try:
        resp = _openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception(f"Error OpenAI hybrid: {e}")
        return ("En este momento no puedo procesar tu consulta. "
                "¿Te puedo orientar sobre alguno de nuestros servicios: "
                "préstamos IMSS, seguros o financiamiento empresarial?")


def is_gpt_command(msg: str) -> bool:
    return (msg or "").strip().lower().startswith("sgpt:")


# ---------------------------------------------------------------
# SYSTEM PROMPT PARA CAPA HÍBRIDA
# ---------------------------------------------------------------
_HYBRID_SYSTEM_PROMPT = """Eres Vicky, asistente comercial de Christian López, asesor financiero de Inbursa.

Tu único propósito es orientar al usuario sobre EXACTAMENTE estos 6 servicios:
1. Préstamos IMSS Pensionados Ley 73 — montos $40K–$650K, descuento vía pensión, sin aval, plazos 12–60 meses
2. Seguros de Auto Inbursa — cobertura amplia, RC, robo total/parcial, asistencia vial 24/7
3. Seguros de Vida y Salud — vida, gastos médicos mayores, hospitalización, atención 24/7
4. Tarjetas Médicas VRIM — consultas ilimitadas, especialistas, laboratorios, descuentos en medicamentos
5. Financiamiento Empresarial — $100K–$100M, PYMES y empresas consolidadas, tasas preferenciales
6. Financiamiento Práctico Empresarial — aprobación desde 24 horas, desde $100K, sin garantía, para empresas y personas físicas con actividad empresarial

REGLAS ABSOLUTAS (incumplirlas es un error grave):
- Responde SIEMPRE en español mexicano, tono profesional pero cercano y cálido.
- Máximo 80–120 palabras. Si no cabe en eso, recorta; no escribas más.
- NO inventes políticas, tasas, plazos, requisitos ni condiciones que no estén descritos arriba.
- NO prometas aprobación ni resultados garantizados.
- NO respondas temas ajenos a los 6 servicios. Si la consulta no corresponde a ninguno, responde en 1–2 líneas explicando que solo puedes orientar sobre esos servicios e invita a elegir uno.
- NO repitas el menú completo. Menciona opciones solo si es imprescindible para orientar.
- SIEMPRE termina con UNA sola pregunta útil o una guía concreta al siguiente paso.
- Si la intención no está clara, haz UNA sola pregunta de aclaración concisa.
- Tu objetivo es la conversión: llevar al usuario a elegir un servicio específico.
"""


# ---------------------------------------------------------------
# CAPA HÍBRIDA — funciones auxiliares
# ---------------------------------------------------------------

def normalize_text(text: str) -> str:
    """
    Normaliza texto para comparaciones semánticas:
    minúsculas, sin tildes (unicodedata NFD), sin signos de puntuación, espacios simples.
    """
    if not text:
        return ""
    import unicodedata
    t = unicodedata.normalize("NFD", text.lower().strip())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("ñ", "n")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------
# DETECCIÓN DE LEADS DE CAMPAÑA IMSS
# Prioridad máxima — evaluada ANTES que cualquier trigger de menú.
# Intercepta leads de anuncios FB/IG click-to-WhatsApp y los enruta
# directamente al embudo IMSS sin mostrar el menú general.
# ---------------------------------------------------------------

# Frases exactas (tras normalización) que señalan lead de campaña IMSS.
# Incluye aperturas en inglés (señal de anuncio click-to-WA desde FB/IG),
# solicitudes genéricas de información y términos de préstamo/pensión.
_IMSS_CAMPAIGN_TRIGGERS_EXACT: set = {
    # Aperturas en inglés — señal fuerte de anuncio click-to-WhatsApp
    "hello", "hi", "hey", "yo",
    # Solicitudes de información sin especificar servicio distinto
    "info", "informacion", "informes",
    "mas informacion", "mas info", "mas informes",
    "quiero informacion", "quiero info", "quiero informes",
    "quiero saber", "quiero saber mas",
    "quiero mas informacion", "quiero mas info",
    "me pueden informar", "me puedes informar",
    "me interesa", "me interesa saber",
    # Intención de préstamo genérica o IMSS
    "prestamo", "prestamos",
    "quiero prestamo", "quiero un prestamo", "quiero el prestamo",
    "quiero credito", "quiero un credito", "quiero el credito",
    "me interesa el prestamo", "me interesa el credito",
    "necesito un prestamo", "necesito prestamo",
    "necesito dinero", "necesito credito",
    # Calificación
    "califico", "si califico", "como califico",
    "quiero saber si califico", "puedo calificar",
    "quiero calificar", "quiero aplicar", "como aplico",
    # Términos IMSS / pensión directos
    "jubilado", "jubilada", "pensionado", "pensionada",
}

# Frases que, contenidas en el mensaje, indican lead de campaña IMSS.
_IMSS_CAMPAIGN_CONTAINMENT: set = {
    "can i get more info", "more info on this", "more info about",
    "get more info", "more information", "send me info", "give me info",
    "quiero saber si califico",
    "me interesa el prestamo", "me interesa el credito",
    "quiero informacion sobre el prestamo",
    "quiero saber del prestamo",
    "quiero saber sobre el prestamo",
    "informacion sobre el prestamo",
    "quiero saber si puedo",
    "como puedo aplicar",
}


def _is_imss_campaign_lead(message: dict, norm_text: str) -> bool:
    """
    Detecta si el mensaje corresponde a un lead de campaña IMSS.

    Orden de prioridad:
    1. Metadata de referral de Meta (anuncio click-to-WhatsApp) → señal definitiva.
    2. Coincidencia exacta con trigger phrases normalizadas.
    3. Coincidencia por contenido (substring).

    Retorna True si el lead debe ir al flujo IMSS directamente, sin pasar por el menú.
    """
    # 1. Referral de Meta — señal más confiable (clickeo en anuncio)
    referral = message.get("referral") or {}
    if referral:
        source_type = (referral.get("source_type") or "").lower()
        if source_type == "ad":
            log.info("📌 Referral de anuncio detectado (source_type=ad) → campaña IMSS")
            return True
        # Referral sin tipo "ad" — verificar contenido
        headline = normalize_text(referral.get("headline") or "")
        body_ref = normalize_text(referral.get("body") or "")
        imss_kw = {"imss", "pension", "pensionado", "prestamo", "jubilado", "ley 73"}
        if any(kw in headline or kw in body_ref for kw in imss_kw):
            log.info("📌 Referral con keywords IMSS detectado → campaña IMSS")
            return True

    # 2. Coincidencia exacta con trigger phrases
    if norm_text in _IMSS_CAMPAIGN_TRIGGERS_EXACT:
        log.info(f"📌 Trigger exacto campaña IMSS: '{norm_text}'")
        return True

    # 3. Coincidencia por contenido (substring)
    if any(phrase in norm_text for phrase in _IMSS_CAMPAIGN_CONTAINMENT):
        log.info(f"📌 Trigger contenido campaña IMSS en: '{norm_text[:60]}'")
        return True

    return False


# ---------------------------------------------------------------
# MAPA DE OPCIONES EXACTAS DEL MENÚ
# Fuente única de verdad. Keys pre-normalizadas (sin tildes, minúsculas).
# ---------------------------------------------------------------
_MENU_OPTIONS: dict = {
    # --- Opción 1: Préstamo IMSS Pensionados ---
    "1": "prestamo_imss",
    "imss": "prestamo_imss",
    "prestamo imss": "prestamo_imss",
    "prestamos imss": "prestamo_imss",
    "prestamo pensionado": "prestamo_imss",
    "prestamo pensionados": "prestamo_imss",
    "credito imss": "prestamo_imss",
    "credito pensionado": "prestamo_imss",
    "credito pensionados": "prestamo_imss",
    "prestamo imss pensionados": "prestamo_imss",
    "prestamos imss pensionados": "prestamo_imss",
    "ley 73": "prestamo_imss",
    "pension": "prestamo_imss",
    "pensionado": "prestamo_imss",
    "pensionados": "prestamo_imss",
    # --- Opción 2: Seguros de Auto ---
    "2": "seguro_auto",
    "seguro auto": "seguro_auto",
    "seguro carro": "seguro_auto",
    "seguro vehiculo": "seguro_auto",
    "seguros de auto": "seguro_auto",
    "seguro de auto": "seguro_auto",
    "seguro para auto": "seguro_auto",
    "seguro para carro": "seguro_auto",
    "seguro para vehiculo": "seguro_auto",
    # --- Opción 3: Seguros de Vida y Salud ---
    "3": "seguro_vida",
    "seguro vida": "seguro_vida",
    "seguros de vida": "seguro_vida",
    "seguro de vida": "seguro_vida",
    "seguro de vida y salud": "seguro_vida",
    "seguros de vida y salud": "seguro_vida",
    "seguro salud": "seguro_vida",
    "seguro de salud": "seguro_vida",
    "seguro medico": "seguro_vida",
    "gastos medicos": "seguro_vida",
    "gastos medicos mayores": "seguro_vida",
    # --- Opción 4: Tarjetas Médicas VRIM ---
    "4": "vrim",
    "vrim": "vrim",
    "tarjeta medica": "vrim",
    "tarjetas medicas": "vrim",
    "membresia medica": "vrim",
    "consultas medicas": "vrim",
    # --- Opción 5: Financiamiento Empresarial ---
    "5": "empresarial",
    "financiamiento empresarial": "empresarial",
    "credito empresarial": "empresarial",
    "pyme": "empresarial",
    # --- Opción 6: Financiamiento Práctico Empresarial ---
    "6": "financiamiento_practico",
    "financiamiento practico": "financiamiento_practico",
    "financiamiento practico empresarial": "financiamiento_practico",
    "credito practico": "financiamiento_practico",
    "credito simple": "financiamiento_practico",
    "credito rapido": "financiamiento_practico",
    "prestamo rapido": "financiamiento_practico",
}

# Frases que activan el menú principal explícitamente.
# IMPORTANTE: "quiero informacion" y "quiero info" se ELIMINARON de esta lista.
# Ambas frases son indicadores de lead de campaña y se capturan en el paso 4.5
# (_is_imss_campaign_lead) antes de que lleguen aquí.
# El menú solo aparece cuando la intención es navegar el catálogo, no cuando
# el lead busca información sobre un producto.
_MENU_TRIGGER_PHRASES: set = {
    # Palabras clave de menú / navegación explícita
    "menu", "memu", "inicio", "start",
    # Saludos en español (no se tratan como leads de campaña)
    "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches",
    # Solicitudes explícitas de catálogo / servicios
    "servicios", "opciones", "catalogo", "productos",
    "que manejas", "que ofrecen", "que ofreces", "que tienen", "que tienes",
    "que servicios tienen", "que servicios ofrecen", "que servicios manejan",
    "quiero ver opciones",
    "en que me puedes ayudar", "para que sirves",
}

# Palabras clave para detección semántica (puntaje ponderado).
_SEMANTIC_KEYWORDS: dict = {
    "prestamo_imss": {
        "strong": [
            "prestamo pensionado", "prestamo para pensionado", "prestamo imss",
            "credito imss", "prestamo jubilado", "credito pensionado",
            "necesito dinero pensionado", "calificar prestamo",
            "descuento pension", "cobro imss", "ley 73",
            "soy pensionado", "estoy jubilado", "estoy pensionado",
            "quiero prestamo pensionado", "quiero credito pensionado",
        ],
        "weak": ["pensionado", "jubilado", "jubilada", "pension", "pensionada"],
    },
    "seguro_auto": {
        "strong": [
            "asegurar carro", "seguro carro", "seguro auto", "seguro vehiculo",
            "seguro automovil", "cotizar seguro auto", "cobertura vehiculo",
            "cobertura carro", "seguro para mi auto", "seguro para mi carro",
            "asegurar vehiculo", "poliza auto", "seguro de mi carro",
            "quiero seguro auto", "necesito seguro auto", "seguro de auto",
        ],
        "weak": [],
    },
    "seguro_vida": {
        "strong": [
            "seguro de vida", "seguro vida", "gastos medicos", "seguro medico",
            "seguro salud", "proteger familia", "cobertura medica",
            "seguro enfermedad", "seguro de salud", "seguro familiar",
            "vida y salud", "cobertura familiar",
            "quiero seguro vida", "necesito seguro vida",
            "gastos medicos mayores",
        ],
        "weak": [],
    },
    "vrim": {
        "strong": [
            "tarjeta medica", "tarjetas medicas", "membresia medica",
            "consultas medicas", "consultas con descuento", "descuentos medicamentos",
            "especialistas descuento", "laboratorios descuento",
            "plan medico", "que incluye vrim", "acceso medico",
            "consultas ilimitadas",
        ],
        "weak": ["vrim"],
    },
    "empresarial": {
        "strong": [
            "credito empresa", "credito empresarial", "prestamo empresa",
            "prestamo empresarial", "financiamiento empresa",
            "capital para negocio", "credito para negocio", "prestamo negocio",
            "financiar empresa", "capital de trabajo", "inversion empresa",
            "financiar mi negocio", "credito pyme", "mi empresa necesita",
            "necesito credito empresa", "necesito financiamiento empresa",
        ],
        "weak": [],
    },
    "financiamiento_practico": {
        "strong": [
            "financiamiento rapido", "credito rapido", "aprobacion rapida",
            "aprobacion en 24", "24 horas", "liquidez rapida", "liquidez negocio",
            "facturo necesito credito", "facturacion credito",
            "credito sin garantia", "sin garantia empresa",
            "necesito liquidez", "prestamo rapido negocio",
            "financiamiento en 24", "credito en 24 horas", "quiero liquidez",
        ],
        "weak": [],
    },
}


def detect_exact_option(text: str) -> str | None:
    """Busca coincidencia exacta en el mapa de opciones del menú."""
    normalized = normalize_text(text)
    if normalized in _MENU_OPTIONS:
        return _MENU_OPTIONS[normalized]
    return None


def detect_semantic_intent(text: str) -> str | None:
    """
    Detecta intención semántica usando palabras clave ponderadas por servicio.

    Sistema de puntaje:
    - Frase fuerte (2+ palabras): 2.0 pts
    - Palabra débil (1 palabra):  1.0 pt
    - Umbral mínimo para activar: 2.0 pts

    Desambiguación 5 vs 6:
    - Señal de urgencia/liquidez/24h → opción 6 (Financiamiento Práctico)
    - Contexto empresarial sin urgencia → opción 5 (Financiamiento Empresarial)

    Empates entre otros servicios → None (va a GPT).
    """
    normalized = normalize_text(text)
    msg_words = set(normalized.split())
    scores: dict = {}

    def kw_matches(kw_norm: str) -> bool:
        if kw_norm in normalized:
            return True
        kw_words = set(kw_norm.split())
        if len(kw_words) >= 2 and kw_words.issubset(msg_words):
            return True
        return False

    for service, kw_groups in _SEMANTIC_KEYWORDS.items():
        score = 0.0
        for kw in kw_groups.get("strong", []):
            if kw_matches(normalize_text(kw)):
                score += 2.0
        for kw in kw_groups.get("weak", []):
            if kw_matches(normalize_text(kw)):
                score += 1.0
        if score >= 2.0:
            scores[service] = score

    if not scores:
        return None

    _URGENCY_SIGNALS = {
        "rapido", "rapida", "urgente", "urgencia", "liquidez",
        "24 horas", "24h", "inmediato", "inmediata", "pronto",
        "hoy mismo", "en 24", "sin garantia",
    }
    _BUSINESS_SIGNALS = {
        "empresa", "negocio", "pyme", "capital", "trabajo",
        "empresarial", "factura", "facturo",
    }
    normalized_for_56 = normalize_text(text)
    has_urgency  = any(s in normalized_for_56 for s in _URGENCY_SIGNALS)
    has_business = any(s in normalized_for_56 for s in _BUSINESS_SIGNALS)

    if has_business:
        if has_urgency:
            return "financiamiento_practico"
        if scores.get("empresarial", 0) >= 2.0:
            return "empresarial"
        if scores.get("financiamiento_practico", 0) >= 2.0 and "empresarial" not in scores:
            return "empresarial"

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_service, best_score = ranked[0]

    if len(ranked) > 1:
        second_service, second_score = ranked[1]

        if set([best_service, second_service]) == {"empresarial", "financiamiento_practico"}:
            if has_urgency:
                return "financiamiento_practico"
            return "empresarial"

        if best_score == second_score:
            return None

    return best_service


# Registro de último modo de resolución por usuario (anti-repetición).
_last_route: dict = {}


def _reset_user_session(phone: str) -> None:
    """Limpia todo el estado conversacional del usuario."""
    user_state.pop(phone, None)
    user_data.pop(phone, None)
    _last_service.pop(phone, None)
    _last_route.pop(phone, None)


def _route_to_service(phone: str, service: str, msg_id: str) -> None:
    """Enruta al flujo de servicio correcto dado su nombre."""
    if service == "prestamo_imss":
        _last_service[phone] = "imss"
        user_state[phone] = "imss_beneficios"
        user_data.setdefault(phone, {})
        funnel_prestamo_imss(phone, "1")

    elif service == "empresarial":
        _last_service[phone] = "empresarial"
        user_state[phone] = "emp_beneficios"
        user_data.setdefault(phone, {})
        funnel_credito_empresarial(phone, "5")

    elif service == "financiamiento_practico":
        _last_service[phone] = "financiamiento_practico"
        user_state[phone] = "fp_intro"
        user_data.setdefault(phone, {})
        funnel_financiamiento_practico(phone, "6")

    elif service == "seguro_auto":
        _last_service[phone] = "seguro_auto"
        send_message(
            phone,
            "🚗 *Seguro de Auto Inbursa*\n\n"
            "✅ Cobertura amplia\n"
            "✅ Robo total y parcial\n"
            "✅ Responsabilidad civil\n"
            "✅ Asistencia vial 24/7\n\n"
            "📞 Un asesor te contactará para cotizar tu vehículo.",
            msg_id,
        )
        notify_advisor(
            f"🚗 INTERESADO EN SEGURO AUTO\nWhatsApp: {phone}", msg_id
        )

    elif service == "seguro_vida":
        _last_service[phone] = "seguro_vida"
        send_message(
            phone,
            "🏥 *Seguro de Vida y Salud Inbursa*\n\n"
            "✅ Seguro de vida\n"
            "✅ Gastos médicos mayores\n"
            "✅ Hospitalización\n"
            "✅ Atención 24/7\n\n"
            "📞 Un asesor te contactará para explicarte coberturas y costos.",
            msg_id,
        )
        notify_advisor(
            f"🏥 INTERESADO EN VIDA/SALUD\nWhatsApp: {phone}", msg_id
        )

    elif service == "vrim":
        _last_service[phone] = "vrim"
        send_message(
            phone,
            "💳 *Tarjeta Médica VRIM*\n\n"
            "✅ Consultas ilimitadas con médico general\n"
            "✅ Acceso a especialistas\n"
            "✅ Laboratorios y estudios\n"
            "✅ Descuentos en medicamentos\n\n"
            "📞 Un asesor te contactará para explicarte los planes disponibles.",
            msg_id,
        )
        notify_advisor(
            f"💳 INTERESADO EN VRIM\nWhatsApp: {phone}", msg_id
        )


def handle_hybrid_message(phone: str, text: str, msg_id: str) -> None:
    """
    Capa híbrida de interpretación natural.
    Se llama solo cuando no hubo coincidencia exacta con el menú.

    Orden de decisión:
    1. Semántica clara (score >= 2.0) → enrutar al servicio.
    2. Hint financiero real → GPT (anti-repetición: si ya fue GPT → aclaración).
    3. Anti-repetición de menú/aclaración → aclaración breve.
    4. Último recurso → menú completo (una sola vez).
    """
    last_route = _last_route.get(phone, "")

    # --- Paso 1: intención semántica clara ---
    intent = detect_semantic_intent(text)
    if intent:
        log.info(f"📍 {phone}: route=semantic → {intent}")
        _last_route[phone] = "semantic"
        _route_to_service(phone, intent, msg_id)
        return

    # --- Paso 2: activar GPT solo si hay hint financiero REAL ---
    normalized = normalize_text(text)
    tokens = set(normalized.split())

    _FINANCIAL_HINTS = {
        "seguro", "prestamo", "credito", "financiamiento", "inbursa",
        "dinero", "beneficio", "tarjeta", "medico", "medica",
        "pension", "jubilado", "pensionado",
        "cotizar", "precio", "costo", "aplico", "califico",
        "requisito", "documentos", "aplica",
    }
    _CONSULTIVE_PHRASES = {
        "como funciona", "cuanto cuesta", "que necesito", "que cubre",
        "si califico", "que documentos", "que requisitos", "cuanto es",
        "como aplico", "en que consiste", "que incluye", "que es el",
        "que es la", "explicame", "informame", "como accedo",
    }

    has_financial_hint = bool(tokens & _FINANCIAL_HINTS)
    has_consultive     = any(p in normalized for p in _CONSULTIVE_PHRASES)

    should_use_gpt = has_financial_hint or has_consultive

    if should_use_gpt:
        if last_route == "gpt":
            log.info(f"📍 {phone}: route=clarification (tras GPT)")
            _last_route[phone] = "clarification"
            send_message(
                phone,
                "¿Tu consulta es sobre *pensión*, *seguros* o *financiamiento para empresa*? "
                "Con eso te oriento directo al servicio correcto. 😊",
                msg_id,
            )
            return

        log.info(f"📍 {phone}: route=gpt")
        _last_route[phone] = "gpt"
        gpt_reply = ask_gpt_hybrid(text)
        send_message(phone, gpt_reply, msg_id)
        return

    # --- Paso 3: anti-repetición de menú / aclaración ---
    if last_route in ("menu", "clarification"):
        log.info(f"📍 {phone}: route=clarification (anti-repetición de menú)")
        _last_route[phone] = "clarification"
        send_message(
            phone,
            "¿En qué puedo ayudarte hoy? 😊\n"
            "¿Buscas apoyo para *pensión*, *seguros* o *financiamiento empresarial*?",
            msg_id,
        )
        return

    # --- Paso 4: menú completo como último recurso ---
    log.info(f"📍 {phone}: route=menu")
    _last_route[phone] = "menu"
    send_main_menu(phone)


# ---------------------------------------------------------------
# EMBUDO – PRÉSTAMO IMSS PENSIONADOS (Opción 1)
# ---------------------------------------------------------------
def funnel_prestamo_imss(user_id: str, user_message: str):
    state = user_state.get(user_id, "imss_beneficios")
    datos = user_data.get(user_id, {})

    # ── Apertura para leads de campaña ────────────────────────────────────────
    # Activado cuando el sistema detecta un lead de anuncio FB/IG.
    # Directo y comercial: muestra beneficios clave y hace UNA sola pregunta.
    if state == "imss_campaign_open":
        send_message(
            user_id,
            "💰 *Préstamo para Pensionados IMSS (Ley 73)*\n\n"
            "✅ Montos desde *$40,000 hasta $650,000*\n"
            "✅ Sin aval ni garantía\n"
            "✅ Descuento directo vía tu pensión\n"
            "✅ Depósito a tu cuenta en días\n"
            "✅ Atención personalizada\n\n"
            "*¿Ya eres pensionado o jubilado del IMSS bajo la Ley 73?*"
        )
        user_state[user_id] = "imss_preg_pensionado"
        return jsonify({"status": "ok", "funnel": "imss_campaign"})

    # ── Apertura estándar (desde menú o selección directa) ───────────────────
    if state == "imss_beneficios":
        send_message(
            user_id,
            "💰 *Préstamo para Pensionados IMSS (Ley 73)*\n\n"
            "✅ Montos desde *$40,000 hasta $650,000*\n"
            "✅ Sin aval ni garantía\n"
            "✅ Descuento vía pensión — sin trámites complicados\n"
            "✅ Plazos de 12 a 60 meses\n"
            "✅ Depósito directo a tu cuenta\n\n"
            "🏦 *Si ya recibes tu pensión en Inbursa*, aplican beneficios adicionales:\n"
            "tasas preferenciales, seguro de vida y anticipo de nómina.\n"
            "_(No es obligatorio — son ventajas adicionales)_\n\n"
            "*¿Ya eres pensionado o jubilado del IMSS bajo la Ley 73?*"
        )
        user_state[user_id] = "imss_preg_pensionado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # ── Respuesta a la pregunta de calificación ───────────────────────────────
    if state == "imss_preg_pensionado":
        resp = interpret_response(user_message)
        if resp == "negative":
            # No califica — respuesta cálida, no cortar la conversación
            send_message(
                user_id,
                "Gracias por confirmarlo 🙏\n\n"
                "Este financiamiento está diseñado exclusivamente para pensionados y "
                "jubilados del IMSS bajo la *Ley 73*.\n\n"
                "Sin embargo, contamos con otras opciones que podrían adaptarse a tu "
                "situación. ¿Te gustaría que un asesor te oriente sobre alternativas?"
            )
            user_state[user_id] = "imss_no_califica_asesor"
            return jsonify({"status": "ok"})
        if resp == "positive":
            send_message(
                user_id,
                "¡Perfecto! 👏 Eso es justo lo que necesitamos.\n\n"
                "*¿Cuánto recibes aproximadamente al mes por concepto de pensión?*\n"
                "_(Puedes escribir el monto, ej. 7500)_"
            )
            user_state[user_id] = "imss_preg_monto_pension"
            return jsonify({"status": "ok"})
        send_message(user_id, "Por favor responde *sí* o *no* para continuar. 😊")
        return jsonify({"status": "ok"})

    # ── Manejo de no calificados ──────────────────────────────────────────────
    if state == "imss_no_califica_asesor":
        resp = interpret_response(user_message)
        if resp == "positive":
            notify_advisor(
                f"📣 PROSPECTO NO CALIFICA – IMSS LEY 73\n"
                f"WhatsApp: {user_id}\n"
                f"No es pensionado IMSS Ley 73.\n"
                f"Origen: {datos.get('origen', 'directo')}\n"
                f"Solicitó orientación sobre otras alternativas."
            )
            send_message(
                user_id,
                "¡Perfecto! 👍 Le avisaré a nuestro asesor *Christian López* para que "
                "te contacte y explore las mejores opciones para ti.\n\n"
                "Te atendemos a la brevedad. 😊"
            )
        else:
            send_message(
                user_id,
                "Entendido 😊 Aquí estaré si necesitas más información.\n\n"
                "Si en algún momento deseas explorar nuestros otros servicios, con gusto te oriento."
            )
            send_main_menu(user_id)
        _reset_user_session(user_id)
        return jsonify({"status": "ok"})

    # ── Monto de pensión ──────────────────────────────────────────────────────
    if state == "imss_preg_monto_pension":
        monto = extract_number(user_message)
        if monto is None:
            send_message(
                user_id,
                "Indica el monto mensual que recibes por pensión _(ej. 6500)_."
            )
            return jsonify({"status": "ok"})
        datos["pension_mensual"] = monto
        user_data[user_id] = datos
        if monto < 5000:
            send_message(
                user_id,
                "Gracias por la información 🙏\n\n"
                "Por ahora los créditos aplican a pensiones a partir de *$5,000 mensuales*.\n\n"
                "Sin embargo, puedo notificar a nuestro asesor para que te ofrezca opciones "
                "adaptadas a tu caso. ¿Deseas que lo haga?"
            )
            user_state[user_id] = "imss_ofrecer_asesor"
            return jsonify({"status": "ok"})
        send_message(
            user_id,
            "Excelente 💪 Con esa pensión puedes calificar.\n\n"
            "*¿Qué monto de préstamo te gustaría solicitar?*\n"
            "_(Mínimo $40,000 — Máximo $650,000)_"
        )
        user_state[user_id] = "imss_preg_monto_solicitado"
        return jsonify({"status": "ok"})

    if state == "imss_ofrecer_asesor":
        resp = interpret_response(user_message)
        if resp == "positive":
            formatted = (
                "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"WhatsApp: {user_id}\n"
                f"Pensión mensual: ${datos.get('pension_mensual', 'ND')}\n"
                f"Origen: {datos.get('origen', 'directo')}\n"
                "Estatus: Pensión baja — requiere opciones alternativas"
            )
            notify_advisor(formatted)
            send_message(
                user_id,
                "¡Listo! ✅ Un asesor te contactará para ofrecerte las mejores "
                "alternativas disponibles para tu situación."
            )
        else:
            send_message(
                user_id,
                "Entendido 😊 Si en algún momento deseas más información, aquí estaré.\n\n"
                "¿Hay algo más en lo que pueda ayudarte?"
            )
            send_main_menu(user_id)
        _reset_user_session(user_id)
        return jsonify({"status": "ok"})

    # ── Monto solicitado ──────────────────────────────────────────────────────
    if state == "imss_preg_monto_solicitado":
        monto_sol = extract_number(user_message)
        if monto_sol is None or monto_sol < 40000:
            send_message(
                user_id,
                "Indica el monto que deseas solicitar _(mínimo $40,000)_, ej. *65,000*."
            )
            return jsonify({"status": "ok"})
        datos["monto_solicitado"] = monto_sol
        user_data[user_id] = datos
        send_message(
            user_id,
            f"Perfecto, anotado: *${monto_sol:,.0f}* ✅\n\n"
            "*¿Cuál es tu nombre completo?*"
        )
        user_state[user_id] = "imss_preg_nombre"
        return jsonify({"status": "ok"})

    # ── Nombre ────────────────────────────────────────────────────────────────
    if state == "imss_preg_nombre":
        datos["nombre"] = user_message.title()
        user_data[user_id] = datos
        send_message(
            user_id,
            f"Mucho gusto, *{datos['nombre']}* 😊\n\n"
            "*¿Cuál es tu número de teléfono de contacto?*\n"
            "_(Si es el mismo de este WhatsApp, escribe: mismo)_"
        )
        user_state[user_id] = "imss_preg_telefono"
        return jsonify({"status": "ok"})

    # ── Teléfono ──────────────────────────────────────────────────────────────
    if state == "imss_preg_telefono":
        telf = user_message.strip().lower()
        datos["telefono_contacto"] = (
            user_id if telf in ("mismo", "este", "el mismo") else telf
        )
        user_data[user_id] = datos
        send_message(user_id, "*¿En qué ciudad vives?*")
        user_state[user_id] = "imss_preg_ciudad"
        return jsonify({"status": "ok"})

    # ── Ciudad ────────────────────────────────────────────────────────────────
    if state == "imss_preg_ciudad":
        datos["ciudad"] = user_message.title()
        user_data[user_id] = datos
        send_message(
            user_id,
            "*¿Ya recibes tu pensión en Inbursa?* (Sí / No)\n\n"
            "_(Si es en otro banco, no hay problema — igual puedes aplicar)_"
        )
        user_state[user_id] = "imss_preg_nomina_inbursa"
        return jsonify({"status": "ok"})

    # ── Nómina Inbursa + cierre + notificación al asesor ─────────────────────
    if state == "imss_preg_nomina_inbursa":
        resp = interpret_response(user_message)
        datos["nomina_inbursa"] = (
            "Sí" if resp == "positive" else "No" if resp == "negative" else "ND"
        )
        if resp not in ("positive", "negative"):
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok"})

        send_message(
            user_id,
            "✅ *¡Todo listo!*\n\n"
            "Con los datos que me compartiste, tu solicitud quedó registrada.\n"
            "Nuestro asesor *Christian López* se pondrá en contacto contigo a la brevedad "
            "para darte todos los detalles y continuar el proceso. 🙌"
        )
        formatted = (
            "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS LEY 73\n"
            "─────────────────────────────\n"
            f"Nombre: {datos.get('nombre', 'ND')}\n"
            f"WhatsApp: {user_id}\n"
            f"Teléfono: {datos.get('telefono_contacto', 'ND')}\n"
            f"Ciudad: {datos.get('ciudad', 'ND')}\n"
            f"Pensión mensual: ${datos.get('pension_mensual', 0):,.0f}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado', 0):,.0f}\n"
            f"Pensión en Inbursa: {datos.get('nomina_inbursa', 'ND')}\n"
            f"Origen: {datos.get('origen', 'directo')}\n"
            "─────────────────────────────"
        )
        notify_advisor(formatted)
        send_main_menu(user_id)
        _reset_user_session(user_id)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------
# EMBUDO – CRÉDITO EMPRESARIAL (Opción 5)
# ---------------------------------------------------------------
def funnel_credito_empresarial(user_id: str, user_message: str):
    state = user_state.get(user_id, "emp_beneficios")
    datos = user_data.get(user_id, {})

    if state == "emp_beneficios":
        send_message(
            user_id,
            "🏢 *Crédito Empresarial Inbursa*\n"
            "- Financiamiento desde $100,000 hasta $100,000,000\n"
            "- Tasas preferenciales y plazos flexibles\n"
            "- Sin aval con buen historial\n"
            "- Apoyo a PYMES, comercios y empresas consolidadas\n\n"
            "¿Eres empresario o representas una empresa?"
        )
        user_state[user_id] = "emp_confirmacion"
        return jsonify({"status": "ok", "funnel": "empresarial"})

    if state == "emp_confirmacion":
        resp = interpret_response(user_message)
        lowered = (user_message or "").lower()
        if resp == "positive" or any(k in lowered for k in ["empresario", "empresa", "negocio", "pyme", "comercio"]):
            send_message(user_id, "¿A qué *se dedica* tu empresa?")
            user_state[user_id] = "emp_actividad"
            return jsonify({"status": "ok"})
        if resp == "negative":
            send_main_menu(user_id)
            _reset_user_session(user_id)
            return jsonify({"status": "ok"})
        send_message(user_id, "Responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    if state == "emp_actividad":
        datos["actividad_empresa"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Qué *monto* deseas solicitar? (mínimo $100,000)")
        user_state[user_id] = "emp_monto"
        return jsonify({"status": "ok"})

    if state == "emp_monto":
        monto_solicitado = extract_number(user_message)
        if monto_solicitado is None or monto_solicitado < 100000:
            send_message(user_id, "Indica el monto (mínimo $100,000), ej. 250000.")
            return jsonify({"status": "ok"})
        datos["monto_solicitado"] = monto_solicitado
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *nombre completo*?")
        user_state[user_id] = "emp_nombre"
        return jsonify({"status": "ok"})

    if state == "emp_nombre":
        datos["nombre"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *número telefónico*?")
        user_state[user_id] = "emp_telefono"
        return jsonify({"status": "ok"})

    if state == "emp_telefono":
        datos["telefono"] = user_message.strip()
        user_data[user_id] = datos
        send_message(user_id, "¿En qué *ciudad* está ubicada tu empresa?")
        user_state[user_id] = "emp_ciudad"
        return jsonify({"status": "ok"})

    if state == "emp_ciudad":
        datos["ciudad"] = user_message.title()
        user_data[user_id] = datos
        send_message(
            user_id,
            "✅ Gracias por la información. Un asesor financiero (Christian López) "
            "se pondrá en contacto contigo en breve para continuar con tu solicitud."
        )
        formatted = (
            "🔔 NUEVO PROSPECTO – CRÉDITO EMPRESARIAL\n"
            f"Nombre: {datos.get('nombre', 'ND')}\n"
            f"Teléfono: {datos.get('telefono', 'ND')}\n"
            f"Ciudad: {datos.get('ciudad', 'ND')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado', 0):,.0f}\n"
            f"Actividad: {datos.get('actividad_empresa', 'ND')}\n"
            f"WhatsApp: {user_id}"
        )
        notify_advisor(formatted)
        send_main_menu(user_id)
        _reset_user_session(user_id)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------
# EMBUDO – FINANCIAMIENTO PRÁCTICO EMPRESARIAL (Opción 6)
# ---------------------------------------------------------------
def funnel_financiamiento_practico(user_id: str, user_message: str):
    state = user_state.get(user_id, "fp_intro")
    datos = user_data.get(user_id, {})

    if state == "fp_intro":
        send_message(
            user_id,
            "💼 *Financiamiento Práctico Empresarial – Inbursa*\n\n"
            "⏱️ *Aprobación desde 24 horas*\n"
            "💰 *Crédito simple sin garantía* desde $100,000 MXN\n"
            "🏢 Para empresas y *personas físicas con actividad empresarial*.\n\n"
            "¿Deseas conocer si puedes acceder a este financiamiento? (Sí/No)"
        )
        user_state[user_id] = "fp_confirmar_interes"
        return jsonify({"status": "ok", "funnel": "financiamiento_practico"})

    if state == "fp_confirmar_interes":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_message(
                user_id,
                "Perfecto 👍. Un ejecutivo te contactará para conocer tus necesidades y "
                "ofrecerte otras opciones."
            )
            notify_advisor(
                f"📩 Prospecto NO interesado en Financiamiento Práctico\nNúmero: {user_id}"
            )
            send_main_menu(user_id)
            _reset_user_session(user_id)
            return jsonify({"status": "ok"})
        if resp == "positive":
            send_message(
                user_id,
                "Excelente 🙌. Comencemos con un *perfilamiento* rápido.\n"
                "1️⃣ ¿Cuál es el *giro de la empresa*?"
            )
            user_state[user_id] = "fp_q1_giro"
            return jsonify({"status": "ok"})
        send_message(user_id, "Responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    preguntas = {
        "fp_q1_giro":        "2️⃣ ¿Qué *antigüedad fiscal* tiene la empresa?",
        "fp_q2_antiguedad":  "3️⃣ ¿Es *persona física con actividad empresarial* o *persona moral*?",
        "fp_q3_tipo":        "4️⃣ ¿Qué *edad tiene el representante legal*?",
        "fp_q4_edad":        "5️⃣ ¿Buró de crédito empresa y accionistas al día? (Responde *positivo* o *negativo*).",
        "fp_q5_buro":        "6️⃣ ¿Aproximadamente *cuánto factura al año* la empresa?",
        "fp_q6_facturacion": "7️⃣ ¿Tiene *facturación constante* en los últimos seis meses? (Sí/No)",
        "fp_q7_constancia":  "8️⃣ ¿Cuánto es el *monto de financiamiento* que requiere?",
        "fp_q8_monto":       "9️⃣ ¿Cuenta con la *opinión de cumplimiento positiva* ante el SAT?",
        "fp_q9_opinion":     "🔟 ¿Qué *tipo de financiamiento* requiere?",
        "fp_q10_tipo":       "1️⃣1️⃣ ¿Cuenta con financiamiento actualmente? ¿Con quién?",
    }

    orden = [
        "fp_q1_giro", "fp_q2_antiguedad", "fp_q3_tipo", "fp_q4_edad", "fp_q5_buro",
        "fp_q6_facturacion", "fp_q7_constancia", "fp_q8_monto", "fp_q9_opinion",
        "fp_q10_tipo", "fp_q11_actual", "fp_comentario",
    ]

    if state in orden[:-1]:
        datos[state] = user_message
        user_data[user_id] = datos
        next_index = orden.index(state) + 1
        next_state = orden[next_index]
        user_state[user_id] = next_state

        if next_state == "fp_comentario":
            send_message(user_id, "📝 ¿Deseas dejar *algún comentario adicional* para el asesor?")
        else:
            send_message(user_id, preguntas.get(state, "Por favor continúa con la siguiente información."))
        return jsonify({"status": "ok"})

    if state == "fp_comentario":
        datos["comentario"] = user_message
        formatted = (
            "🔔 *NUEVO PROSPECTO – FINANCIAMIENTO PRÁCTICO EMPRESARIAL*\n\n"
            f"📱 WhatsApp: {user_id}\n"
            f"🏢 Giro: {datos.get('fp_q1_giro', 'ND')}\n"
            f"📆 Antigüedad Fiscal: {datos.get('fp_q2_antiguedad', 'ND')}\n"
            f"👤 Tipo de Persona: {datos.get('fp_q3_tipo', 'ND')}\n"
            f"🧑‍⚖️ Edad Rep. Legal: {datos.get('fp_q4_edad', 'ND')}\n"
            f"📊 Buró empresa/accionistas: {datos.get('fp_q5_buro', 'ND')}\n"
            f"💵 Facturación anual: {datos.get('fp_q6_facturacion', 'ND')}\n"
            f"📈 6 meses constantes: {datos.get('fp_q7_constancia', 'ND')}\n"
            f"🎯 Monto requerido: {datos.get('fp_q8_monto', 'ND')}\n"
            f"🧾 Opinión SAT: {datos.get('fp_q9_opinion', 'ND')}\n"
            f"🏦 Tipo de financiamiento: {datos.get('fp_q10_tipo', 'ND')}\n"
            f"💼 Financiamiento actual: {datos.get('fp_q11_actual', 'ND')}\n"
            f"💬 Comentario: {datos.get('comentario', 'Ninguno')}"
        )
        notify_advisor(formatted)
        send_message(
            user_id,
            "✅ Gracias por la información. Un asesor financiero (Christian López) "
            "se pondrá en contacto contigo en breve para continuar con tu solicitud."
        )
        send_main_menu(user_id)
        _reset_user_session(user_id)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------
# Verificación de firma Meta (HMAC-SHA256)
# ---------------------------------------------------------------
def _verify_meta_signature(raw_body: bytes, sig_header: str) -> bool:
    """
    Valida X-Hub-Signature-256 de Meta.
    Si META_APP_SECRET no está configurado, pasa en modo degradado (no bloquea).
    """
    if not META_APP_SECRET:
        return True
    if not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ---------------------------------------------------------------
# Procesamiento de un mensaje individual
# ---------------------------------------------------------------
def _handle_message(message: dict) -> None:
    """
    Procesa un mensaje individual del webhook.
    Registra el mensaje entrante en Sheets antes de procesarlo.

    Orden de decisión:
    1. Validar tipo de mensaje.
    2. Registrar en Sheets.
    3. Comando sgpt: → GPT libre.
    4. Embudo activo → continuar embudo.
    4.5. Detección de lead de campaña IMSS → flujo IMSS directo [PRIORIDAD MÁXIMA].
         Se evalúa ANTES que los triggers de menú para capturar leads de anuncios
         FB/IG click-to-WhatsApp con mensajes genéricos o de bajo contexto.
    5. Saludo / solicitud explícita de menú → mostrar menú.
    5b. Solicitud directa de asesor → notificar.
    6. Intención numérica incrustada en texto → enrutar.
    7. Coincidencia exacta con mapa de opciones → enrutar.
    8. Capa híbrida → detect_semantic_intent → GPT → aclaración → menú.
    """
    try:
        phone_number = message.get("from")
        if not phone_number:
            log.warning("⚠️ Mensaje sin número de teléfono — ignorado")
            return

        # Idempotencia [M-1]
        msg_id = message.get("id", "")
        if msg_id:
            with _idempotency_lock:
                if msg_id in _processed_ids:
                    log.info(f"⚠️ Mensaje duplicado ignorado: {msg_id}")
                    return
                if len(_processed_deque) >= 3000:
                    oldest = _processed_deque[0]
                    _processed_ids.discard(oldest)
                _processed_deque.append(msg_id)
                _processed_ids.add(msg_id)

        _tl.msg_id = msg_id

        mtype = message.get("type")
        if mtype != "text":
            try:
                _sheets_append_row(
                    phone      = phone_number,
                    nombre     = _get_nombre(phone_number),
                    mensaje    = f"[{mtype}]",
                    tipo       = "entrante",
                    origen     = "cliente",
                    message_id = msg_id,
                )
            except Exception:
                log.exception("❌ Error registrando multimedia entrante en Sheets")
            send_message(phone_number, "Por ahora solo puedo procesar mensajes de texto 📩", msg_id)
            return

        user_message = (message.get("text") or {}).get("body", "").strip()
        user_message = user_message[:500]

        if not user_message:
            return

        log.info(f"📱 {phone_number}: {user_message}")

        # Registrar mensaje ENTRANTE en Sheets
        try:
            _sheets_append_row(
                phone      = phone_number,
                nombre     = _get_nombre(phone_number),
                mensaje    = user_message,
                tipo       = "entrante",
                origen     = "cliente",
                message_id = msg_id,
            )
        except Exception:
            log.exception("❌ Error registrando mensaje entrante en Sheets")

        # --- 3. Comando GPT directo ---
        if is_gpt_command(user_message):
            prompt = user_message.split(":", 1)[1].strip() if ":" in user_message else ""
            if not prompt:
                send_message(phone_number, "Ejemplo: sgpt: ¿Qué ventajas tiene el crédito IMSS?", msg_id)
                return
            gpt_reply = ask_gpt(prompt)
            send_message(phone_number, gpt_reply, msg_id)
            return

        # --- 4. Continuar embudo activo ---
        state = user_state.get(phone_number, "")
        if state.startswith("imss_"):
            funnel_prestamo_imss(phone_number, user_message)
            return
        if state.startswith("emp_"):
            funnel_credito_empresarial(phone_number, user_message)
            return
        if state.startswith("fp_"):
            funnel_financiamiento_practico(phone_number, user_message)
            return

        # --- 4.5. Detección de lead de campaña IMSS — PRIORIDAD MÁXIMA ---
        # Se ejecuta ANTES de los triggers de menú para interceptar leads de
        # anuncios FB/IG con mensajes genéricos y enrutarlos directamente al
        # embudo IMSS. Sin esta verificación, mensajes como "Hello!", "Info"
        # o "Quiero información" caerían en el menú general y romperían la conversión.
        _msg_norm = normalize_text(user_message)
        if _is_imss_campaign_lead(message, _msg_norm):
            log.info(f"📍 {phone_number}: route=imss_campaign_lead")
            _last_route[phone_number] = "imss_campaign"
            _last_service[phone_number] = "imss"
            user_data.setdefault(phone_number, {})
            # Registrar origen para la notificación al asesor
            referral = message.get("referral") or {}
            origen_tag = "campaña_IMSS"
            if referral:
                ad_headline = referral.get("headline", "")
                ad_id       = referral.get("source_id", "")
                if ad_headline or ad_id:
                    origen_tag = f"campaña_IMSS | anuncio: {ad_headline or ad_id}"
            user_data[phone_number]["origen"] = origen_tag
            user_state[phone_number] = "imss_campaign_open"
            funnel_prestamo_imss(phone_number, user_message)
            return

        # --- 5. Saludos y solicitudes explícitas de menú ---
        _MENU_CONTAINMENT = {
            "que manejas", "que ofrecen", "que ofreces",
            "que servicios tienen", "que servicios ofrecen",
            "quiero ver opciones", "ver el menu", "ver el menú",
            "ver los servicios", "mostrar opciones",
        }
        _is_menu_trigger = (
            _msg_norm in _MENU_TRIGGER_PHRASES
            or any(phrase in _msg_norm for phrase in _MENU_CONTAINMENT)
        )
        if _is_menu_trigger:
            _reset_user_session(phone_number)
            _last_route[phone_number] = "greeting"
            log.info(f"📍 {phone_number}: route=greeting")
            send_main_menu(phone_number)
            return

        # --- 5b. Solicitud explícita de hablar con asesor ---
        _ADVISOR_TRIGGERS = {
            "hablar con un asesor", "hablar con alguien",
            "contactar asesor", "contactar con asesor",
            "que me contacten", "que me llamen", "me pueden llamar",
            "llamame", "quiero que me llamen",
            "hablar con un ejecutivo", "comunicame con alguien",
        }
        if any(t in _msg_norm for t in _ADVISOR_TRIGGERS):
            log.info(f"📍 {phone_number}: route=advisor")
            _last_route[phone_number] = "advisor"
            send_message(
                phone_number,
                "¡Claro! 📞 Voy a avisarle a nuestro asesor *Christian López* para que "
                "se comunique contigo a la brevedad.\n\n"
                "¿Hay algo específico en lo que te pueda orientar mientras tanto?",
                msg_id,
            )
            notify_advisor(
                f"📣 SOLICITUD DE CONTACTO DIRECTO\n"
                f"Cliente pide hablar con asesor.\n"
                f"WhatsApp: {phone_number}\n"
                f"Mensaje: {user_message}",
                msg_id,
            )
            return

        # --- 6. Intención numérica incrustada en texto ---
        _NUM_CONTEXT = re.search(
            r'\b(?:opcion|opcion|numero|numero|servicio|'
            r'quiero el|quiero la|me interesa el|me interesa la|'
            r'me interesa|elige|elijo|selecciono|dame el|dame la|'
            r'escojo el|escojo la|quiero el numero|quiero la opcion)\s*([1-6])\b'
            r'|\b([1-6])\s*(?:por favor|porfavor|gracias|pls)\b',
            normalize_text(user_message),
        )
        if _NUM_CONTEXT:
            _digit = _NUM_CONTEXT.group(1) or _NUM_CONTEXT.group(2)
            _embedded_option = detect_exact_option(_digit)
            if _embedded_option:
                log.info(f"📍 {phone_number}: route=exact (número incrustado) → {_embedded_option}")
                _last_route[phone_number] = "exact"
                _route_to_service(phone_number, _embedded_option, msg_id)
                return

        # --- 7. Coincidencia exacta → centralizado en _route_to_service ---
        exact_option = detect_exact_option(user_message)
        if exact_option:
            log.info(f"📍 {phone_number}: route=exact → {exact_option}")
            _last_route[phone_number] = "exact"
            _route_to_service(phone_number, exact_option, msg_id)
            return

        # --- 8. Capa híbrida (semántica → GPT → aclaración → menú) ---
        handle_hybrid_message(phone_number, user_message, msg_id)

    except Exception:
        log.exception(f"❌ Error procesando mensaje de {message.get('from', '?')}")
    finally:
        _tl.msg_id = ""


# ---------------------------------------------------------------
# RUTAS FLASK
# ---------------------------------------------------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "status": "online",
        "service": "Vicky Bot Inbursa",
        "sheets_ready": _sheets_ready,
        "timestamp": _now_mx(),
    }), 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
            return challenge, 200
        return "forbidden", 403

    # POST
    try:
        raw_body   = request.get_data()
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_meta_signature(raw_body, sig_header):
            log.warning("⚠️ Firma Meta inválida — rechazando webhook")
            return jsonify({"status": "forbidden"}), 403

        data = request.get_json(force=True, silent=True) or {}

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value    = change.get("value") or {}
                messages = value.get("messages") or []
                for message in messages:
                    _handle_message(message)

        return jsonify({"status": "ok"}), 200

    except Exception:
        log.exception("❌ Error en webhook POST")
        # Siempre 200 para evitar reintentos infinitos de Meta
        return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "Vicky Bot Inbursa",
        "sheets_ready": _sheets_ready,
    }), 200


# ---------------------------------------------------------------
# ARRANQUE
# ---------------------------------------------------------------
_sheets_init()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
