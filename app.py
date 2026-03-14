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
# Usamos pytz si está disponible; si no, fallback a UTC-6 fijo.
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
# Librerías Google — importación condicional (modo degradado si no están instaladas)
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

META_TOKEN        = os.getenv("META_TOKEN")
WABA_PHONE_ID     = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER    = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
META_APP_SECRET   = os.getenv("META_APP_SECRET", "").strip()

# Google Sheets — bitácora de conversaciones
GOOGLE_CREDENTIALS_JSON      = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEETS_ID_CONVERSACIONES     = os.getenv("SHEETS_ID_CONVERSACIONES", "").strip()
SHEETS_TAB_CONVERSACIONES    = os.getenv("SHEETS_TAB_CONVERSACIONES", "Conversaciones").strip()

# ---------------------------------------------------------------
# Cliente OpenAI — SDK >= 1.0 requerido (agrega openai>=1.0.0 a requirements.txt).
# El fallback legacy se eliminó: openai.OpenAI() es incompatible con el módulo
# legacy y mezclarlo produce errores silenciosos en chat.completions.create().
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
_sheets_service  = None   # cliente global reutilizable
_sheets_ready    = False  # True si la conexión fue exitosa al arrancar

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Cabeceras esperadas en la hoja (en este orden exacto)
_SHEET_HEADERS = [
    "Phone", "Nombre", "Mensaje", "Fecha", "Tipo",
    "Origen", "Servicio", "EstadoEmbudo",
    "ResultadoEnvio", "DetalleError", "MessageID",
]


def _sheets_init() -> bool:
    """
    Inicializa el cliente de Google Sheets API desde la variable de entorno
    GOOGLE_CREDENTIALS_JSON. Retorna True si fue exitoso.
    Llama una sola vez al arrancar; el cliente se reutiliza.
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
        _sheets_ensure_headers()   # garantiza que la fila 1 tenga los encabezados correctos
        return True
    except json.JSONDecodeError:
        log.exception("❌ GOOGLE_CREDENTIALS_JSON no es JSON válido.")
    except Exception:
        log.exception("❌ Error inicializando Google Sheets API.")
    return False


def _sheets_ensure_headers() -> None:
    """
    Verifica que la fila 1 del tab contenga los encabezados correctos.
    Si está vacía, los escribe. Si tiene contenido distinto, solo registra advertencia
    (no sobreescribe para proteger datos existentes).
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
            # Fila 1 vacía — escribir encabezados
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
# desde los embudos sin necesidad de cambiar cada firma.
_tl = threading.local()


def _active_msg_id() -> str:
    """Retorna el message_id activo en el hilo actual (vacío si no hay ninguno)."""
    return getattr(_tl, "msg_id", "")



_last_service: dict = {}


def _detect_service(phone: str) -> str:
    """
    Infiere el servicio del usuario a partir de su estado en el embudo.
    Si no hay embudo activo, usa el último servicio registrado.
    Cubre todos los servicios: imss, empresarial, financiamiento_practico,
    seguro_auto, seguro_vida, vrim.
    """
    state = user_state.get(phone, "")
    if state.startswith("imss_"):
        return "imss"
    if state.startswith("emp_"):
        return "empresarial"
    if state.startswith("fp_"):
        return "financiamiento_practico"
    # Sin estado de embudo activo: usar último servicio registrado
    return _last_service.get(phone, "desconocido")


