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
# Cliente OpenAI — instanciación explícita compatible con SDK >= 1.0
# openai.api_key = ... es patrón SDK v0 y puede fallar en SDK moderno.
# Usamos openai.OpenAI(api_key=...) que funciona desde SDK 1.0 en adelante.
# ---------------------------------------------------------------
try:
    _openai_client = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if _openai_client:
        log.info("✅ Cliente OpenAI inicializado (SDK >= 1.0).")
    else:
        log.warning("⚠️ OPENAI_API_KEY no configurado. Comando sgpt: deshabilitado.")
except AttributeError:
    # Fallback para instalaciones muy antiguas del SDK (< 1.0) — improbable en Render actual
    log.warning("⚠️ SDK OpenAI antiguo detectado. Intentando modo legacy.")
    try:
        import openai as _openai_legacy
        _openai_legacy.api_key = OPENAI_API_KEY
        _openai_client = _openai_legacy
    except Exception:
        log.exception("❌ No se pudo inicializar OpenAI.")
        _openai_client = None

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
def interpret_response(text: str) -> str:
    t = (text or "").strip().lower()
    positive = ["sí", "si", "sip", "claro", "ok", "vale", "afirmativo", "yes", "correcto"]
    negative = ["no", "nop", "negativo", "para nada", "not", "nel"]
    if any(k in t for k in positive):
        return "positive"
    if any(k in t for k in negative):
        return "negative"
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
# GPT (opcional por comando "sgpt: ...")
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


def is_gpt_command(msg: str) -> bool:
    return (msg or "").strip().lower().startswith("sgpt:")


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
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            _last_service.pop(user_id, None)
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
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            _last_service.pop(user_id, None)
            return jsonify({"status": "ok"})
        send_message(user_id, "Perfecto, si deseas podemos continuar con otros servicios.")
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        _last_service.pop(user_id, None)
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
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        _last_service.pop(user_id, None)
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
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            _last_service.pop(user_id, None)
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
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        _last_service.pop(user_id, None)
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
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            _last_service.pop(user_id, None)
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
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        _last_service.pop(user_id, None)
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
        # automáticamente desde los embudos sin refactorizar cada llamada.
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

        # Comando GPT
        if is_gpt_command(user_message):
            prompt = user_message.split(":", 1)[1].strip() if ":" in user_message else ""
            if not prompt:
                send_message(phone_number, "Ejemplo: sgpt: ¿Qué ventajas tiene el crédito IMSS?", msg_id)
                return
            gpt_reply = ask_gpt(prompt)
            send_message(phone_number, gpt_reply, msg_id)
            return

        # Continuar embudo activo
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

        # Mapa de opciones del menú
        menu_options = {
            "1": "prestamo_imss", "imss": "prestamo_imss", "préstamo": "prestamo_imss",
            "prestamo": "prestamo_imss", "ley 73": "prestamo_imss",
            "pensión": "prestamo_imss", "pension": "prestamo_imss",
            "2": "seguro_auto", "auto": "seguro_auto", "seguros de auto": "seguro_auto",
            "3": "seguro_vida", "seguro vida": "seguro_vida", "seguros de vida": "seguro_vida",
            "seguro salud": "seguro_vida", "vida": "seguro_vida",
            "4": "vrim", "tarjetas médicas": "vrim", "tarjetas medicas": "vrim", "vrim": "vrim",
            "5": "empresarial", "financiamiento empresarial": "empresarial",
            "empresa": "empresarial", "negocio": "empresarial", "pyme": "empresarial",
            "crédito empresarial": "empresarial", "credito empresarial": "empresarial",
            "6": "financiamiento_practico", "financiamiento practico": "financiamiento_practico",
            "financiamiento práctico": "financiamiento_practico",
            "crédito simple": "financiamiento_practico", "credito simple": "financiamiento_practico",
        }

        option = menu_options.get(user_message.lower())

        if option == "prestamo_imss":
            _last_service[phone_number] = "imss"
            user_state[phone_number] = "imss_beneficios"
            user_data.setdefault(phone_number, {})
            funnel_prestamo_imss(phone_number, user_message)
            return

        if option == "empresarial":
            _last_service[phone_number] = "empresarial"
            user_state[phone_number] = "emp_beneficios"
            user_data.setdefault(phone_number, {})
            funnel_credito_empresarial(phone_number, user_message)
            return

        if option == "financiamiento_practico":
            _last_service[phone_number] = "financiamiento_practico"
            user_state[phone_number] = "fp_intro"
            user_data.setdefault(phone_number, {})
            funnel_financiamiento_practico(phone_number, user_message)
            return

        if user_message.lower() in ["menu", "menú", "hola", "buenas", "servicios", "opciones"]:
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            _last_service.pop(phone_number, None)
            send_main_menu(phone_number)
            return

        if option == "seguro_auto":
            _last_service[phone_number] = "seguro_auto"
            send_message(
                phone_number,
                "🚗 *Seguros de Auto Inbursa*\n"
                "✅ Cobertura amplia\n✅ Asistencia vial 24/7\n✅ RC, robo total/parcial\n\n"
                "📞 Un asesor te contactará para cotizar.",
                msg_id,
            )
            notify_advisor(f"🚗 Interesado en Seguro Auto · WhatsApp: {phone_number}", msg_id)
            return

        if option == "seguro_vida":
            _last_service[phone_number] = "seguro_vida"
            send_message(
                phone_number,
                "🏥 *Seguros de Vida y Salud Inbursa*\n"
                "✅ Vida\n✅ Gastos médicos\n✅ Hospitalización\n✅ Atención 24/7\n\n"
                "📞 Un asesor te contactará para explicar coberturas.",
                msg_id,
            )
            notify_advisor(f"🏥 Interesado en Vida/Salud · WhatsApp: {phone_number}", msg_id)
            return

        if option == "vrim":
            _last_service[phone_number] = "vrim"
            send_message(
                phone_number,
                "💳 *Tarjetas Médicas VRIM*\n"
                "✅ Consultas ilimitadas\n✅ Especialistas y laboratorios\n✅ Descuentos en medicamentos\n\n"
                "📞 Un asesor te contactará para explicar beneficios.",
                msg_id,
            )
            notify_advisor(f"💳 Interesado en VRIM · WhatsApp: {phone_number}", msg_id)
            return

        # Fallback
        send_main_menu(phone_number)

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