def _sheets_append_row(
    phone: str,
    nombre: str,
    mensaje: str,
    tipo: str,          # "entrante" | "saliente"
    origen: str,        # "cliente" | "bot" | "asesor"
    servicio: str = "",
    resultado: str = "",   # "ok" | "error" | ""
    detalle_error: str = "",
    message_id: str = "",
) -> None:
    """
    Appends una fila de bitácora en la hoja de Google Sheets.
    Si Sheets no está disponible o falla, solo registra en log — el bot no se detiene.

    Columnas (en orden): Phone, Nombre, Mensaje, Fecha, Tipo, Origen,
                         Servicio, EstadoEmbudo, ResultadoEnvio, DetalleError, MessageID
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
# Modificado para registrar en Sheets cada intento (éxito o error)
# ---------------------------------------------------------------
def send_message(to: str, text: str, _message_id: str = "") -> bool:
    """
    Envía un mensaje de texto por WhatsApp Cloud API.
    Registra el intento en Google Sheets (saliente / bot).
    _message_id: id del mensaje entrante que disparó este envío (opcional).
                 Si no se pasa, se usa el msg_id activo del hilo actual.
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
        # Registrar en Sheets — no detiene el bot si falla
        try:
            _sheets_append_row(
                phone       = str(to),
                nombre      = _get_nombre(str(to)),
                mensaje     = text,
                tipo        = "saliente",
                origen      = "bot",
                resultado   = resultado,
                detalle_error = detalle_err,
                message_id  = _message_id,
            )
        except Exception:
            log.exception("❌ Error registrando send_message en Sheets")

    return success


# send_whatsapp_message eliminado — era alias redundante de send_message.


# ---------------------------------------------------------------
# NOTIFICAR AL ASESOR
# Modificado para registrar en Sheets con Origen = "asesor"
# ---------------------------------------------------------------
def notify_advisor(message: str, _message_id: str = "") -> bool:
    """
    Envía una notificación al asesor.
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
            log.info("✅ Notificación enviada al asesor")
            resultado = "ok"
            success   = True
        else:
            log.error(f"❌ Falló notificación al asesor {resp.status_code}: {resp.text}")
            resultado   = "error"
            detalle_err = f"HTTP {resp.status_code}: {resp.text[:200]}"

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
# Tokens positivos y negativos evaluados por palabra completa.
# Las frases de 2+ palabras se verifican como substring del texto normalizado.
# Se evitan falsos positivos por substring parcial
# (ej. "si" dentro de "simple", "not" dentro de "notificar").
_RESP_POSITIVE_WORDS   = {"si", "sip", "claro", "ok", "vale", "afirmativo",
                           "yes", "correcto", "exacto", "andale", "dale"}
_RESP_POSITIVE_PHRASES = {"por supuesto", "desde luego", "claro que si",
                           "claro que sí", "con gusto"}
# Palabras sueltas que hacen la respuesta negativa con certeza.
_RESP_STRONG_NEG_WORDS = {"no", "nel", "nop", "negativo", "tampoco",
                           "nunca", "jamas"}


def interpret_response(text: str) -> str:
    """
    Interpreta si el texto es una respuesta positiva, negativa o neutral.

    Reglas (en orden de prioridad):
    1. Negativa fuerte por palabra completa → "negative" (incluye "ok no", "sí pero no").
    2. Positiva por palabra completa o frase → "positive".
    3. Neutral en cualquier otro caso.
    """
    if not text:
        return "neutral"

    norm = normalize_text(text)
    tokens = set(norm.split())

    # Negativa fuerte: cualquier token negativo gana sobre positivos
    if tokens & _RESP_STRONG_NEG_WORDS:
        return "negative"

    # Positiva: token positivo suelto
    if tokens & _RESP_POSITIVE_WORDS:
        return "positive"

    # Positiva: frase multi-palabra contenida en el texto normalizado
    if any(phrase in norm for phrase in _RESP_POSITIVE_PHRASES):
        return "positive"

    return "neutral"


def extract_number(text: str):
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
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n"
        "6️⃣ Financiamiento Práctico Empresarial (desde 24 hrs)\n\n"
        "Escribe el número o el nombre del servicio que te interesa."
    )
    send_message(phone, menu)


# ---------------------------------------------------------------
# GPT — helpers
# ---------------------------------------------------------------
def ask_gpt(prompt: str, model: str = "gpt-4o-mini", temperature: float = 0.3) -> str:
    """
    Llama a OpenAI usando el cliente instanciado explícitamente (_openai_client).
    Funciona con SDK >= 1.0. Temperature 0.3 — preciso para contexto financiero.
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
    Acepta un system_prompt explícito; si no se pasa, usa el de Vicky Bot.
    max_tokens reducido a 250 para respuestas breves y comerciales.
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
    minúsculas, sin tildes (unicodedata NFD), sin signos de puntuación,
    espacios simples.
    """
    if not text:
        return ""
    import unicodedata
    # NFD descompone caracteres acentuados en base + diacrítico
    t = unicodedata.normalize("NFD", text.lower().strip())
    # Eliminar diacríticos (Mn = Mark, Nonspacing)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # Reemplazar ñ explícitamente (NFD no la descompone)
    t = t.replace("ñ", "n")
    # Eliminar puntuación, dejar alfanuméricos y espacios
    t = re.sub(r"[^\w\s]", " ", t)
    # Colapsar espacios múltiples
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Mapa de opciones exactas del menú — fuente única de verdad.
# TODAS las keys están pre-normalizadas (sin tildes, minúsculas) para que
# detect_exact_option solo necesite normalizar el input del usuario.
_MENU_OPTIONS: dict = {
    # --- Opción 1: Préstamo IMSS Pensionados ---
    # Solo frases que identifican inequívocamente el producto.
    # "prestamo" solo → ambiguo (puede ser empresarial) → va a semántica/GPT.
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
    # "auto" / "vehiculo" / "carro" solos → ambiguos → van a semántica/GPT.
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
    # "vida" sola → demasiado ambigua → va a semántica/GPT.
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
    # "empresa" / "negocio" solos → ambiguos → van a semántica/GPT.
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

# Frases que deben mostrar el menú principal sin pasar por la capa híbrida.
# Se normalizan en runtime con normalize_text().
_MENU_TRIGGER_PHRASES: set = {
    # Saludos y arranques de conversación → menú inmediato
    "menu", "memu", "inicio", "start",
    "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches",
    # Solicitudes explícitas de catálogo / opciones
    "servicios", "opciones", "catalogo", "productos",
    "que manejas", "que ofrecen", "que ofreces", "que tienen", "que tienes",
    "que servicios tienen", "que servicios ofrecen", "que servicios manejan",
    "quiero informacion", "quiero info", "quiero ver opciones",
    "en que me puedes ayudar", "para que sirves",
    # NOTA: "como funciona", "info", "informacion", "ayuda" se movieron al
    # híbrido/GPT porque pueden ser consultas sobre un servicio específico.
}

# Palabras clave para detección semántica.
# Estructura: { servicio: { "strong": [...frases 2+palabras...], "weak": [...palabras sueltas...] } }
# Puntaje: frase fuerte = 2pts | palabra débil = 1pt | umbral mínimo = 2pts para activar.
# Si hay empate entre servicios → None (va a GPT).
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
        "weak": [],  # "auto" y "carro" solos son demasiado ambiguos
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
        "weak": [],  # "vida" sola es muy ambigua
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
        # Intencionalmente más genérico — gana cuando hay señal empresarial SIN urgencia/rapidez
        "strong": [
            "credito empresa", "credito empresarial", "prestamo empresa",
            "prestamo empresarial", "financiamiento empresa",
            "capital para negocio", "credito para negocio", "prestamo negocio",
            "financiar empresa", "capital de trabajo", "inversion empresa",
            "financiar mi negocio", "credito pyme", "mi empresa necesita",
            "necesito credito empresa", "necesito financiamiento empresa",
        ],
        "weak": [],  # "negocio", "empresa" solos son débiles; ver nota abajo
    },
    "financiamiento_practico": {
        # Requiere señal EXPLÍCITA de rapidez/liquidez/urgencia para diferenciarse del 5
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
    """
    Busca coincidencia exacta en el mapa de opciones del menú.
    Normaliza el input del usuario antes de buscar.
    Las keys de _MENU_OPTIONS ya están normalizadas (sin tildes, minúsculas).
    """
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

    Matching de keywords:
    - Substring exacto O todas las palabras del keyword aparecen como palabras
      individuales en el mensaje (maneja frases con palabras intermedias).

    Desambiguación empresarial (opción 5) vs financiamiento_practico (opción 6):
    - Regla comercial explícita (tiene prioridad sobre el score):
        si hay señal de rapidez/liquidez/urgencia/24h → opción 6
        si no hay esa señal pero sí empresa/negocio/capital → opción 5
    - NO se resuelve por empate: siempre devuelve uno de los dos.

    Para empates entre otros servicios distintos → None (va a GPT).
    """
    normalized = normalize_text(text)
    msg_words = set(normalized.split())
    scores: dict = {}

    def kw_matches(kw_norm: str) -> bool:
        # Coincidencia exacta como substring
        if kw_norm in normalized:
            return True
        # Coincidencia por conjunto de palabras (maneja palabras intermedias)
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

    # ── Pre-regla comercial 5 vs 6 ───────────────────────────────────────────
    # Se aplica ANTES del ranking para cubrir casos donde solo uno de los dos
    # servicios alcanzó umbral pero el mensaje tiene señal decisiva de urgencia.
    # Señales de urgencia → opción 6 (Financiamiento Práctico)
    # Contexto empresarial sin urgencia → opción 5 (Financiamiento Empresarial)
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
        # Si hay señal de negocio con urgencia → siempre opción 6
        if has_urgency:
            return "financiamiento_practico"
        # Si hay señal de negocio sin urgencia y el score de empresarial alcanzó umbral
        if scores.get("empresarial", 0) >= 2.0:
            return "empresarial"
        # Negocio + no urgencia pero solo financiamiento_practico en scores → igual opción 5
        if scores.get("financiamiento_practico", 0) >= 2.0 and "empresarial" not in scores:
            return "empresarial"

    # ── Ranking general ──────────────────────────────────────────────────────
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_service, best_score = ranked[0]

    if len(ranked) > 1:
        second_service, second_score = ranked[1]

        # Si los top 2 son 5 y 6, la pre-regla ya debería haberlo resuelto arriba.
        # Como seguridad: si llega aquí sin resolución previa, aplica urgencia.
        if set([best_service, second_service]) == {"empresarial", "financiamiento_practico"}:
            if has_urgency:
                return "financiamiento_practico"
            return "empresarial"

        # Empate real entre otros servicios distintos → None (va a GPT)
        if best_score == second_score:
            return None

    return best_service


# Registro de último modo de resolución por usuario (anti-repetición).
# Valores posibles: "exact" | "semantic" | "gpt" | "clarification" | "menu" | "greeting" | "advisor"
_last_route: dict = {}


def _reset_user_session(phone: str) -> None:
    """
    Limpia todo el estado conversacional del usuario.
    Centraliza el reseteo para evitar olvidar algún dict en el futuro.
    """
    user_state.pop(phone, None)
    user_data.pop(phone, None)
    _last_service.pop(phone, None)
    _last_route.pop(phone, None)


def _route_to_service(phone: str, service: str, msg_id: str) -> None:
    """
    Enruta al flujo de servicio correcto dado su nombre.
    Centraliza la lógica de activación para exact + semantic.
    """
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
            "🚗 *Seguros de Auto Inbursa*\n"
            "✅ Cobertura amplia\n✅ Asistencia vial 24/7\n✅ RC, robo total/parcial\n\n"
            "📞 Un asesor te contactará para cotizar.",
            msg_id,
        )
        notify_advisor(f"🚗 Interesado en Seguro Auto · WhatsApp: {phone}", msg_id)

    elif service == "seguro_vida":
        _last_service[phone] = "seguro_vida"
        send_message(
            phone,
            "🏥 *Seguros de Vida y Salud Inbursa*\n"
            "✅ Vida\n✅ Gastos médicos\n✅ Hospitalización\n✅ Atención 24/7\n\n"
            "📞 Un asesor te contactará para explicar coberturas.",
            msg_id,
        )
        notify_advisor(f"🏥 Interesado en Vida/Salud · WhatsApp: {phone}", msg_id)

    elif service == "vrim":
        _last_service[phone] = "vrim"
        send_message(
            phone,
            "💳 *Tarjetas Médicas VRIM*\n"
            "✅ Consultas ilimitadas\n✅ Especialistas y laboratorios\n✅ Descuentos en medicamentos\n\n"
            "📞 Un asesor te contactará para explicar beneficios.",
            msg_id,
        )
        notify_advisor(f"💳 Interesado en VRIM · WhatsApp: {phone}", msg_id)


def handle_hybrid_message(phone: str, text: str, msg_id: str) -> None:
    """
    Capa híbrida de interpretación natural.
    Se llama solo cuando no hubo coincidencia exacta con el menú.

    Orden de decisión:
    1. Semántica clara (score >= 2.0) → enrutar al servicio.
    2. Hint financiero real → GPT (con anti-repetición: si ya fue GPT → aclaración).
    3. Anti-repetición de menú/aclaración → aclaración breve.
    4. Último recurso → menú completo (una sola vez).

    _last_route válidos: "exact" | "semantic" | "gpt" | "clarification" | "menu" | "greeting" | "advisor"
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
    # Regla: GPT se activa si el mensaje contiene un término financiero específico
    # O si es una pregunta consultiva explícita sobre servicios/calificación/costo.
    # NO se activa solo por ser una pregunta genérica de 3+ tokens.
    normalized = normalize_text(text)
    tokens = set(normalized.split())

    # Términos que indican consulta real sobre los servicios del bot
    _FINANCIAL_HINTS = {
        "seguro", "prestamo", "credito", "financiamiento", "inbursa",
        "dinero", "beneficio", "tarjeta", "medico", "medica",
        "pension", "jubilado", "pensionado",
        "cotizar", "precio", "costo", "aplico", "califico",
        "requisito", "documentos", "aplica",
    }
    # Frases consultivas de servicio (requieren estar completas en el texto)
    _CONSULTIVE_PHRASES = {
        "como funciona", "cuanto cuesta", "que necesito", "que cubre",
        "si califico", "que documentos", "que requisitos", "cuanto es",
        "como aplico", "en que consiste", "que incluye", "que es el",
        "que es la", "explicame", "informame", "como accedo",
    }

    has_financial_hint  = bool(tokens & _FINANCIAL_HINTS)
    has_consultive      = any(p in normalized for p in _CONSULTIVE_PHRASES)

    # NO activar GPT por empresa/negocio solos porque esos ya los maneja semántica
    # y si llegan aquí es porque no hubo coincidencia suficiente.
    should_use_gpt = has_financial_hint or has_consultive

    if should_use_gpt:
        # Anti-repetición: GPT ya respondió → aclaración en lugar de GPT de nuevo
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

    if state == "imss_beneficios":
        send_message(
            user_id,
            "💰 *Préstamo para Pensionados IMSS (Ley 73)*\n"
            "- Montos desde $40,000 hasta $650,000\n"
            "- Descuento vía pensión\n"
            "- Plazos de 12 a 60 meses\n"
            "- Depósito directo a tu cuenta\n"
            "- Sin aval ni garantía\n\n"
            "🏦 *Beneficios adicionales si recibes tu pensión en Inbursa*\n"
            "- Tasas preferenciales\n"
            "- Acceso a seguro de vida sin costo\n"
            "- Anticipo de nómina disponible\n"
            "- Atención personalizada 24/7\n\n"
            "(Los beneficios de nómina son *adicionales* y *no obligatorios*)."
        )
        send_message(user_id, "¿Eres pensionado o jubilado del IMSS bajo la Ley 73?")
        user_state[user_id] = "imss_preg_pensionado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    if state == "imss_preg_pensionado":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_main_menu(user_id)
            _reset_user_session(user_id)
            return jsonify({"status": "ok"})
        if resp == "positive":
            send_message(user_id, "¿Cuánto recibes aproximadamente al mes por concepto de pensión?")
            user_state[user_id] = "imss_preg_monto_pension"
            return jsonify({"status": "ok"})
        send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    if state == "imss_preg_monto_pension":
        monto = extract_number(user_message)
        if monto is None:
            send_message(user_id, "Indica el monto mensual que recibes por pensión (ej. 6500).")
            return jsonify({"status": "ok"})
        datos["pension_mensual"] = monto
        user_data[user_id] = datos
        if monto < 5000:
            send_message(
                user_id,
                "Por ahora los créditos aplican a pensiones a partir de $5,000.\n"
                "Puedo notificar a nuestro asesor para ofrecerte otra opción. ¿Deseas que lo haga?"
            )
            user_state[user_id] = "imss_ofrecer_asesor"
            return jsonify({"status": "ok"})
        send_message(user_id, "Perfecto 👏 ¿Qué monto de préstamo te gustaría solicitar (mínimo $40,000)?")
        user_state[user_id] = "imss_preg_monto_solicitado"
        return jsonify({"status": "ok"})

    if state == "imss_ofrecer_asesor":
        resp = interpret_response(user_message)
        if resp == "positive":
            formatted = (
                "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"WhatsApp: {user_id}\n"
                f"Pensión mensual: ${datos.get('pension_mensual','ND')}\n"
                "Estatus: Pensión baja, requiere opciones alternativas"
            )
            notify_advisor(formatted)
            send_message(user_id, "¡Listo! Un asesor te contactará con opciones alternativas.")
            send_main_menu(user_id)
            _reset_user_session(user_id)
            return jsonify({"status": "ok"})
        send_message(user_id, "Perfecto, si deseas podemos continuar con otros servicios.")
        send_main_menu(user_id)
        _reset_user_session(user_id)
        return jsonify({"status": "ok"})

    if state == "imss_preg_monto_solicitado":
        monto_sol = extract_number(user_message)
        if monto_sol is None or monto_sol < 40000:
            send_message(user_id, "Indica el monto que deseas solicitar (mínimo $40,000), ej. 65000.")
            return jsonify({"status": "ok"})
        datos["monto_solicitado"] = monto_sol
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *nombre completo*?")
        user_state[user_id] = "imss_preg_nombre"
        return jsonify({"status": "ok"})

    if state == "imss_preg_nombre":
        datos["nombre"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *teléfono de contacto*?")
        user_state[user_id] = "imss_preg_telefono"
        return jsonify({"status": "ok"})

    if state == "imss_preg_telefono":
        datos["telefono_contacto"] = user_message.strip()
        user_data[user_id] = datos
        send_message(user_id, "¿En qué *ciudad* vives?")
        user_state[user_id] = "imss_preg_ciudad"
        return jsonify({"status": "ok"})

    if state == "imss_preg_ciudad":
        datos["ciudad"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Ya recibes tu pensión en *Inbursa*? (Sí/No)")
        user_state[user_id] = "imss_preg_nomina_inbursa"
        return jsonify({"status": "ok"})

    if state == "imss_preg_nomina_inbursa":
        resp = interpret_response(user_message)
        datos["nomina_inbursa"] = "Sí" if resp == "positive" else "No" if resp == "negative" else "ND"
        if resp not in ("positive", "negative"):
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok"})

        send_message(
            user_id,
            "✅ ¡Listo! Tu crédito ha sido *preautorizado*.\n"
            "Un asesor financiero (Christian López) se pondrá en contacto contigo."
        )
        formatted = (
            "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
            f"Nombre: {datos.get('nombre','ND')}\n"
            f"WhatsApp: {user_id}\n"
            f"Teléfono: {datos.get('telefono_contacto','ND')}\n"
            f"Ciudad: {datos.get('ciudad','ND')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado',0):,.0f}\n"
            f"Nómina Inbursa: {datos.get('nomina_inbursa','ND')}"
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
            f"Nombre: {datos.get('nombre','ND')}\n"
            f"Teléfono: {datos.get('telefono','ND')}\n"
            f"Ciudad: {datos.get('ciudad','ND')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado',0):,.0f}\n"
            f"Actividad: {datos.get('actividad_empresa','ND')}\n"
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
            notify_advisor(f"📩 Prospecto NO interesado en Financiamiento Práctico\nNúmero: {user_id}")
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

    # Preguntas secuenciales — el valor del estado actual es la respuesta anterior;
    # el dict `preguntas` mapea estado_actual → texto de la SIGUIENTE pregunta.
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
            f"🏢 Giro: {datos.get('fp_q1_giro','ND')}\n"
            f"📆 Antigüedad Fiscal: {datos.get('fp_q2_antiguedad','ND')}\n"
            f"👤 Tipo de Persona: {datos.get('fp_q3_tipo','ND')}\n"
            f"🧑‍⚖️ Edad Rep. Legal: {datos.get('fp_q4_edad','ND')}\n"
            f"📊 Buró empresa/accionistas: {datos.get('fp_q5_buro','ND')}\n"
            f"💵 Facturación anual: {datos.get('fp_q6_facturacion','ND')}\n"
            f"📈 6 meses constantes: {datos.get('fp_q7_constancia','ND')}\n"
            f"🎯 Monto requerido: {datos.get('fp_q8_monto','ND')}\n"
            f"🧾 Opinión SAT: {datos.get('fp_q9_opinion','ND')}\n"
            f"🏦 Tipo de financiamiento: {datos.get('fp_q10_tipo','ND')}\n"
            f"💼 Financiamiento actual: {datos.get('fp_q11_actual','ND')}\n"
            f"💬 Comentario: {datos.get('comentario','Ninguno')}"
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
    Separado para permitir iterar sobre múltiples mensajes por payload [A-2].
    Registra el mensaje entrante en Sheets antes de procesarlo.

    Orden de decisión:
    1. Validar tipo de mensaje.
    2. Registrar en Sheets.
    3. Comando sgpt: → responder con GPT libre.
    4. Embudo activo → continuar embudo.
    5. Saludo / menú explícito → mostrar menú.
    6. Coincidencia exacta con mapa de opciones → enrutar.
    7. Capa híbrida → detect_semantic_intent → GPT → aclaración → menú.
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

        # Propagar msg_id al hilo para que send_message/notify_advisor lo hereden
        _tl.msg_id = msg_id

        mtype = message.get("type")
        if mtype != "text":
            # Registrar evento multimedia entrante en Sheets antes de responder
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
        user_message = user_message[:500]   # [B-3] límite de longitud

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

        # --- 5. Saludos y solicitudes explícitas de menú ---
        # Exact match contra el set de trigger phrases (ya son frases completas seguras).
        # Contención solo para frases largas y claras, NO palabras cortas como
        # "info", "ayuda", "servicios", "hola" sueltas que dispararían menú en
        # cualquier mensaje que las contenga.
        _msg_norm = normalize_text(user_message)
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
        # Solo frases inequívocas de contacto humano directo.
        # EXCLUIDAS: "quiero hablar con" (puede ser "quiero hablar con alguien sobre pensión"),
        #            "como me comunico" (puede ser sobre un trámite, no sobre un asesor),
        #            "con quien hablo" (puede ser consulta de proceso).
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
        # Solo activar con contexto inequívoco de selección de menú.
        # EXCLUIDOS "el" y "la" porque son demasiado genéricos:
        #   "el 3 de marzo", "la 2 de la tarde", "el 5%" → falsos positivos.
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
        # Limpiar msg_id del hilo para evitar rastro si el worker se reutiliza
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
        # [C-3] Guardia explícita: VERIFY_TOKEN vacío nunca valida
        if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
            return challenge, 200
        return "forbidden", 403

    # POST
    try:
        # [A-3] Verificar firma HMAC-SHA256 de Meta
        raw_body   = request.get_data()
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_meta_signature(raw_body, sig_header):
            log.warning("⚠️ Firma Meta inválida — rechazando webhook")
            return jsonify({"status": "forbidden"}), 403

        data = request.get_json(force=True, silent=True) or {}

        # [A-2] Iterar sobre TODOS los entries/changes/messages
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value    = change.get("value") or {}
                messages = value.get("messages") or []
                for message in messages:
                    _handle_message(message)

        return jsonify({"status": "ok"}), 200

    except Exception:
        log.exception("❌ Error en webhook POST")
        # [C-2] Siempre 200 para evitar reintentos infinitos de Meta
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
# Inicializar Google Sheets al arrancar (antes de recibir requests)
_sheets_init()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
