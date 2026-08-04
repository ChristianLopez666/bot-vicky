import os
import json
import logging
import re
import hmac
import hashlib
import threading
import unicodedata
import uuid
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
import openai
from flask import Flask, request, jsonify
from dotenv import load_dotenv


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

try:
    import pytz
    _TZ = pytz.timezone("America/Mexico_City")
    def now_mx(): return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
except ImportError:
    _TZ = timezone(timedelta(hours=-6))
    def now_mx(): return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")

# ── Google Sheets (condicional) ───────────────────────────────────────────────
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    _glibs = True
except ImportError:
    _glibs = False
    log.warning("⚠️ google-api-python-client no instalado. Sheets deshabilitado.")

try:
    import redis
    _redis_libs = True
except ImportError:
    redis = None
    _redis_libs = False
    log.warning("⚠️ redis no instalado. Persistencia en memoria.")

# ── Variables de entorno ──────────────────────────────────────────────────────
load_dotenv()

META_TOKEN   = os.getenv("META_TOKEN")
WABA_ID      = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUM  = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_KEY   = os.getenv("OPENAI_API_KEY")
APP_SECRET   = os.getenv("META_APP_SECRET", "").strip()
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "").strip()


# Notificación al asesor fuera de ventana 24h:
# Crea un template aprobado en Meta Business Manager con un parámetro {{1}}.
# Configura: ADVISOR_TEMPLATE_NAME=nombre_del_template
ADV_TPL      = os.getenv("ADVISOR_TEMPLATE_NAME", "").strip()
ADV_TPL_LANG = os.getenv("ADVISOR_TEMPLATE_LANG", "es_MX").strip()

GG_CREDS  = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEET_ID  = os.getenv("SHEETS_ID_CONVERSACIONES", "").strip()
SHEET_TAB = os.getenv("SHEETS_TAB_CONVERSACIONES", "Conversaciones").strip()


STATE_TTL = 24 * 60 * 60

class StateStore:
    def __init__(self, ttl: int = STATE_TTL):
        self.ttl = ttl
        self._redis = None
        self._state_mem = {}
        self._data_mem = {}
        self._aux_mem = {}
        redis_url = (os.getenv("KV_URL", "").strip() or os.getenv("REDIS_URL", "").strip())
        if redis_url and _redis_libs:
            try:
                self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                log.info("✅ StateStore conectado a Redis/Valkey.")
            except Exception as e:
                self._redis = None
                log.warning("⚠️ Redis/Valkey no disponible. Usando memoria. err=%s", e)
        elif redis_url and not _redis_libs:
            log.warning("⚠️ KV_URL/REDIS_URL configurado pero redis no está instalado. Usando memoria.")
        else:
            log.warning("⚠️ KV_URL/REDIS_URL no configurado. Estado en memoria.")

    def _key(self, kind: str, phone: str) -> str:
        ph = re.sub(r"\D", "", str(phone))
        return f"vicky:{kind}:{ph}"

    def get_state(self, phone: str, default: str = "") -> str:
        if self._redis:
            key = self._key("state", phone)
            val = self._redis.get(key)
            if val is not None:
                self._redis.expire(key, self.ttl)
                return val
            return default
        return self._state_mem.get(str(phone), default)

    def set_state(self, phone: str, state: str) -> None:
        if self._redis:
            self._redis.setex(self._key("state", phone), self.ttl, state or "")
            return
        self._state_mem[str(phone)] = state or ""

    def pop_state(self, phone: str, default=None):
        if self._redis:
            key = self._key("state", phone)
            val = self._redis.get(key)
            self._redis.delete(key)
            return val if val is not None else default
        return self._state_mem.pop(str(phone), default)

    def get_data(self, phone: str, default=None):
        default = {} if default is None else default
        if self._redis:
            key = self._key("data", phone)
            raw = self._redis.get(key)
            if raw is None:
                return dict(default) if isinstance(default, dict) else default
            self._redis.expire(key, self.ttl)
            try:
                val = json.loads(raw)
                return val if isinstance(val, dict) else (dict(default) if isinstance(default, dict) else default)
            except Exception:
                return dict(default) if isinstance(default, dict) else default
        val = self._data_mem.get(str(phone))
        if val is None:
            return dict(default) if isinstance(default, dict) else default
        return val

    def set_data(self, phone: str, data: dict) -> None:
        data = data if isinstance(data, dict) else {}
        if self._redis:
            self._redis.setex(self._key("data", phone), self.ttl, json.dumps(data, ensure_ascii=False))
            return
        self._data_mem[str(phone)] = data

    def pop_data(self, phone: str, default=None):
        if self._redis:
            key = self._key("data", phone)
            raw = self._redis.get(key)
            self._redis.delete(key)
            if raw is None:
                return default
            try:
                return json.loads(raw)
            except Exception:
                return default
        return self._data_mem.pop(str(phone), default)

    # ── Almacen auxiliar con TTL propio ───────────────────────────────────────
    # Usado por la instrumentacion de alertas al asesor: ventana de 24h y
    # correlacion por wamid. Cada clave lleva su propio TTL, independiente de
    # STATE_TTL, y no comparte espacio con el estado de los funnels.
    #
    # Es best-effort por diseño: si Redis/Valkey no esta disponible o falla,
    # degrada a memoria del proceso y NUNCA propaga la excepcion. La entrega de
    # un mensaje jamas debe depender de que este almacen funcione.
    def aux_set(self, key: str, value: str, ttl: int) -> bool:
        try:
            if self._redis:
                self._redis.setex(f"vicky:{key}", max(int(ttl), 1), value)
                return True
            self._aux_prune()
            self._aux_mem[key] = (time.time() + ttl, value)
            return True
        except Exception:
            return False

    def aux_get(self, key: str):
        try:
            if self._redis:
                return self._redis.get(f"vicky:{key}")
            item = self._aux_mem.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= time.time():
                self._aux_mem.pop(key, None)
                return None
            return value
        except Exception:
            return None

    def _aux_prune(self) -> None:
        # Solo en modo memoria: acota el diccionario cuando Redis no esta
        # disponible, para que un proceso de larga vida no acumule claves
        # vencidas indefinidamente.
        if len(self._aux_mem) < 500:
            return
        now = time.time()
        for k in [k for k, (exp, _) in self._aux_mem.items() if exp <= now]:
            self._aux_mem.pop(k, None)

class _StateMap:
    def __init__(self, store: StateStore):
        self.store = store

    def get(self, key, default=None):
        return self.store.get_state(key, "" if default is None else default)

    def __getitem__(self, key):
        val = self.store.get_state(key, None)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key, value):
        self.store.set_state(key, value)

    def pop(self, key, default=None):
        return self.store.pop_state(key, default)

    def setdefault(self, key, default=None):
        cur = self.store.get_state(key, None)
        if cur is None:
            self.store.set_state(key, "" if default is None else default)
            return "" if default is None else default
        return cur

class _DataMap:
    def __init__(self, store: StateStore):
        self.store = store

    def get(self, key, default=None):
        return self.store.get_data(key, {} if default is None else default)

    def __getitem__(self, key):
        val = self.store.get_data(key, None)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key, value):
        self.store.set_data(key, value)

    def pop(self, key, default=None):
        return self.store.pop_data(key, default)

    def setdefault(self, key, default=None):
        cur = self.store.get_data(key, None)
        if cur is None:
            val = {} if default is None else default
            self.store.set_data(key, val)
            return val
        return cur



# ── OpenAI ────────────────────────────────────────────────────────────────────
_oai = None
if OPENAI_KEY:
    try:
        _oai = openai.OpenAI(api_key=OPENAI_KEY)
        log.info("✅ OpenAI inicializado.")
    except Exception:
        log.exception("❌ Error inicializando OpenAI.")
else:
    log.warning("⚠️ OPENAI_API_KEY no configurado. GPT deshabilitado.")

# ── Flask + estado ────────────────────────────────────────────────────────────
app = Flask(__name__)
_state_store = StateStore()
user_state = _StateMap(_state_store)
user_data = _DataMap(_state_store)



def _service_to_product_code(svc: str | None) -> str:
    return {
        "imss": "prestamo_imss_ley73",
        "auto": "seguro_vida",
        "vida": "seguro_vida",
        "vrim": "seguro_vida",
        "emp": "nomina_empresarial",
        "fp": "credito_empresarial_sin_garantia",
    }.get((svc or "").strip(), "seguro_vida")


def _conversation_intent(text: str) -> str:
    n = norm(text)
    if any(k in n for k in ("estatus", "seguimiento", "folio", "avance")):
        return "policy_status"
    if any(k in n for k in ("pago", "pagos", "mensualidad", "mensualidades")):
        return "payment_question"
    if any(k in n for k in ("requisito", "requisitos", "documento", "documentos", "papeles", "ine")):
        return "document_request"
    if any(k in n for k in ("cotiza", "cotizacion", "precio", "cuanto", "monto", "simular")):
        return "quote_request"
    if any(k in n for k in ("llamen", "asesor", "contacten", "ayuda humana")):
        return "human_help"
    return "general_question"


def _requires_human_guardrail(text: str) -> tuple[bool, bool, bool]:
    n = norm(text)
    business_action_requested = any(
        k in n for k in (
            "autoriza", "aprobar", "activar", "cancelar", "contratar",
            "depositar", "transferir", "pagar", "registrar", "dar de alta"
        )
    )
    persistent_state_mutation_requested = any(
        k in n for k in (
            "cambia mi", "actualiza mi", "modifica mi", "corrige mi",
            "edita mi", "actualizar datos", "cambiar datos"
        )
    )
    business_data = any(
        k in n for k in (
            "curp", "rfc", "nss", "seguro social", "numero de poliza",
            "número de póliza", "cuenta bancaria", "clabe"
        )
    )
    return business_action_requested, persistent_state_mutation_requested, business_data


def _safe_reply_for_service(text: str, svc: str | None) -> tuple[str, str]:
    service = (svc or "").strip()
    n = norm(text)
    if service == "imss":
        if any(k in n for k in ("requisito", "requisitos", "papeles", "documento")):
            return (
                "document_request",
                "Con gusto te oriento con el Préstamo IMSS Ley 73. Para revisar tu perfil necesito saber si tu pensión es del IMSS bajo Ley 73 y de cuánto es aproximadamente. ¿Me compartes esos dos datos?"
            )
        return (
            "quote_request" if any(k in n for k in ("monto", "prestamo", "credito", "cuanto")) else "general_question",
            "Sí te puedo orientar con el Préstamo IMSS Ley 73. Para darte información exacta necesito confirmar si eres pensionado del IMSS Ley 73 y el monto aproximado de tu pensión. ¿Cuál es tu pensión mensual?"
        )
    if service == "auto":
        return (
            "quote_request",
            "Con gusto te ayudo con tu seguro de auto. La cotización depende del vehículo y perfil. ¿Me compartes marca, modelo y año de tu auto?"
        )
    if service == "vida":
        return (
            "quote_request",
            "Te ayudo con vida y gastos médicos. Para orientarte bien necesito saber qué buscas: vida, gastos médicos mayores o ambas coberturas. ¿Cuál te interesa?"
        )
    if service == "vrim":
        return (
            "general_question",
            "VRIM es una membresía médica. Para orientarte mejor necesito saber si la quieres solo para ti o para más personas. ¿Cuántas personas serían?"
        )
    if service == "emp":
        return (
            "quote_request",
            "Te apoyo con financiamiento empresarial. Para darte una guía correcta necesito saber a qué se dedica tu empresa y qué monto buscas. ¿Cuál es el giro y cuánto necesitas?"
        )
    if service == "fp":
        return (
            "quote_request",
            "Te ayudo con financiamiento práctico. Para orientarte con precisión necesito saber el monto y el tiempo en que lo requieres. ¿Cuánto necesitas y para cuándo?"
        )
    return (
        _conversation_intent(text),
        "Con gusto te oriento sobre préstamo IMSS, seguro de auto, vida/GMM, VRIM, financiamiento empresarial o financiamiento práctico. ¿Cuál de estos servicios te interesa?"
    )



# ── Idempotencia ──────────────────────────────────────────────────────────────
_seen_ids: set = set()
_seen_dq: deque = deque(maxlen=3000)
_id_lock = threading.Lock()
_tl = threading.local()

def _mid() -> str:
    return getattr(_tl, "mid", "")

# ── Google Sheets ─────────────────────────────────────────────────────────────
_svc = None
_srdy = False
_HDR = ["Phone", "Nombre", "Mensaje", "Fecha", "Tipo", "Origen",
        "Servicio", "Estado", "Resultado", "Error", "MsgID"]

def _sheets_init():
    global _svc, _srdy
    if not _glibs or not GG_CREDS or not SHEET_ID:
        return
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GG_CREDS),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        _svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _srdy = True
        r = _svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:K1").execute()
        if not r.get("values"):
            _svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:K1",
                valueInputOption="RAW", body={"values": [_HDR]}).execute()
        log.info("✅ Sheets inicializado.")
    except Exception:
        log.exception("❌ Error inicializando Sheets.")

def _svc_name(phone: str) -> str:
    s = user_state.get(phone, "")
    if s.startswith("imss_"):
        return "imss"
    if s.startswith("emp_"):
        return "empresarial"
    if s.startswith("fp_"):
        return "fp"
    return "desconocido"

def _nombre(phone: str) -> str:
    return str((user_data.get(phone) or {}).get("nombre", ""))[:100]

def _log(phone, nombre, msg, tipo, origen, resultado="", error="", mid=""):
    if not _srdy:
        return
    try:
        ph = re.sub(r"\D", "", str(phone))
        _svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A:K",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[
                ph, str(nombre)[:100], str(msg)[:500], now_mx(),
                tipo, origen, _svc_name(ph),
                str(user_state.get(ph, ""))[:100],
                resultado, str(error)[:300], str(mid)[:100]
            ]]}).execute()
    except Exception:
        log.exception("❌ Error en Sheets")

# ── WhatsApp helpers ──────────────────────────────────────────────────────────
_WA_BASE = "https://graph.facebook.com/v20.0"

def _wa_post(payload: dict) -> requests.Response:
    url = f"{_WA_BASE}/{WABA_ID}/messages"
    hdr = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, headers=hdr, json=payload, timeout=15)

def send_msg(to: str, text: str) -> bool:
    if not META_TOKEN or not WABA_ID:
        log.error("❌ META_TOKEN o WABA_PHONE_ID no configurados")
        return False
    try:
        r = _wa_post({"messaging_product": "whatsapp", "to": str(to),
                      "type": "text", "text": {"body": text}})
        ok = r.status_code in (200, 201)
        if not ok:
            log.error(f"❌ WA {r.status_code}: {r.text[:200]}")
        _log(to, _nombre(to), text, "saliente", "bot",
             "ok" if ok else "error", "" if ok else r.text[:200], _mid())
        return ok
    except Exception as e:
        log.exception(f"💥 send_msg {to}")
        _log(to, _nombre(to), text, "saliente", "bot", "error", str(e)[:200], _mid())
        return False

def _is_internal_request(req) -> bool:
    if not INTERNAL_TOKEN:
        return False
    provided = (req.headers.get("X-Internal-Token", "") or "").strip()
    return bool(provided) and hmac.compare_digest(provided, INTERNAL_TOKEN)

_TPL_PARAM_FALLBACK = "Nuevo lead recibido. Revisar conversación en WhatsApp."

# El cuerpo de la plantilla aprobada lleva texto literal ademas de {{1}}, y el
# limite de Meta aplica al mensaje ya renderizado. Se deja holgura frente a los
# 1024 del limite para que literal + parametro nunca lo rebasen.
_TPL_PARAM_LIMIT = 900

# ── Ventana de 24h del asesor ─────────────────────────────────────────────────
# WhatsApp solo entrega texto libre dentro de las 24h posteriores al ultimo
# mensaje que el destinatario le envio al negocio. Fuera de esa ventana Meta
# ACEPTA el envio con HTTP 200/201 y lo descarta despues, sin error sincrono: por
# eso `✅ Asesor notificado` podia registrarse sin que la alerta llegara nunca.
#
# El asesor es un numero unico y conocido, asi que Vicky puede llevar su propia
# contabilidad de la ventana en vez de depender de que Meta avise.
_ADV_WINDOW_KEY = "adv_window"
_ADV_WINDOW_SECONDS = 24 * 60 * 60
# El registro sobrevive mucho mas que la ventana a proposito: guardar el timestamp
# permite distinguir "cerrada" (hay registro y es viejo) de "desconocida" (nunca
# se ha visto escribir al asesor). Son casos con decisiones distintas.
_ADV_WINDOW_RECORD_TTL = 30 * 24 * 60 * 60
# Correlacion ligera para observabilidad: sin PII, vive lo suficiente para cubrir
# estados tardios de Meta (`read` puede llegar mucho despues de `delivered`).
_ADV_WAMID_TTL = 48 * 60 * 60
# El cuerpo de la alerta solo se retiene lo necesario para poder reenviarla si
# Meta reporta `failed`. TTL corto y deliberadamente separado de la correlacion.
_ADV_RETRY_TTL = 2 * 60 * 60


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _mask_phone(value) -> str:
    d = _digits(value)
    return ("*" * 8 + d[-4:]) if len(d) >= 4 else "****"


def _is_advisor_phone(phone) -> bool:
    adv = _digits(ADVISOR_NUM)
    return bool(adv) and _digits(phone) == adv


def _advisor_window_touch() -> None:
    """El asesor escribio: la ventana de 24h queda abierta desde ahora."""
    _state_store.aux_set(_ADV_WINDOW_KEY, str(time.time()), _ADV_WINDOW_RECORD_TTL)


def _advisor_window_expire() -> None:
    """Marca la ventana como cerrada conservando registro.

    Se usa cuando Meta confirma un `failed`: su veredicto manda sobre la
    contabilidad local. Guardar `0` deja el estado en `closed` y no en
    `unknown`, que llevaria a reintentar texto libre.
    """
    _state_store.aux_set(_ADV_WINDOW_KEY, "0", _ADV_WINDOW_RECORD_TTL)


def _advisor_window_state() -> str:
    """`open` | `closed` | `unknown`.

    `unknown` es un tercer estado real, no un detalle: tras un despliegue nuevo
    o un Redis vacio no hay registro, y ahi se conserva el comportamiento
    historico (texto libre primero) en vez de gastar un template a ciegas. El
    reenvio reactivo por `statuses[].failed` cubre ese hueco.
    """
    raw = _state_store.aux_get(_ADV_WINDOW_KEY)
    if raw is None:
        return "unknown"
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return "unknown"
    return "open" if (time.time() - ts) < _ADV_WINDOW_SECONDS else "closed"


def _advisor_wamid_remember(wamid: str, level: str, msg: str) -> None:
    _state_store.aux_set(
        f"adv_wamid:{wamid}",
        json.dumps({"ts": time.time(), "level": level}, ensure_ascii=False),
        _ADV_WAMID_TTL,
    )
    _state_store.aux_set(f"adv_retry:{wamid}", str(msg or "")[:2000], _ADV_RETRY_TTL)


def _advisor_wamid_lookup(wamid: str):
    raw = _state_store.aux_get(f"adv_wamid:{wamid}")
    if raw is None:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except Exception:
        return None


def _advisor_record_send(resp, level: str, msg: str) -> str:
    """Instrumentacion del envio al asesor. Best-effort y sin excepciones.

    Registra lo que antes se perdia por completo: status HTTP real, si el cuerpo
    era JSON valido y el `wamid`. Sin el `wamid` no hay forma de correlacionar el
    intento con el estado asincrono que Meta manda despues, que es exactamente la
    evidencia que faltaba para diagnosticar este defecto.
    """
    status = getattr(resp, "status_code", 0)
    wamid = ""
    json_valid = False
    try:
        body = resp.json()
        if isinstance(body, dict):
            json_valid = True
            msgs = body.get("messages") or []
            if msgs and isinstance(msgs[0], dict):
                wamid = str(msgs[0].get("id") or "")
    except Exception:
        json_valid = False
    log.info(
        "asesor_envio: nivel=%s http=%s json_valido=%s wamid=%s destino=%s",
        level, status, json_valid, (wamid[:24] or "ninguno"), _mask_phone(ADVISOR_NUM),
    )
    if wamid:
        try:
            _advisor_wamid_remember(wamid, level, msg)
        except Exception:
            log.warning("asesor_correlacion_no_persistida: wamid=%s", wamid[:24])
    return wamid


def _notify_advisor_via_template(msg: str, motivo: str) -> bool:
    """Nivel 2 — template aprobada.

    Unico formato que Meta entrega fuera de la ventana de 24h. Sin
    ADVISOR_TEMPLATE_NAME configurado no existe ninguna ruta de entrega.
    """
    if not ADV_TPL:
        log.warning("advisor_template_missing: ADVISOR_TEMPLATE_NAME no configurado. "
                    "Define esta variable con el template aprobado en Meta para "
                    "notificaciones fuera de ventana 24h.")
        return False
    tpl_param = _sanitize_template_param(msg, limit=_TPL_PARAM_LIMIT)
    r2 = _wa_post({"messaging_product": "whatsapp", "to": ADVISOR_NUM,
                   "type": "template", "template": {
                       "name": ADV_TPL, "language": {"code": ADV_TPL_LANG},
                       "components": [{"type": "body",
                                       "parameters": [{"type": "text", "text": tpl_param}]}]}})
    ok = r2.status_code in (200, 201)
    if ok:
        _advisor_record_send(r2, "template", msg)
    _log(ADVISOR_NUM, "Asesor", msg, "saliente", "asesor",
         "ok" if ok else "error", "" if ok else r2.text[:200], _mid())
    if ok:
        log.info("asesor_template_ok: Asesor notificado vía template (%s)", motivo)
    else:
        log.error("asesor_template_failed: %s", r2.text[:200])
    return ok


def _sanitize_template_param(text: str, limit: int = 1024) -> str:
    """Meta rechaza parámetros de template con saltos de línea, tabs o más de
    4 espacios consecutivos (error 132000/132012). Los mensajes al asesor son
    multilínea, así que pasar msg crudo hacía fallar SIEMPRE el nivel 2 fuera
    de ventana 24h. Colapsa todo whitespace/control a espacio simple, preserva
    acentos y emojis, trunca a `limit` y nunca regresa vacío."""
    s = str(text) if text is not None else ""
    s = "".join(
        ch if (ch == " " or not unicodedata.category(ch).startswith("C")) else " "
        for ch in s
    )
    s = re.sub(r"\s+", " ", s).strip()
    if limit > 0:
        s = s[:limit].strip()
    return s or _TPL_PARAM_FALLBACK


def notify_advisor(msg: str) -> bool:
    """
    Nivel 1 — texto libre (solo se entrega dentro de la ventana 24h del asesor).
    Nivel 2 — template aprobada (ADVISOR_TEMPLATE_NAME), con el parámetro
    sanitizado (_sanitize_template_param), nunca msg crudo.

    Cuando consta que la ventana está cerrada se salta el nivel 1: Meta lo
    aceptaría con HTTP 200 y lo descartaría después sin avisar de forma síncrona,
    así que intentarlo solo produce un log de éxito falso. Si el estado de la
    ventana se desconoce se conserva el comportamiento histórico (texto libre
    primero) y el reenvío reactivo por `statuses[].failed` cubre ese caso.

    El contrato booleano no cambia: un 200/201 sin `wamid` sigue devolviendo
    True. Devolver False ahí rompería /ext/lead, que responde HTTP 502 al
    formulario de cohifis.com cuando esta función falla.
    """
    if not ADVISOR_NUM:
        return False
    try:
        if _advisor_window_state() == "closed" and ADV_TPL:
            log.info("asesor_ventana_cerrada: envío directo por template "
                     "(texto libre no se entregaría)")
            return _notify_advisor_via_template(msg, motivo="ventana_cerrada")
    except Exception:
        # La contabilidad de la ventana nunca puede impedir un envío: ante
        # cualquier fallo se sigue por el camino histórico.
        log.exception("💥 notify_advisor ventana")
    try:
        r = _wa_post({"messaging_product": "whatsapp", "to": ADVISOR_NUM,
                      "type": "text", "text": {"body": msg}})
        if r.status_code in (200, 201):
            _advisor_record_send(r, "texto_libre", msg)
            log.info("✅ Asesor notificado (texto libre)")
            _log(ADVISOR_NUM, "Asesor", msg, "saliente", "asesor", "ok", "", _mid())
            return True

        err1 = f"HTTP {r.status_code}: {r.text[:150]}"
        log.warning("asesor_text_fallback_template_attempt: texto libre falló (%s). "
                    "Reintentando con template...", err1)

        if not ADV_TPL:
            log.warning("advisor_template_missing: ADVISOR_TEMPLATE_NAME no configurado. "
                        "Define esta variable con el template aprobado en Meta para "
                        "notificaciones fuera de ventana 24h.")
            _log(ADVISOR_NUM, "Asesor", msg, "saliente", "asesor", "error", err1, _mid())
            return False

        return _notify_advisor_via_template(msg, motivo="texto_libre_falló")

    except Exception:
        log.exception("💥 notify_advisor")
        return False


BOARDROOM_URL = os.getenv(
    "BOARDROOM_URL",
    "https://boardroom-engine.onrender.com"
).strip()
BOARDROOM_API_TOKEN = os.getenv("BOARDROOM_API_TOKEN", "").strip()
BUS_URL = os.getenv("BUS_URL", "").strip()
BUS_INTERNAL_TOKEN = os.getenv("BUS_INTERNAL_TOKEN", "").strip()
_BUS_ACTIVE = os.getenv("BUS_ENABLED", "true").strip().lower() \
              in {"1", "true", "yes", "on"}
BOARDROOM_IS_AUTHORITY = True
NEUTRAL_FALLBACK_MESSAGE = "Recibí tu mensaje. En un momento te atiendo."
_BOARDROOM_ALLOWED_INSTRUCTIONS = {
    "send_message",
    "ask_question",
    "send_options",
    "request_document",
    "notify_advisor",
    "handoff",
    "no_action",
}


def _emit_bus_event(
    phone: str,
    text: str,
    event_type: str = "inbound_message",
    template_name: str | None = None,
    intent: str | None = None,
    metadata: dict | None = None,
) -> None:
    if not _BUS_ACTIVE:
        return
    if not BUS_URL or not BUS_INTERNAL_TOKEN:
        log.warning("BUS_URL o BUS_INTERNAL_TOKEN no configurados — emit omitido")
        return

    payload: dict = {
        "source": "vicky_redes",
        "event_type": event_type,
        "telefono": phone,
        "mensaje": text or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if template_name:
        payload["template_name"] = template_name
    if intent:
        payload["intent"] = intent
    if metadata:
        payload["metadata"] = metadata

    def _post() -> None:
        try:
            requests.post(
                BUS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=3,
            )
        except Exception as exc:
            log.warning(
                "Bus emit fallido phone_last4=%s error=%s: %s",
                str(phone)[-4:],
                type(exc).__name__,
                str(exc),
            )

    threading.Thread(target=_post, daemon=True).start()


def _bus_event_url() -> str:
    url = (BUS_URL or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/bus/event"):
        return url
    return f"{url}/bus/event"


def _bus_confirm_url() -> str:
    url = _bus_event_url()
    if not url:
        return ""
    return f"{url}/confirm"


def _message_text(msg_obj: dict, mtype: str) -> str:
    if mtype == "text":
        return ((msg_obj.get("text") or {}).get("body") or "").strip()[:500]
    if mtype == "button":
        btn = msg_obj.get("button") or {}
        return (btn.get("text") or btn.get("payload") or "").strip()[:500]
    return ""


def _canonical_message_type(mtype: str) -> str:
    return mtype if mtype in {"text", "audio", "image", "document", "button"} else "unknown"


def _campaign_source(ref: dict) -> str:
    source_type = norm(ref.get("source_type", ""))
    if source_type in {"ad", "ads", "facebook"}:
        return "facebook"
    if source_type in {"whatsapp", "web"}:
        return source_type
    return "unknown" if ref else "direct"


def _campaign_product_hint(msg_obj: dict, n: str) -> str:
    ref = msg_obj.get("referral") or {}
    fields = " ".join(
        norm(str(ref.get(k, "")))
        for k in ("headline", "body", "source_id", "source_url")
    )
    if any(k in f"{fields} {n}" for k in _IMSS_REF_KW | _IMSS_STRONG):
        return "prestamo_imss"
    if _is_ctc_meta_campaign_referral(msg_obj, n):
        return "credito_empresarial_sin_garantia"
    return "unknown"


def _attachments_for_message(msg_obj: dict, mtype: str) -> list[dict]:
    media = msg_obj.get(mtype) or {}
    media_id = media.get("id")
    if mtype in {"image", "document", "audio"} and media_id:
        return [{"type": mtype, "media_id": media_id}]
    return []


def _build_boardroom_event(phone: str, text: str, msg_obj: dict, mtype: str) -> dict:
    ref = msg_obj.get("referral") or {}
    n = norm(text)
    data = user_data.get(phone, {})
    state = user_state.get(phone, "")
    return {
        "event_id": str(uuid.uuid4()),
        "message_id": msg_obj.get("id", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "whatsapp",
        "channel": "vicky_campanas",
        "phone": phone,
        "contact_name": _nombre(phone) or None,
        "text": text or "",
        "message_type": _canonical_message_type(mtype),
        "campaign": {
            "source": _campaign_source(ref),
            "campaign_id": str(ref.get("campaign_id") or ref.get("source_id") or "") or None,
            "ad_id": str(ref.get("ad_id") or ref.get("source_id") or "") or None,
            "product_hint": _campaign_product_hint(msg_obj, n),
        },
        "conversation": {
            "conversation_id": f"vicky_campanas:{phone}",
            "last_known_stage": state or None,
            "last_bot_message": data.get("last_bot_message") or None,
        },
        "attachments": _attachments_for_message(msg_obj, mtype),
        "metadata": {
            "raw_payload_available": True,
            "vicky_version": "bot-vicky-5146-phase1",
            "environment": "production",
        },
    }


def _parse_boardroom_instruction(body: object, event_id: str) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict):
        return None, "invalid_json"
    status = body.get("status")
    if status not in {"ok", "fallback", "error"}:
        return None, "invalid_status"
    if body.get("event_id") and body.get("event_id") != event_id:
        log.warning("Boardroom event_id mismatch sent=%s got=%s", event_id, body.get("event_id"))
    instruction = body.get("instruction")
    if not isinstance(instruction, dict):
        return None, "missing_instruction"
    instruction_type = str(instruction.get("type") or "").strip()
    if instruction_type not in _BOARDROOM_ALLOWED_INSTRUCTIONS:
        log.error("Boardroom instruction type not allowed: %s", instruction_type)
        return None, "invalid_instruction_type"
    return body, None


def _request_boardroom_instruction(payload: dict) -> tuple[dict | None, str | None]:
    if not _BUS_ACTIVE or not BUS_URL:
        return None, "bus_disabled_or_empty"
    if not BUS_INTERNAL_TOKEN:
        return None, "missing_bus_token"
    try:
        resp = requests.post(
            _bus_event_url(),
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                "X-Source-System": "vicky",
                "X-Event-Type": "inbound_message",
            },
            timeout=3,
        )
        if resp.status_code >= 400:
            return None, f"http_{resp.status_code}"
        body = resp.json() if resp.text else {}
        return _parse_boardroom_instruction(body, payload["event_id"])
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception as exc:
        log.warning("Boardroom bus request failed: %s: %s", type(exc).__name__, exc)
        return None, "exception"


def _instruction_message(instruction: dict) -> str:
    message = str(instruction.get("message") or "").strip()
    options = instruction.get("options")
    if instruction.get("type") == "send_options" and isinstance(options, list) and options:
        labels = []
        for idx, option in enumerate(options, start=1):
            if isinstance(option, dict) and option.get("label"):
                labels.append(f"{idx}. {option['label']}")
        if labels:
            return "\n".join([message, *labels]).strip()
    return message


def _execute_boardroom_instruction(phone: str, body: dict) -> tuple[bool, str, str | None]:
    instruction = body.get("instruction") or {}
    instruction_type = instruction.get("type")
    advisor = body.get("advisor_notification") or {}
    delivery_status = "unknown"
    try:
        if advisor.get("required") and advisor.get("message"):
            notify_advisor(str(advisor.get("message")))

        if instruction_type == "no_action":
            return True, delivery_status, None

        if instruction_type == "notify_advisor":
            message = _instruction_message(instruction)
            if message:
                notify_advisor(message)
            return True, delivery_status, None

        message = _instruction_message(instruction) or NEUTRAL_FALLBACK_MESSAGE
        ok = send_msg(phone, message)
        delivery_status = "sent" if ok else "failed"
        return ok, delivery_status, None if ok else "send_failed"
    except Exception as exc:
        log.exception("Boardroom instruction execution failed")
        return False, "failed", f"{type(exc).__name__}: {exc}"


def _confirm_boardroom_execution(body: dict, executed: bool, delivery_status: str, error: str | None) -> None:
    instruction_id = body.get("instruction_id")
    if not instruction_id or not _BUS_ACTIVE or not BUS_URL or not BUS_INTERNAL_TOKEN:
        return
    try:
        requests.post(
            _bus_confirm_url(),
            json={
                "instruction_id": instruction_id,
                "executed": bool(executed),
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "delivery_status": delivery_status,
                "error": error,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                "X-Source-System": "vicky",
            },
            timeout=3,
        )
    except Exception as exc:
        log.warning("Boardroom confirm failed instruction_id=%s error=%s", instruction_id, exc)


def _send_neutral_fallback(phone: str) -> None:
    send_msg(phone, NEUTRAL_FALLBACK_MESSAGE)


def _handle_boardroom_authority(phone: str, msg_obj: dict, mtype: str, text: str) -> bool:
    if not BOARDROOM_IS_AUTHORITY:
        return False

    payload = _build_boardroom_event(phone, text, msg_obj, mtype)
    body, error = _request_boardroom_instruction(payload)
    if body is None:
        log.warning("Boardroom authority fallback reason=%s phone_last4=%s", error, phone[-4:])
        _send_neutral_fallback(phone)
        return True

    executed, delivery_status, exec_error = _execute_boardroom_instruction(phone, body)
    _confirm_boardroom_execution(body, executed, delivery_status, exec_error)
    if not executed:
        _send_neutral_fallback(phone)
    return True


def _notify_boardroom_document(phone: str, media_id: str, doc_type: str) -> None:
    """Notifica a Boardroom que Vicky Redes recibió un documento."""
    if not BOARDROOM_URL or not BOARDROOM_API_TOKEN:
        log.warning("boardroom_not_configured: documento no notificado")
        return
    try:
        resp = requests.post(
            f"{BOARDROOM_URL}/api/document/process",
            json={
                "phone": phone,
                "media_id": media_id,
                "doc_type": doc_type,
                "source": "vicky_redes"
            },
            headers={
                "Content-Type": "application/json",
                "X-Boardroom-Token": BOARDROOM_API_TOKEN
            },
            timeout=5
        )
        log.info("boardroom_doc_notified: phone=%s status=%s", phone, resp.status_code)
    except Exception as e:
        log.error("boardroom_doc_notify_failed: phone=%s error=%s", phone, e)


def _notify_boardroom_lead_qualified(phone: str, product_code: str, data: dict) -> None:
    """Notifica a Boardroom cuando Vicky Redes completa calificación."""
    if not BOARDROOM_URL or not BOARDROOM_API_TOKEN:
        log.warning("boardroom_not_configured: lead no notificado")
        return
    try:
        from uuid import uuid4
        resp = requests.post(
            f"{BOARDROOM_URL}/boardroom/tasks/commercial",
            json={
                "event_id": str(uuid4()),
                "lead_id": phone,
                "event_type": "lead_new",
                "product_code": product_code,
                "product_config": {
                    "product_code": product_code,
                    "product_name": product_code.replace("_", " ").title(),
                    "priority": "A",
                    "requirements": ["ine", "comprobante_domicilio"],
                    "stage_scripts": {
                        "qualification": "Prospecto calificado por Vicky Redes.",
                        "default": "Seguimos con tu proceso COHIFIS."
                    },
                    "commission_rate": 0.12
                },
                "classification": {
                    "intent": "lead_new",
                    "confidence": 0.95
                },
                "advisor_id": "don_chiwy",
                "channel": "whatsapp",
                "source": "vicky_redes",
                "metadata": data
            },
            headers={
                "Content-Type": "application/json",
                "X-Boardroom-Token": BOARDROOM_API_TOKEN
            },
            timeout=8
        )
        log.info("boardroom_lead_notified: phone=%s product=%s status=%s",
                 phone, product_code, resp.status_code)
    except Exception as e:
        log.error("boardroom_lead_notify_failed: phone=%s error=%s", phone, e)

# ── Utilidades ────────────────────────────────────────────────────────────────
def norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFD", text.lower().strip())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("ñ", "n")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def yes_no(text: str) -> str:
    n = norm(text)
    toks = set(n.split())
    neg = {"no", "nel", "nop", "negativo", "tampoco", "nunca", "jamas"}
    pos = {"si", "sip", "claro", "ok", "vale", "afirmativo", "yes", "correcto", "exacto", "andale", "dale"}
    if toks & neg:
        return "no"
    if toks & pos or any(p in n for p in ("por supuesto", "desde luego", "claro que si")):
        return "si"
    return "?"

def extract_num(text: str):
    if not text:
        return None
    m = re.search(r"(\d{1,12})(\.\d+)?", re.sub(r"[$, ]", "", text))
    if not m:
        return None
    try:
        return float(m.group(1) + (m.group(2) or ""))
    except Exception:
        return None


def _ensure_user(phone: str) -> dict:
    return dict(user_data.get(phone) or {})

def reset(phone: str):
    user_state.pop(phone, None)
    user_data.pop(phone, None)

# ── Menú general ──────────────────────────────────────────────────────────────
_MENU = (
    "🏦 *Servicios Financieros Inbursa*\n"
    "────────────────────────────\n"
    "1️⃣  *Préstamo IMSS para pensionados*\n"
    "     🧮 Calcula una propuesta estimada con tu pensión\n\n"
    "2️⃣  *Seguro de Auto*\n"
    "     🚗 Cobertura amplia · Asistencia 24/7\n\n"
    "3️⃣  *Seguro de Vida y Salud*\n"
    "     🏥 Vida · GMM · Hospitalización\n\n"
    "4️⃣  *Tarjeta Médica VRIM*\n"
    "     💳 Consultas ilimitadas · Labs · Descuentos\n\n"
    "5️⃣  *Financiamiento Empresarial*\n"
    "     🏢 $100K–$100M · PYMES y empresas\n\n"
    "6️⃣  *Consigue Tu Crédito (CTC)*\n"
    "     💼 Crédito empresarial sin garantía para tu negocio o actividad independiente\n"
    "────────────────────────────\n"
    "Escribe el *número* o el nombre del servicio. 😊"
)

def show_menu(phone: str):
    send_msg(phone, _MENU)

# ── Detección de campaña IMSS ─────────────────────────────────────────────────
_IMSS_STRONG = {
    "prestamo imss", "credito imss",
    "prestamos imss", "creditos imss",
    "quiero prestamo imss", "quiero credito imss",
    "ley 73",
    "jubilado imss", "pensionado imss",
    "informacion sobre el prestamo imss",
    "quiero saber del prestamo imss",
}

_IMSS_REF_KW = {
    "imss", "pension", "pensionado", "jubilado", "ley 73",
    "prestamo imss", "credito imss"
}

def _is_campaign(msg_obj: dict, n: str) -> bool:
    ref = msg_obj.get("referral") or {}

    # CTC conserva prioridad absoluta: si el referral pertenece a una campaña/ad
    # de Consigue Tu Credito, IMSS nunca debe reclamarlo, aunque coincida algun
    # keyword generico.
    if _is_ctc_meta_campaign_referral(msg_obj, n):
        return False

    if ref:
        st = (ref.get("source_type") or "")
        sid = (ref.get("source_id") or "")
        hl = norm(ref.get("headline", ""))
        bd = norm(ref.get("body", ""))
        log.info(f"📎 referral source_type={st!r} source_id={sid!r} "
                 f"headline={hl[:50]!r} body={bd[:50]!r}")
        fields = f"{hl} {bd} {norm(sid)}"
        if any(k in fields for k in _IMSS_REF_KW):
            return True
        if _is_imss_meta_campaign_referral(msg_obj, n):
            return True

    if any(norm(k) in n for k in _IMSS_STRONG):
        return True

    return False

# ── Ruteo por referral de anuncio Meta (Click to WhatsApp) ────────────────────
# El mensaje que Meta pone en boca del prospecto ("Hello! Can I get more info
# on this?") no dice el producto; el producto viene en msg_obj["referral"]
# (headline/body del anuncio). Estos helpers leen ese referral para rutear
# emp/fp localmente ANTES de Boardroom. El referral IMSS ya lo cubre
# _is_campaign() y se evalua antes, asi que aqui solo llega lo no-IMSS.

_META_REF_KEYS = (
    "headline", "body", "source_id", "source_url",
    "campaign_id", "campaign_name", "ad_id", "ad_name",
    "ctwa_clid"
)

# Override CTC por campaña/anuncio Meta.
# Motivo: el mensaje público "Me interesa crédito empresarial" también es válido
# para Financiamiento Empresarial Inbursa, pero ESTA campaña/ad de Meta pertenece
# a Consigue Tu Crédito. Por eso el referral del anuncio tiene prioridad sobre
# keywords genéricos como "credito empresarial".
_CTC_META_REFERRAL_IDS = {
    "6951847773049",  # CTC_Video_WhatsApp_Ad_Julio2026
}
_CTC_META_REFERRAL_HINTS = (
    "ctc_whatsapp_leads_julio2026",
    "ctc_video_whatsapp_ad_julio2026",
)

# Los keywords ya estan en forma norm() (sin acentos): "crédito empresarial"
# y "credito empresarial" colapsan al mismo termino.
_META_REF_FP_STRONG = ("ctc", "consigue tu credito", "sin garantia")
_META_REF_EMP_STRONG = ("factoraje", "facturas por cobrar",
                        "creditos empresariales", "liquidez empresarial")
_META_REF_FP_KW = ("credito empresarial sin garantia",
                   "actividad independiente", "negocio independiente")
_META_REF_EMP_KW = ("credito empresarial", "financiamiento empresarial",
                    "liquidez inmediata", "capital de trabajo",
                    "empresa", "pyme", "pymes", "negocio")


def _campaign_referral_text(msg_obj: dict, text: str) -> str:
    """Concatena y normaliza el texto del usuario + todos los campos string
    del referral del anuncio (headline, body, ids, url, ctwa_clid y cualquier
    otro string), sin romper si el referral no existe o viene incompleto."""
    parts = [text or ""]
    ref = msg_obj.get("referral") or {}
    if isinstance(ref, dict):
        for key in _META_REF_KEYS:
            val = ref.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val)
        for key, val in ref.items():
            if key in _META_REF_KEYS:
                continue
            if isinstance(val, str) and val.strip():
                parts.append(val)
    return norm(" ".join(parts))


def _env_csv_set(name: str) -> set[str]:
    raw = os.getenv(name, "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _ctc_meta_referral_ids() -> set[str]:
    # Permite agregar nuevos ad_id/source_id/campaign_id sin tocar código:
    # CTC_META_REFERRAL_IDS="123,456"
    return _CTC_META_REFERRAL_IDS | _env_csv_set("CTC_META_REFERRAL_IDS")


def _ctc_meta_referral_hints() -> tuple[str, ...]:
    # Permite agregar nombres/pistas de campaña sin tocar código:
    # CTC_META_REFERRAL_HINTS="campana_nueva,ad_nuevo"
    extra = tuple(norm(x) for x in _env_csv_set("CTC_META_REFERRAL_HINTS"))
    return tuple(_CTC_META_REFERRAL_HINTS) + extra


def _is_ctc_meta_campaign_referral(msg_obj: dict, text: str = "") -> bool:
    """Override estricto para campañas Meta de Consigue Tu Crédito.

    Solo aplica cuando WhatsApp entrega msg_obj["referral"]. No cambia el
    comportamiento de mensajes directos como "Me interesa crédito empresarial",
    porque esos no traen referral y siguen entrando al flujo empresarial normal.
    """
    ref = msg_obj.get("referral") or {}
    if not isinstance(ref, dict) or not ref:
        return False

    allowed_ids = _ctc_meta_referral_ids()
    for key in ("source_id", "ad_id", "campaign_id"):
        raw = str(ref.get(key) or "").strip()
        if not raw:
            continue
        raw_digits = re.sub(r"\D", "", raw)
        if raw in allowed_ids or (raw_digits and raw_digits in allowed_ids):
            return True

    referral_text = _campaign_referral_text(msg_obj, text)
    return any(hint and hint in referral_text for hint in _ctc_meta_referral_hints())


# Override de campaña/anuncio Meta para Préstamo IMSS Ley 73, mismo patrón que
# CTC. Vacío a propósito: el ad_id real de la campaña IMSS lo carga Don Chiwy
# via IMSS_META_REFERRAL_IDS antes de encender la pauta. No se inventa aquí.
_IMSS_META_REFERRAL_IDS: set[str] = set()


def _imss_meta_referral_ids() -> set[str]:
    # IMSS_META_REFERRAL_IDS="123,456" agrega ad_id/source_id/campaign_id sin
    # tocar código.
    return _IMSS_META_REFERRAL_IDS | _env_csv_set("IMSS_META_REFERRAL_IDS")


def _imss_meta_referral_hints() -> tuple[str, ...]:
    # IMSS_META_REFERRAL_HINTS="campana_nueva,ad_nuevo" agrega pistas de
    # nombre de campaña sin tocar código.
    return tuple(norm(x) for x in _env_csv_set("IMSS_META_REFERRAL_HINTS"))


def _is_imss_meta_campaign_referral(msg_obj: dict, text: str = "") -> bool:
    """Override por ad_id/campaign_id/source_id o hints de campaña Meta para
    Préstamo IMSS Ley 73. Evaluado en _is_campaign() DESPUÉS del check CTC,
    para que CTC conserve prioridad absoluta si un referral coincide con
    ambos."""
    ref = msg_obj.get("referral") or {}
    if not isinstance(ref, dict) or not ref:
        return False

    allowed_ids = _imss_meta_referral_ids()
    for key in ("source_id", "ad_id", "campaign_id"):
        raw = str(ref.get(key) or "").strip()
        if not raw:
            continue
        raw_digits = re.sub(r"\D", "", raw)
        if raw in allowed_ids or (raw_digits and raw_digits in allowed_ids):
            return True

    referral_text = _campaign_referral_text(msg_obj, text)
    return any(hint and hint in referral_text for hint in _imss_meta_referral_hints())


def _detect_meta_referral_svc(msg_obj: dict, text: str) -> str | None:
    """Detecta el producto (emp/fp) a partir del referral del anuncio Meta.
    Prioridad: override CTC por campaña/ad -> senales fuertes de CTC
    (ctc/consigue tu credito/sin garantia) -> senales fuertes empresariales ->
    keywords genericos. Devuelve None si no hay senal clara."""
    ref = msg_obj.get("referral") or {}
    if not isinstance(ref, dict) or not ref:
        return None

    n = _campaign_referral_text(msg_obj, text)
    if not n:
        return None
    toks = set(n.split())

    def hit(kw: str) -> bool:
        return (kw in toks) if " " not in kw else (kw in n)

    if _is_ctc_meta_campaign_referral(msg_obj, text):
        return "fp"
    if any(hit(k) for k in _META_REF_FP_STRONG):
        return "fp"
    if any(hit(k) for k in _META_REF_EMP_STRONG):
        return "emp"
    if any(hit(k) for k in _META_REF_FP_KW):
        return "fp"
    if any(hit(k) for k in _META_REF_EMP_KW):
        return "emp"
    return None

# ── Constantes financieras del préstamo IMSS ──────────────────────────────────
# Este bloque vive ARRIBA de _SYS a proposito: _SYS se construye con
# IMSS_TASA_ANUAL_SIN_IVA / IMSS_CAT_SIN_IVA y en Python el modulo se evalua de
# arriba hacia abajo -- dejarlas en la seccion de la calculadora provocaria
# NameError al importar app. Los valores son exactamente los mismos que tenia
# la seccion "Calculadora de prestamo IMSS"; no se modifico ninguno.
#
# Puerto exacto del modo "Calcular por pension" de cotizador_prestamos_imss.jsx
# (mismas constantes financieras, misma biseccion). No se inventa formula nueva.
IMSS_TASA_MENSUAL = 0.018659
IMSS_IVA_RATE = 0.16
IMSS_CAT = 29.3          # criterio CON IVA, uso interno. Nunca se muestra al cliente.
IMSS_PLAZO_MESES = 60
IMSS_LIMITE_DESCUENTO = 0.30
IMSS_MONTO_MINIMO = 40000

# Tasa fija anual sin IVA, en puntos porcentuales (misma unidad que IMSS_CAT).
# Derivada de la tasa mensual vigente: 0.018659 * 12 * 100 = 22.3908 -> 22.39%.
# Fuente unica: no se hardcodea 22.39 en ninguna plantilla de texto.
IMSS_TASA_ANUAL_SIN_IVA = IMSS_TASA_MENSUAL * 12 * 100

# Costo Anual Total sin IVA, en puntos porcentuales. Valor OFICIAL, no calculado.
# Procedencia documental: diez tablas de amortizacion generadas por el cotizador
# oficial de Banco Inbursa entregadas por el usuario el 28 de julio de 2026
# (plazos 6, 12, 18, 24, 30, 36, 42, 48, 54 y 60 meses). Las diez reportan
# "Tasa de interes fija anual sin IVA: 22.39%" y "CAT sin IVA: 24.8%".
# El control de coherencia ((1+IMSS_TASA_MENSUAL)**12 - 1)*100 = 24.8377 se
# usa solo para detectar deriva: si deja de coincidir, prevalece este valor
# oficial y la discrepancia debe hacerse visible (ver pruebas).
IMSS_CAT_SIN_IVA = 24.8

# Plazos vigentes del cotizador oficial de Inbursa. Fuente unica: la lista no
# se repite literal en ningun otro punto del modulo.
IMSS_PLAZOS_DISPONIBLES = (6, 12, 18, 24, 30, 36, 42, 48, 54, 60)

# ── GPT ───────────────────────────────────────────────────────────────────────
_SYS = (
    "Eres Vicky, asistente comercial de Christian López, asesor financiero de Inbursa. "
    "Orientas sobre 6 servicios: (1) Préstamo IMSS Pensionados Ley 73 $40K–$650K sin aval, "
    "(2) Seguro Auto, (3) Seguro Vida/GMM, (4) VRIM tarjeta médica, "
    "(5) Financiamiento Empresarial $100K–$100M, (6) Consigue Tu Crédito (CTC): crédito empresarial "
    "sin garantía para negocio o actividad independiente. "
    "Responde en español mexicano, máximo 100 palabras, tono profesional y cálido. "
    "Resuelve dudas reales del cliente. Si la pregunta es abierta, contesta de forma útil; "
    "no mandes al menú salvo que el cliente lo pida. "
    "Termina con UNA sola pregunta cuando ayude a avanzar. "
    "No inventes tasas, requisitos ni condiciones no confirmadas. "
    "DATOS FINANCIEROS COHIFIS: "
    f"IMSS Ley 73: tasa fija anual {IMSS_TASA_ANUAL_SIN_IVA:.2f}% sin IVA, "
    f"CAT informativo {IMSS_CAT_SIN_IVA:.1f}% sin IVA (condiciones oficiales del "
    "cotizador Inbursa, informativas y sujetas a cambio). "
    "Monto $40,000 a $650,000. Sin aval. Sin cambio de banco. "
    "VRIM Plus preelegibilidad preliminar en préstamos IMSS desde $40,000 en adelante, "
    "sujeta a formalización y a las condiciones de la promoción; nunca afirmar como "
    "otorgada ni garantizada. "
    "PyME Alta Eficiencia: 18%. PyME Flexible: 36%. "
    "Tolerancia buró hasta $30,000 de mancha. "
    "TPV: desde 1.05% por transacción. Sin mensualidad fija. "
    "VRIM: membresía médica con elegibilidad preliminar desde IMSS $40,000, nunca "
    "presentarla como regalo confirmado antes de la formalización. "
    # Se elimino la comparacion "75.19% vs 29.3%": el 29.3% es criterio CON IVA
    # y no hay evidencia verificable de bajo que criterio esta expresado el
    # 75.19% de la competencia. No se sustituye por otra cifra.
    "COMPORTAMIENTO: Si cliente objeta precio, explicar las condiciones oficiales "
    "vigentes del producto sin compararlas con otras instituciones. "
    "Si cliente objeta trámite enfatizar proceso 100% digital. "
    "Si cliente muestra intención de compra dirigir al funnel correcto. "
    "NUNCA mezclar productos B2C con B2B en misma respuesta. "
    "Cierre siempre: Ten listos tus documentos en PDF o foto clara. "
    "Christian López te contactará por WhatsApp para recibirlos y agendar tu cierre sin salir de casa."
)

_SERVICE_LABELS = {
    "imss": "Préstamo IMSS Ley 73",
    "auto": "Seguro de Auto",
    "vida": "Seguro de Vida y Salud",
    "vrim": "Tarjeta Médica VRIM",
    "emp": "Financiamiento Empresarial",
    "fp": "Consigue Tu Crédito (CTC)",
    "general": "Consulta general"
}

def ask_gpt(prompt: str, svc: str | None = None) -> str:
    if not _oai:
        return "Lo siento, servicio no disponible en este momento."
    try:
        ctx = _SERVICE_LABELS.get(svc or "", "Consulta general")
        user_prompt = f"Servicio detectado: {ctx}\nConsulta del cliente: {prompt}"
        r = _oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": _SYS},
                      {"role": "user", "content": user_prompt}],
            temperature=0.35, max_tokens=220)
        return r.choices[0].message.content.strip()
    except Exception:
        log.exception("GPT error")
        return "Ocurrió un error. ¿Sobre qué servicio te puedo orientar?"



# ── Detección de servicio ─────────────────────────────────────────────────────
_EXACT: dict = {
    "1": "imss", "imss": "imss", "prestamo imss": "imss", "credito imss": "imss",
    "prestamos imss": "imss", "ley 73": "imss",
    "pensionado imss": "imss", "jubilado imss": "imss",
    "2": "auto", "seguro auto": "auto", "seguro de auto": "auto", "seguro carro": "auto",
    "seguros de auto": "auto", "seguro vehiculo": "auto",
    "3": "vida", "seguro vida": "vida", "seguro de vida": "vida", "gastos medicos": "vida",
    "seguro salud": "vida", "seguro medico": "vida", "gastos medicos mayores": "vida",
    "4": "vrim", "vrim": "vrim", "tarjeta medica": "vrim", "consultas medicas": "vrim",
    "5": "emp", "financiamiento empresarial": "emp", "credito empresarial": "emp", "pyme": "emp",
    "6": "fp", "financiamiento practico": "fp", "credito rapido": "fp",
    "financiamiento practico empresarial": "fp",
    "consigue tu credito": "fp", "ctc": "fp", "consigue tu credito ctc": "fp",
}

# Opciones numericas del menu local (1-6): deben resolver de forma
# deterministica via _EXACT/detect_svc y nunca caer en el fallback neutral.
_LOCAL_NUMERIC_OPTIONS = {"1", "2", "3", "4", "5", "6"}

_SEM = [
    ("imss", ["prestamo imss", "credito imss", "ley 73", "jubilado imss", "pensionado imss"]),
    ("auto", ["seguro carro", "seguro auto", "asegurar carro", "asegurar vehiculo", "poliza auto"]),
    ("vida", ["seguro de vida", "gastos medicos", "seguro medico", "cobertura medica", "seguro salud"]),
    ("vrim", ["tarjeta medica", "consultas medicas", "membresia medica", "consultas ilimitadas"]),
    ("fp", ["credito rapido", "24 horas", "aprobacion rapida", "sin garantia empresa", "liquidez"]),
    ("emp", ["credito empresa", "prestamo empresa", "capital trabajo", "financiar negocio", "credito pyme"]),
]

def detect_svc(text: str) -> str | None:
    n = norm(text)
    toks = set(n.split())
    if n in _EXACT:
        return _EXACT[n]

    for svc, kws in _SEM:
        for k in kws:
            nk = norm(k)
            if nk in n:
                return svc
            parts = nk.split()
            if parts and all(p in toks for p in parts):
                return svc

    if ("imss" in toks and ({"prestamo", "prestamos", "credito", "creditos", "pension", "pensionado", "pensionada", "jubilado", "jubilada"} & toks)) or ("ley" in toks and "73" in toks):
        return "imss"

    if ({"seguro", "seguros", "cobertura", "coberturas", "poliza", "polizas"} & toks) and ({"auto", "autos", "carro", "carros", "vehiculo", "vehiculos", "placa", "placas"} & toks):
        return "auto"

    if ({"vida", "gmm", "hospitalizacion", "hospitalario"} & toks) and ({"seguro", "seguros", "salud", "medico", "medicos", "gastos"} & toks):
        return "vida"

    if "vrim" in toks or ({"tarjeta", "membresia", "consultas"} & toks and {"medica", "medicas", "medico", "medicos"} & toks):
        return "vrim"

    if ({"empresa", "empresas", "empresarial", "negocio", "negocios", "pyme", "pymes"} & toks) and ({"credito", "creditos", "financiamiento", "prestamo", "prestamos"} & toks):
        return "emp"

    if ({"practico", "rapido", "rapida", "liquidez", "24", "horas"} & toks) and ({"empresa", "empresarial", "financiamiento", "credito"} & toks):
        return "fp"

    return None

# ── Enrutamiento a servicio ───────────────────────────────────────────────────
def route(phone: str, svc: str) -> None:
    if svc == "imss":
        user_state[phone] = "imss_open"
        user_data.setdefault(phone, {})
        funnel_imss(phone, "")
    elif svc == "emp":
        user_state[phone] = "emp_start"
        user_data.setdefault(phone, {})
        funnel_emp(phone, "")
    elif svc == "fp":
        user_state[phone] = "fp_start"
        user_data.setdefault(phone, {})
        funnel_fp(phone, "")
    elif svc == "auto":
        user_state[phone] = "auto_open"
        user_data.setdefault(phone, {})
        funnel_auto(phone, "")
    elif svc == "vida":
        user_state[phone] = "vida_open"
        user_data.setdefault(phone, {})
        funnel_vida(phone, "")
    elif svc == "vrim":
        user_state[phone] = "vrim_open"
        user_data.setdefault(phone, {})
        funnel_vrim(phone, "")

# ── Calculadora de préstamo IMSS ───────────────────────────────────────────────
# Las constantes financieras (IMSS_TASA_MENSUAL, IMSS_IVA_RATE, IMSS_CAT,
# IMSS_PLAZO_MESES, IMSS_LIMITE_DESCUENTO, IMSS_MONTO_MINIMO,
# IMSS_TASA_ANUAL_SIN_IVA, IMSS_CAT_SIN_IVA, IMSS_PLAZOS_DISPONIBLES) se
# definen arriba, antes de _SYS, por orden de evaluacion del modulo.

# ── Promoción VRIM Plus (campaña IMSS) ─────────────────────────────────────────
# Preelegibilidad preliminar, nunca "aprobado"/"garantizado". El costo de lista
# de VRIM es dato interno -- nunca se menciona aquí.
_IMSS_VRIM_PROMO_MESSAGE = (
    "🎁 *¡Tu propuesta puede darte mucho más que un préstamo!*\n\n"
    "Por el monto estimado, podrías recibir *sin costo una membresía VRIM Plus "
    "durante 12 meses*, sujeta a la formalización del préstamo y a las condiciones "
    "de la promoción.\n\n"
    "Con ella tendrías acceso a beneficios pensados para cuidar tu salud, proteger "
    "tu economía y dar tranquilidad a tu familia:\n\n"
    "🩺 *Atención de emergencias y asistencia médica por teléfono o videoconsulta, "
    "las 24 horas, los 365 días del año.*\n\n"
    "🧠 *Orientación emocional y nutricional por teléfono o videoconsulta.*\n\n"
    "👨‍⚕️ *Dos videoconsultas de especialidad sin costo*, a elegir entre medicina "
    "interna, ginecología o pediatría.\n\n"
    "🚑 *Una ambulancia sin costo al año*, en caso de urgencia real.\n\n"
    "🧪 *Un check-up sin costo*, que incluye química sanguínea de 6 elementos, "
    "biometría hemática y examen general de orina, coordinado en laboratorios "
    "participantes de la red VRIM.\n\n"
    "🛡️ *Reembolso de gastos médicos por accidente de hasta $20,000*, sin deducible "
    "y sin límite de eventos, para personas de *0 a 70 años*.\n\n"
    "⚱️ *Servicio funerario completo, incluyendo cremación*, por fallecimiento "
    "accidental o por enfermedad, para personas de *0 a 70 años cumplidos*. En "
    "enfermedades preexistentes aplica un periodo de espera de 90 días; por "
    "accidente, la cobertura es inmediata.\n\n"
    "*Porque resolver una necesidad económica es importante, pero tener atención "
    "médica disponible, respaldo frente a un accidente y apoyo para tu familia "
    "puede darte tranquilidad durante todo un año.*\n\n"
    "Christian López revisará personalmente tu propuesta y te explicará claramente "
    "cómo funciona cada beneficio.\n\n"
    "*¿Quieres que Christian revise tu caso?*\n\n"
    "1️⃣ Sí, quiero que me contacte\n"
    "2️⃣ No por ahora"
)

# CTA de respaldo: si la burbuja VRIM completa falla al enviarse, el
# prospecto igual debe recibir el CTA 1/2 -- nunca queda sin poder responder.
# Texto conservado tal cual del parche anterior a proposito.
_IMSS_REVISION_CTA_FALLBACK = (
    "¿Quieres que Christian revise tu caso?\n"
    "1. Sí, quiero que me contacte\n"
    "2. No por ahora"
)

# CTA que cierra los mensajes de revision (monto/plazo alternativo). Misma
# semantica 1/2 de siempre; fuente unica para no repetirlo en cada rama.
_IMSS_REVISION_CTA = (
    "¿Quieres que Christian revise si podemos avanzar con esta opción?\n"
    "1️⃣ Sí, quiero que me contacte\n"
    "2️⃣ No por ahora"
)


def _imss_calcular_cuota(monto: float, plazo: int) -> float:
    lo, hi = monto / plazo, monto
    for _ in range(120):
        mid = (lo + hi) / 2
        saldo = monto
        for _m in range(plazo):
            interes = saldo * IMSS_TASA_MENSUAL
            saldo -= mid - interes - interes * IMSS_IVA_RATE
        if abs(saldo) < 0.01:
            return mid
        if saldo > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

def _imss_calcular_monto_maximo(cuota_max: float, plazo: int) -> float:
    lo, hi = 0.0, cuota_max * plazo * 2
    for _ in range(120):
        mid = (lo + hi) / 2
        c = _imss_calcular_cuota(mid, plazo)
        if abs(c - cuota_max) < 0.01:
            return mid
        if c > cuota_max:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2

def calcular_propuesta_imss(pension: float, plazo: int = IMSS_PLAZO_MESES) -> dict:
    cuota_max = pension * IMSS_LIMITE_DESCUENTO
    monto = _imss_calcular_monto_maximo(cuota_max, plazo)
    cuota = _imss_calcular_cuota(monto, plazo)
    return {
        "monto": monto,
        "cuota": cuota,
        "total": cuota * plazo,
        "plazo": plazo,
        "cuota_max": cuota_max,
    }


def _imss_plazos_texto() -> str:
    """'6, 12, ... 54 y 60 meses' derivado de IMSS_PLAZOS_DISPONIBLES -- la
    lista nunca se escribe literal en una plantilla."""
    plazos = [str(p) for p in IMSS_PLAZOS_DISPONIBLES]
    return f"{', '.join(plazos[:-1])} y {plazos[-1]} meses"


def _imss_primer_plazo_para_monto(pension: float, monto_objetivo: float):
    """Plazo mas corto de IMSS_PLAZOS_DISPONIBLES cuyo monto maximo estimado
    alcanza `monto_objetivo` con esa pension. Usa la calculadora vigente; no
    hay tabla hardcodeada de montos. Devuelve (plazo, propuesta) o None si
    ningun plazo disponible lo alcanza."""
    for p in sorted(IMSS_PLAZOS_DISPONIBLES):
        propuesta = calcular_propuesta_imss(pension, p)
        if propuesta["monto"] >= monto_objetivo:
            return p, propuesta
    return None


def _imss_primer_plazo_viable(pension: float):
    """Plazo mas corto que alcanza el minimo del producto ($40,000)."""
    return _imss_primer_plazo_para_monto(pension, IMSS_MONTO_MINIMO)


# ── Propuesta activa: la ULTIMA propuesta valida que el cliente vio ────────────
# Fuente unica para el cierre, la notificacion principal al asesor y cualquier
# resumen posterior. Los campos propuesta_* originales se conservan intactos
# para trazabilidad (base de elegibilidad VRIM, tope de monto solicitado).
# Solo se registra con cifras validas: nunca con un monto bajo el minimo ni con
# un plazo fuera de IMSS_PLAZOS_DISPONIBLES.
def _imss_set_propuesta_activa(data: dict, monto: float, cuota: float,
                               plazo: int, origen: str) -> None:
    data["propuesta_activa_monto"] = monto
    data["propuesta_activa_cuota"] = cuota
    data["propuesta_activa_plazo"] = plazo
    data["propuesta_activa_origen"] = origen


def _imss_get_propuesta_activa(data: dict):
    """(monto, cuota, plazo) de la propuesta activa. Cae a propuesta_* solo si
    nunca se registro una activa (compatibilidad con datos previos)."""
    monto = data.get("propuesta_activa_monto") or data.get("propuesta_monto")
    cuota = data.get("propuesta_activa_cuota") or data.get("propuesta_cuota")
    plazo = (data.get("propuesta_activa_plazo")
             or data.get("propuesta_plazo") or IMSS_PLAZO_MESES)
    return monto, cuota, plazo

# ── Deteccion de intent de propuesta de prestamo IMSS (fuera de estado activo) ─
_IMSS_PROPOSAL_KW = {
    "cuanto me prestan", "cuanto me pueden prestar", "cuanto me dan",
    "con mi pension cuanto alcanzo", "cuanto alcanzo con mi pension",
    "pension cuanto me prestan", "prestamo pension", "pension prestamo",
    "si gano", "cuanto me pueden dar",
}

def _is_imss_loan_proposal_intent(n: str) -> bool:
    if any(k in n for k in _IMSS_PROPOSAL_KW):
        return True
    if "pension" in n and any(k in n for k in ("prestamo", "presta", "dan", "alcanzo", "prestan", "credito")):
        return True
    return False

_IMSS_BARE_NECESITO_RE = re.compile(r"\bnecesito\s+\$?\s*\d")
_IMSS_OTHER_PRODUCT_KW = {
    "empresa", "empresarial", "negocio", "pyme", "auto", "carro", "vehiculo",
    "seguro", "vrim", "tarjeta medica", "ctc", "consigue tu credito", "vida", "gmm",
}

def _is_ambiguous_bare_loan_ask(n: str) -> bool:
    if not _IMSS_BARE_NECESITO_RE.search(n):
        return False
    return not any(k in n for k in _IMSS_OTHER_PRODUCT_KW)

def _imss_revision_choice(msg: str) -> str:
    n = norm(msg).strip()
    if n == "1":
        return "si"
    if n == "2":
        return "no"
    toks = set(n.split())
    if toks & {"quiero", "interesa"}:
        return "si"
    if toks & {"despues", "luego"}:
        return "no"
    return yes_no(msg)

def _imss_ley73_choice(msg: str) -> str:
    n = norm(msg).strip()
    if n in ("1", "2", "3", "4"):
        return n
    toks = set(n.split())
    if "familiar" in toks:
        return "4"
    if "pensionar" in n or "pensionarme" in n:
        return "3"
    r = yes_no(msg)
    if r == "si":
        return "1"
    return "?"

def _imss_route_free_form(phone: str, text: str) -> None:
    """Entrada a la calculadora IMSS fuera de un estado activo (menu, intent
    libre tipo 'cuanto me prestan', o mencion ambigua tipo 'necesito 50000').
    Siempre inicia con la bienvenida + filtro Ley 73 (imss_open), incluso si
    el mensaje ya trae una pension mencionada -- ver Correccion 1."""
    user_data.setdefault(phone, {})
    user_state[phone] = "imss_open"
    funnel_imss(phone, "")

# ── Seguimiento sobre monto/plazo/pago con propuesta activa ────────────────────
_IMSS_FOLLOWUP_KW = {
    "cuanto pagaria", "cuanto pago", "cuanto pagare", "cuanto pagara",
    "me prestan mas", "cuanto me descuentan", "cuanto pagaria al mes",
}

def _is_imss_followup_question(n: str) -> bool:
    if any(k in n for k in _IMSS_FOLLOWUP_KW):
        return True
    if any(k in n for k in ("meses", "plazo")) and any(k in n for k in ("cuanto", "pago", "pagar")):
        return True
    if "quiero" in n.split() and re.search(r"\d", n):
        return True
    if re.search(r"\d", n) and any(k in n for k in ("prestan", "presta", "dan")):
        return True
    return False


# Respuestas escuetas que SOLO cuentan como consulta de seguimiento dentro del
# estado imss_q_revision (nunca de forma global: no afectan nombre, ciudad,
# pension, horario ni ningun otro estado). Toda la heuristica exige digitos,
# para no tragarse mensajes de texto ajenos ("tal vez", "no gracias", etc).
_IMSS_BARE_MONTO_RE = re.compile(r"^\d{1,7}(?:\.\d{1,2})?$")
_IMSS_PLAZO_RE = re.compile(r"(\d{1,3})\s*(?:meses|mes)\b", re.IGNORECASE)

# Notacion coloquial mexicana: "80 mil", "80mil", "80 mil pesos", "80.5 mil".
# \b tras "mil" evita capturar "millones"/"milagro". El multiplicador se acota
# a 4 digitos: "80 mil" es $80,000, pero un numero de 5+ digitos pegado a
# "mil" no es una cantidad plausible del producto.
_IMSS_MIL_RE = re.compile(r"(\d{1,4}(?:\.\d{1,3})?)\s*mil\b", re.IGNORECASE)


def _imss_extract_monto(text: str):
    """extract_num() + notacion coloquial 'N mil'. DELIBERADAMENTE separada de
    extract_num(): esa es global (Auto, Vida, VRIM, Empresarial, CTC) y no se
    toca. Esta solo se usa dentro del funnel IMSS, donde "80 mil" significa
    $80,000 y nunca se extraen telefonos ni horarios."""
    if not text:
        return None
    m = _IMSS_MIL_RE.search(re.sub(r"[$,]", "", text))
    if m:
        try:
            return float(m.group(1)) * 1000
        except Exception:
            pass
    return extract_num(text)


def _is_imss_revision_followup(msg: str) -> bool:
    """Consulta de seguimiento dentro de imss_q_revision. '1' y '2' NUNCA se
    capturan: conservan su significado de aceptar/rechazar la revision de
    Christian."""
    n = norm(msg).strip()
    if n in ("1", "2"):
        return False
    if _IMSS_PLAZO_RE.search(msg):          # 'N mes' / 'N meses' (con o sin monto)
        return True
    if _IMSS_MIL_RE.search(msg):            # '80 mil', '80mil', '80 mil pesos'
        return True
    compacto = re.sub(r"[\s,$]", "", n)     # '$80,000' -> '80000'
    if compacto not in ("1", "2") and _IMSS_BARE_MONTO_RE.match(compacto):
        return True
    return _is_imss_followup_question(n)


def _imss_extract_monto_plazo(text: str):
    """(monto, plazo, plazo_fuera_de_catalogo).

    Un numero seguido de 'mes'/'meses' es SIEMPRE plazo y nunca puede caer en
    la extraccion de monto -- incluso si el plazo esta fuera del catalogo
    ('40 meses' no vale $40). Un numero sin esa palabra es monto ('24' es
    $24, no 24 meses)."""
    plazo = None
    plazo_invalido = None
    m_plazo = _IMSS_PLAZO_RE.search(text)
    if m_plazo:
        try:
            p = int(m_plazo.group(1))
        except Exception:
            p = None
        if p in IMSS_PLAZOS_DISPONIBLES:
            plazo = p
        elif p is not None:
            plazo_invalido = p
    # Se limpia la expresion de plazo del texto SIEMPRE que exista, valida o
    # no, para que ese numero nunca llegue a la validacion de monto minimo.
    texto_sin_plazo = _IMSS_PLAZO_RE.sub(" ", text) if m_plazo else text
    monto = _imss_extract_monto(texto_sin_plazo)
    return monto, plazo, plazo_invalido

_IMSS_CORTESIA_KW = {"gracias", "ok", "okay", "perfecto", "sale", "vale", "genial",
                     "excelente", "listo", "entendido", "va"}
_IMSS_CORTESIA_PHRASES = {"de acuerdo", "esta bien"}
_IMSS_CORTESIA_FILLER = {"tambien", "y", "ademas", "porfavor", "por", "favor", "muchas", "muy", "super"}

def _is_pure_courtesy_message(n_msg: str) -> bool:
    """True solo si el mensaje, quitando cortesia y relleno, no deja nada
    sustantivo -- evita que 'gracias, tambien quiero cotizar auto' se trague
    como cortesia en vez de rutearse como nueva intencion."""
    n_msg = n_msg.strip()
    if not n_msg:
        return False
    working = n_msg
    for phrase in _IMSS_CORTESIA_PHRASES:
        working = working.replace(phrase, " ")
    toks = set(working.split())
    has_courtesy = bool(toks & _IMSS_CORTESIA_KW) or any(p in n_msg for p in _IMSS_CORTESIA_PHRASES)
    if not has_courtesy:
        return False
    remaining = toks - _IMSS_CORTESIA_KW - _IMSS_CORTESIA_FILLER
    return len(remaining) == 0

# ── Horario de contacto (funnel IMSS) ─────────────────────────────────────────
# Horario comercial real: lunes a sabado, 9:00 a 18:00, hora de Sinaloa.
# Zona horaria PROPIA de este bloque: _TZ / now_mx() siguen en
# America/Mexico_City para los timestamps generales del sistema (logs, Sheets)
# y NO se tocan aqui. Lo unico que se evalua en America/Mazatlan es que
# opciones de horario se le muestran al cliente en el cierre IMSS.
try:
    _IMSS_TZ_COMERCIAL = pytz.timezone("America/Mazatlan")
except Exception:
    # Sinaloa opera en UTC-7 todo el año (sin horario de verano desde 2022).
    _IMSS_TZ_COMERCIAL = timezone(timedelta(hours=-7))

_IMSS_CIERRE_COMERCIAL_HORA = 18          # a partir de las 6:00 p.m. ya no hay "hoy"
_IMSS_SABADO = 5                          # datetime.weekday(): 0=lunes ... 6=domingo
_IMSS_DOMINGO = 6
_IMSS_OTRO_HORARIO_LABEL = "Otro día y horario específico"


def _imss_ahora_comercial() -> datetime:
    return datetime.now(_IMSS_TZ_COMERCIAL)


def _imss_build_horario_opciones(ahora: datetime | None = None) -> dict:
    """Las TRES etiquetas que se le muestran al cliente, calculadas contra el
    horario comercial real (lunes a sabado 9:00-18:00, America/Mazatlan).

    A) lunes a sabado antes de las 18:00 -> hoy por la tarde + siguiente dia habil.
    B) lunes a viernes desde las 18:00   -> mañana (mañana/tarde) + otro horario.
    C) sabado desde las 18:00 y domingo  -> lunes (mañana/tarde) + otro horario.

    El domingo nunca es opcion, y el lunes nunca se llama "Mañana" cuando el
    mensaje llega en fin de semana."""
    ahora = ahora or _imss_ahora_comercial()
    dia = ahora.weekday()
    antes_del_cierre = ahora.hour < _IMSS_CIERRE_COMERCIAL_HORA

    # C) Fin de semana cerrado: sabado ya sin tarde util, o domingo completo.
    if dia == _IMSS_DOMINGO or (dia == _IMSS_SABADO and not antes_del_cierre):
        return {"1": "Lunes por la mañana",
                "2": "Lunes por la tarde",
                "3": _IMSS_OTRO_HORARIO_LABEL}

    # B) Entre semana ya cerrado: mañana (martes..sabado) si es habil.
    if not antes_del_cierre:
        return {"1": "Mañana por la mañana",
                "2": "Mañana por la tarde",
                "3": _IMSS_OTRO_HORARIO_LABEL}

    # A) Dentro del horario comercial. El sabado, "mañana" seria domingo:
    # el siguiente dia habil real es lunes y se nombra explicitamente.
    siguiente = "Lunes" if dia == _IMSS_SABADO else "Mañana"
    return {"1": "Hoy por la tarde",
            "2": f"{siguiente} por la mañana",
            "3": f"{siguiente} por la tarde"}


def _imss_horarios_ofrecidos(data: dict) -> dict:
    """Las etiquetas REALMENTE mostradas en el cierre de esta conversacion.
    Nunca se recalculan al interpretar la respuesta: si el cliente contesta
    horas despues, se resuelve contra lo que vio. El fallback solo cubre datos
    previos al parche o llamadas directas al constructor del cierre."""
    ofrecidos = data.get("imss_horarios_ofrecidos")
    if isinstance(ofrecidos, dict) and {"1", "2", "3"} <= set(ofrecidos):
        return ofrecidos
    return _imss_build_horario_opciones()


def _imss_normalize_horario(msg: str, opciones: dict):
    """Resuelve la respuesta contra las etiquetas persistidas de ESE turno,
    nunca contra un mapeo global fijo.

    Devuelve la etiqueta elegida, `None` si el cliente escogio "otro dia y
    horario especifico" (hay que pedirle el texto libre), o el texto tal cual
    si escribio un horario libre."""
    n = norm(msg).strip()
    if n in opciones:
        etiqueta = opciones[n]
        return None if etiqueta == _IMSS_OTRO_HORARIO_LABEL else etiqueta
    for opcion, etiqueta in opciones.items():
        if etiqueta == _IMSS_OTRO_HORARIO_LABEL:
            continue
        n_etiqueta = norm(etiqueta)
        if n in (n_etiqueta, f"{opcion} {n_etiqueta}") or n_etiqueta in n:
            return etiqueta
    return msg.strip()[:200]


def _imss_close(phone: str, tipo: str = "generico", data: dict | None = None) -> None:
    """Cierra un tramo terminal del funnel IMSS pero deja un estado corto
    (imss_post_cierre) para poder responder cortesia ('gracias'/'ok'/etc)
    sin caer en el fallback neutral de Boardroom. 'tipo' distingue el cierre
    exitoso (con notificacion al asesor ya enviada) de los demas, para dar
    la cortesia correcta sin volver a notificar.

    'data', si se pasa, se conserva integro (nombre, ciudad, pension,
    propuesta_*, monto_solicitado, origen/referral_*, vrim_*,
    advisor_notify_ok, horario_contacto) en vez de descartarse -- solo se le
    agrega/actualiza 'cierre_tipo'. Sin 'data' el comportamiento es identico
    al de siempre (dict minimo), para no afectar los cierres tempranos que
    no tienen datos comerciales que conservar."""
    reset(phone)
    user_state[phone] = "imss_post_cierre"
    if data is not None:
        preserved = dict(data)
        preserved["cierre_tipo"] = tipo
        user_data[phone] = preserved
    else:
        user_data[phone] = {"cierre_tipo": tipo}


# ── Cierre comercial determinista (plantilla + datos capturados, sin IA) ──────
# Nunca menciona para que usara el prospecto el dinero: ese dato no se
# pregunta, no se infiere y no se persiste en ningun campo del funnel IMSS.
def _imss_build_closing_statement(data: dict) -> str:
    """Cierre comercial + pregunta de horario en UNA sola burbuja. Fuente
    unica del cierre determinista: no existe otra construccion del mismo
    mensaje ni una version anterior viva en otra ruta.

    Usa SIEMPRE la propuesta activa (la ultima propuesta valida que el cliente
    vio), nunca la propuesta inicial por defecto."""
    nombre = str(data.get("nombre") or "").strip()
    primer_nombre = nombre.split()[0] if nombre else ""
    pension = data.get("pension")
    monto, cuota, plazo = _imss_get_propuesta_activa(data)

    encabezado = (f"✅ *Listo, {primer_nombre}. Ya tenemos una propuesta estimada para ti.*"
                  if primer_nombre else
                  "✅ *Listo. Ya tenemos una propuesta estimada para ti.*")
    partes = [encabezado]

    if pension and monto and cuota:
        partes.append(
            f"Con una pensión aproximada de *${pension:,.0f} al mes*, podrías obtener "
            f"alrededor de *${monto:,.0f}*, con un pago estimado de *${cuota:,.0f} "
            f"mensuales durante {plazo} meses*."
        )

    # Referencia BREVE a VRIM: no repite coberturas ni vuelve a pedir la
    # aceptacion de la promocion. Se omite por completo (sin dejar huecos ni
    # saltos de linea sobrantes) cuando no hay preelegibilidad.
    if data.get("vrim_preeligible"):
        partes.append(
            "Además, por el monto de tu propuesta, podrías recibir *sin costo una "
            "membresía VRIM Plus por 12 meses*, sujeta a formalización y a las "
            "condiciones de la promoción."
        )

    partes.append(
        "*Christian López revisará personalmente tu caso*, validará las opciones "
        "disponibles y te explicará cuál puede ajustarse mejor a lo que necesitas."
    )
    # Las tres opciones salen del horario comercial vigente y ya quedaron
    # persistidas en user_data: lo que se pinta aqui es lo mismo que despues
    # interpreta la respuesta 1/2/3.
    opciones = _imss_horarios_ofrecidos(data)
    bloque_horario = ("📞 *¿Cuándo prefieres que te llame Christian?*\n\n"
                      f"1️⃣ {opciones['1']}\n"
                      f"2️⃣ {opciones['2']}\n"
                      f"3️⃣ {opciones['3']}")
    # Cuando la opcion 3 ya es "otro dia y horario", no se repite la invitacion.
    if opciones["3"] != _IMSS_OTRO_HORARIO_LABEL:
        bloque_horario += ("\n\nTambién puedes indicar *otro día y horario específico*, "
                           "por ejemplo:\n“El jueves a las 10:00 a. m.”")
    partes.append(bloque_horario)
    return "\n\n".join(partes)


def _imss_build_advisor_notification(phone: str, data: dict) -> str:
    lines = ["📣 PROSPECTO IMSS CALIFICADO — LLAMAR", "",
             "Producto: Préstamo IMSS pensionados"]
    if data.get("nombre"):
        lines.append(f"Nombre: {data['nombre']}")
    lines.append(f"WhatsApp: {phone}")
    if data.get("ciudad"):
        lines.append(f"Ciudad: {data['ciudad']}")
    if data.get("origen"):
        lines.append(f"Origen: {data['origen']}")
    if data.get("referral_headline"):
        lines.append(f"Headline anuncio: {data['referral_headline']}")
    if data.get("referral_ad_id"):
        lines.append(f"Ad ID: {data['referral_ad_id']}")
    if data.get("referral_campaign_id"):
        lines.append(f"Campaign ID: {data['referral_campaign_id']}")
    if data.get("pension"):
        lines.append(f"Pensión mensual: ${data['pension']:,.0f}")
    # Propuesta ACTIVA: exactamente la misma que vio el cliente en el cierre.
    monto_activo, cuota_activa, plazo_activo = _imss_get_propuesta_activa(data)
    if monto_activo:
        lines.append(f"Monto estimado: ${monto_activo:,.0f}")
    if data.get("monto_solicitado"):
        lines.append(f"Monto solicitado por cliente: ${data['monto_solicitado']:,.0f}")
    if cuota_activa:
        lines.append(f"Cuota estimada: ${cuota_activa:,.0f}")
    if plazo_activo:
        lines.append(f"Plazo: {plazo_activo} meses")
    if data.get("propuesta_activa_origen"):
        lines.append(f"Origen de la propuesta activa: {data['propuesta_activa_origen']}")
    # Trazabilidad: la propuesta inicial se conserva y solo se reporta cuando
    # difiere de la activa, para que el asesor vea de donde partio el caso.
    if data.get("propuesta_monto") and monto_activo and data["propuesta_monto"] != monto_activo:
        lines.append(f"Propuesta inicial (referencia): ${data['propuesta_monto']:,.0f} "
                     f"a {data.get('propuesta_plazo', IMSS_PLAZO_MESES)} meses")

    basis = data.get("vrim_eligibility_basis")
    if basis:
        lines.append(f"Base de elegibilidad VRIM: {basis}")
    lines.append(f"VRIM preelegible: {'Sí' if data.get('vrim_preeligible') else 'No'}")
    lines.append(f"Promoción VRIM presentada: {'Sí' if data.get('vrim_offered') else 'No'}")
    lines.append(f"Interés del cliente en VRIM: {data.get('vrim_interest', 'sin_respuesta')}")
    lines.append("⚠️ Verificar edad: las coberturas de reembolso de gastos médicos "
                 "por accidente y servicio funerario aplican hasta los 70 años.")
    lines.append(f"Estado del funnel: {user_state.get(phone, 'ND')}")
    lines.append("")
    lines.append("Resumen: Cliente solicitó cálculo de préstamo IMSS. Vicky generó una "
                 "propuesta estimada usando la calculadora existente. Requiere revisión "
                 "manual antes de prometer condiciones. Recomendación: llamar.")
    return "\n".join(lines)


def _imss_backup_num(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:,.0f}"
    return "ND"


def _imss_backup_field(v, cap: int) -> str:
    s = str(v) if v not in (None, "") else "ND"
    return s[:cap]


def _imss_log_lead_backup(phone: str, data: dict, resultado: str = "advisor_notify_failed") -> None:
    """Respaldo del lead en Google Sheets cuando falla notify_advisor() o
    cuando el CTA (VRIM + fallback) no pudo entregarse, reutilizando _log()
    y las columnas existentes (sin crear hoja, columna ni sistema nuevo).

    _log() aplica str(msg)[:500] -- no se depende de ese truncado ciego: el
    resumen se construye ya acotado. Los campos criticos de negocio
    (advisor_notify_ok, whatsapp, nombre, pension, propuesta_*,
    vrim_preeligible, vrim_offered) van PRIMERO y con longitud acotada
    individualmente, para que sobrevivan aunque nombre/ciudad/origen sean
    inusualmente largos. ciudad/origen van al final, tambien acotados.
    Identificable por tipo='respaldo_lead' + 'resultado' (distingue fallo de
    notificacion al asesor de fallo de entrega del CTA).

    Limitacion documentada: es una fila de texto plano, no columnas
    estructuradas -- no se puede filtrar/ordenar en Sheets por
    vrim_preeligible o propuesta_monto sin parsear el texto. Suficiente para
    recuperar manualmente el lead completo, insuficiente para reportes
    tabulares automatizados sobre este respaldo especifico.
    """
    nombre_corto = _imss_backup_field(data.get("nombre", "ND"), 60)
    # Mismas columnas y mismo orden de siempre, pero con la propuesta ACTIVA:
    # el lead recuperado a mano debe coincidir con lo que el cliente vio.
    monto_activo, cuota_activa, plazo_activo = _imss_get_propuesta_activa(data)
    resumen = (
        "RESPALDO_LEAD_IMSS"
        f" | advisor_notify_ok={data.get('advisor_notify_ok', 'ND')}"
        f" | whatsapp={phone}"
        f" | nombre={nombre_corto}"
        f" | pension={_imss_backup_num(data.get('pension'))}"
        f" | propuesta_monto={_imss_backup_num(monto_activo)}"
        f" | propuesta_cuota={_imss_backup_num(cuota_activa)}"
        f" | propuesta_plazo={_imss_backup_num(plazo_activo)}"
        f" | vrim_preeligible={data.get('vrim_preeligible', False)}"
        f" | vrim_offered={data.get('vrim_offered', False)}"
        f" | ciudad={_imss_backup_field(data.get('ciudad', 'ND'), 40)}"
        f" | origen={_imss_backup_field(data.get('origen', 'ND'), 60)}"
    )[:500]
    _log(phone, nombre_corto, resumen, "respaldo_lead", "sistema",
         resultado=resultado, error="", mid=_mid())


# ── Flujo IMSS ────────────────────────────────────────────────────────────────
def funnel_imss(phone: str, msg: str) -> None:
    state = user_state.get(phone, "imss_open")
    data = user_data.get(phone, {})

    if state == "imss_filtro":
        r = yes_no(msg)
        if r == "si":
            data["origen"] = "interes_filtrado_IMSS"
            user_data[phone] = data
            send_msg(phone, "Perfecto 👏\n*¿Cuánto recibes al mes por concepto de pensión?* _(ej. 7500)_")
            user_state[phone] = "imss_q_pension_calc"
        elif r == "no":
            send_msg(phone,
                "Entendido 🙏 El préstamo IMSS Ley 73 aplica para pensionados de ese régimen.\n\n"
                "¿Te gustaría que un asesor te oriente sobre otras opciones disponibles?")
            user_state[phone] = "imss_no_califica"
        else:
            send_msg(phone, "Por favor responde *sí* o *no*. 😊")
        return

    if state == "imss_open":
        send_msg(phone,
            "👋 *¡Hola! Soy Vicky, asistente de Christian López.*\n\n"
            "Estoy aquí para ayudarte a conocer, de manera rápida y sencilla, cuánto "
            "podrías obtener con un *préstamo para pensionados IMSS*. 💰\n\n"
            "Prepararé una propuesta estimada de acuerdo con tu pensión y también "
            "podemos revisar más opciones por si tienes en mente algún monto o plazo "
            "específico.\n\n"
            "Para comenzar, selecciona cuál opción corresponde a tu caso:\n\n"
            "1️⃣ Ya estoy pensionado por IMSS Ley 73\n"
            "2️⃣ Estoy pensionado, pero no sé si soy Ley 73\n"
            "3️⃣ Estoy por pensionarme\n"
            "4️⃣ Estoy ayudando a un familiar")
        user_state[phone] = "imss_q_ley73"
        return

    if state == "imss_q_ley73":
        n_ley73 = norm(msg)
        if any(k in n_ley73 for k in ("asesor", "humano", "persona real")):
            notify_advisor(
                f"📣 SOLICITA ASESOR – IMSS Ley 73 (menú no resuelto)\n"
                f"WhatsApp: {phone}\n"
                f"Último mensaje: {msg[:200]}"
            )
            send_msg(phone, "Claro 🙌 Le aviso a *Christian López* para que te contacte directamente.")
            _imss_close(phone)
            return
        r = _imss_ley73_choice(msg)
        if r in ("1", "2"):
            data.pop("ley73_intentos_invalidos", None)
            data["ley73_estatus"] = "pensionado_ley73" if r == "1" else "pensionado_sin_confirmar_ley73"
            data["relacion"] = "titular"
            user_data[phone] = data
            send_msg(phone, "Perfecto 👏\n*¿Cuánto recibes al mes por concepto de pensión?* _(ej. 7500)_")
            user_state[phone] = "imss_q_pension_calc"
        elif r == "3":
            data.pop("ley73_intentos_invalidos", None)
            notify_advisor(f"📣 INTERÉS FUTURO – IMSS (por pensionarse)\nWhatsApp: {phone}")
            send_msg(phone,
                "Entendido 🙏 Para calcular una propuesta necesitamos que la pensión ya esté activa.\n\n"
                "Aun así, si gustas, *Christian* puede revisar tu caso para cuando te pensiones.")
            _imss_close(phone)
        elif r == "4":
            data.pop("ley73_intentos_invalidos", None)
            data["relacion"] = "familiar"
            user_data[phone] = data
            send_msg(phone, "Entendido 👍\n*¿Cuánto recibe al mes de pensión tu familiar?* _(ej. 7500)_")
            user_state[phone] = "imss_q_pension_calc"
        else:
            # Sin esto, un prospecto que no entiende el menu numerado se queda
            # atrapado indefinidamente en el mismo mensaje (hallazgo real
            # 2026-08-04, lead 5214521126115: 8 intentos sin salida ni aviso
            # al asesor). Al segundo intento invalido, se aclara la pregunta
            # con ejemplos y se avisa una sola vez al asesor -- no en cada
            # intento repetido, para no saturarlo.
            intentos = int(data.get("ley73_intentos_invalidos", 0)) + 1
            data["ley73_intentos_invalidos"] = intentos
            user_data[phone] = data
            if intentos == 2:
                notify_advisor(
                    f"⚠️ PROSPECTO ATASCADO – IMSS Ley 73\n"
                    f"WhatsApp: {phone}\n"
                    f"No entiende el menú numérico. Último mensaje: {msg[:200]}"
                )
            if intentos >= 2:
                send_msg(phone,
                    "Escribe solo el número de tu opción, sin palabras:\n\n"
                    "Escribe *1* si ya recibes tu pensión del IMSS.\n"
                    "Escribe *2* si estás pensionado pero no sabes si es Ley 73.\n"
                    "Escribe *3* si todavía no te pensionas.\n"
                    "Escribe *4* si estás preguntando por un familiar.\n\n"
                    "Si prefieres, dime *asesor* y Christian López te contacta directamente.")
            else:
                send_msg(phone, "Por favor responde *1*, *2*, *3* o *4*.")
        return

    if state == "imss_no_califica":
        r = yes_no(msg)
        if r == "si":
            notify_advisor(
                f"📣 NO CALIFICA – IMSS LEY 73\n"
                f"WhatsApp: {phone}\n"
                f"Origen: {data.get('origen', 'directo')}\n"
                "Solicita orientación sobre otras alternativas.")
            send_msg(phone, "¡Perfecto! 👍 Le aviso a nuestro asesor *Christian López* "
                            "para que te contacte a la brevedad.")
        else:
            send_msg(phone, "¡Cuando gustes consultar, aquí estaremos! 😊")
        _imss_close(phone)
        return

    if state == "imss_q_pension_calc":
        # Mismo reconocimiento de "mil" que en la revision: "7 mil" es una
        # pension de $7,000, no de $7. Usa el helper acotado al funnel IMSS;
        # extract_num() global sigue intacto para los demas productos.
        m = _imss_extract_monto(msg)
        if m is None or m <= 0 or m > 200000:
            send_msg(phone, "Para calcularlo bien, dime cuánto recibes al mes de pensión IMSS. Ejemplo: 12000")
            return
        data["pension"] = m
        user_data[phone] = data

        propuesta = calcular_propuesta_imss(m)
        if propuesta["monto"] < IMSS_MONTO_MINIMO:
            send_msg(phone,
                "Gracias 🙏 Con esa pensión, el monto estimado queda por debajo de nuestro mínimo "
                "de *$40,000*.\n\n¿Deseas que un asesor te contacte para explorar otras opciones?")
            user_state[phone] = "imss_pension_baja"
            return

        data["propuesta_monto"] = propuesta["monto"]
        data["propuesta_cuota"] = propuesta["cuota"]
        data["propuesta_plazo"] = propuesta["plazo"]
        data["propuesta_total"] = propuesta["total"]
        # La propuesta inicial valida es, desde este momento, la propuesta activa.
        _imss_set_propuesta_activa(data, propuesta["monto"], propuesta["cuota"],
                                   propuesta["plazo"], "propuesta_inicial")
        # Preelegibilidad VRIM: una vez establecida en True, nunca se degrada
        # a False (ver _imss_extract_monto_plazo / reglas de jerarquía en
        # imss_q_revision). Umbral inclusivo >= IMSS_MONTO_MINIMO, que por
        # diseño es el mismo mínimo del producto -- toda propuesta que llega
        # aquí ya califica.
        if propuesta["monto"] >= IMSS_MONTO_MINIMO and not data.get("vrim_preeligible"):
            data["vrim_preeligible"] = True
            data["vrim_eligibility_basis"] = "propuesta_monto"
        user_data[phone] = data

        # Alerta temprana: el asesor se enteraba de un lead calificado solo
        # hasta que aceptaba la revision y daba nombre+ciudad (mas abajo,
        # imss_q_ciudad_calc). Un lead con propuesta real (ej. $90,226) que
        # nunca contesta esa pregunta quedaba invisible -- hallazgo real
        # 2026-08-04, lead 5216681693152. Esta alerta es adicional, no
        # sustituye a la de calificacion completa.
        notify_advisor(
            "📊 PROPUESTA CALCULADA – IMSS Ley 73 (pendiente de confirmar)\n"
            f"WhatsApp: {phone}\n"
            f"Pensión: ${m:,.0f}\n"
            f"Propuesta: ${propuesta['monto']:,.0f} / ${propuesta['cuota']:,.0f} al mes / "
            f"{propuesta['plazo']} meses\n"
            "Aún no confirma revisión — puede requerir seguimiento si no responde."
        )

        # Solo condiciones SIN IVA en este mensaje (nunca IMSS_CAT, que es el
        # criterio con IVA de uso interno). Ambas cifras salen de las
        # constantes -- no se hardcodean en la plantilla.
        send_msg(phone,
            "🎉 *¡Tenemos una propuesta para ti!*\n\n"
            f"Con una pensión mensual aproximada de *${m:,.0f}*, podrías obtener una "
            "propuesta estimada como esta:\n\n"
            f"💰 *Monto aproximado:* *${propuesta['monto']:,.0f}*\n"
            f"💳 *Pago aproximado:* *${propuesta['cuota']:,.0f} al mes*\n"
            f"📆 *Plazo:* *{propuesta['plazo']} meses*\n"
            f"📈 *Tasa fija anual:* *{IMSS_TASA_ANUAL_SIN_IVA:.2f}% sin IVA*\n"
            f"📊 *CAT informativo:* *{IMSS_CAT_SIN_IVA:.1f}% sin IVA*\n\n"
            "_Esta propuesta es informativa y está sujeta a validación final._\n\n"
            "También podemos revisar *otras opciones de monto o plazo* para encontrar "
            "una alternativa que se adapte mejor a lo que necesitas.")

        # Burbuja VRIM separada, inmediatamente despues del resultado. El CTA
        # 1/2 vive aqui (no en el mensaje de propuesta) porque siempre que se
        # llega a este punto la propuesta ya califica (ver comentario arriba).
        # El estado solo avanza a imss_q_revision si el prospecto realmente
        # recibio un CTA (VRIM o el fallback) -- si ambos envios fallan, no
        # lo dejamos "esperando" una pregunta 1/2 que nunca vio.
        cta_delivered = True
        if data.get("vrim_preeligible") and not data.get("vrim_offered"):
            cta_delivered = False
            vrim_sent_ok = send_msg(phone, _IMSS_VRIM_PROMO_MESSAGE)
            if vrim_sent_ok:
                data["vrim_offered"] = True
                data["vrim_offer_timestamp"] = datetime.now(timezone.utc).isoformat()
                user_data[phone] = data
                cta_delivered = True
            else:
                # La burbuja VRIM completa fallo: intentar el CTA de
                # respaldo. NUNCA se marca vrim_offered=True aqui (para que,
                # si hay una oportunidad futura, se pueda reintentar la
                # oferta completa).
                log.error("imss_vrim_bubble_send_failed phone_last4=%s", phone[-4:])
                fallback_sent_ok = send_msg(phone, _IMSS_REVISION_CTA_FALLBACK)
                if fallback_sent_ok:
                    cta_delivered = True
                else:
                    # Doble fallo: ni la burbuja VRIM ni el CTA de respaldo
                    # llegaron. user_data se conserva intacto; se registra el
                    # doble fallo en el respaldo existente de Sheets; el
                    # estado NO avanza a imss_q_revision (no hay CTA que
                    # responder). Queda en un estado local recuperable que
                    # reintenta el CTA de respaldo ante el siguiente mensaje
                    # del prospecto, sin ir a Boardroom ni reiniciar el
                    # funnel.
                    log.error("imss_cta_fallback_send_failed phone_last4=%s", phone[-4:])
                    _imss_log_lead_backup(phone, data, resultado="cta_send_failed")

        user_state[phone] = "imss_q_revision" if cta_delivered else "imss_cta_pendiente"
        return

    if state == "imss_cta_pendiente":
        # Estado recuperable: el prospecto nunca recibio el CTA (fallo doble
        # de VRIM + fallback). Cualquier mensaje suyo reintenta UNA vez el
        # CTA de respaldo -- reintento acotado por turno del usuario, nunca
        # un bucle ni reenvios ilimitados. user_data no se toca.
        retry_ok = send_msg(phone, _IMSS_REVISION_CTA_FALLBACK)
        if retry_ok:
            user_state[phone] = "imss_q_revision"
        else:
            log.error("imss_cta_fallback_retry_failed phone_last4=%s", phone[-4:])
        return

    if state == "imss_pension_baja":
        if yes_no(msg) == "si":
            notify_advisor(f"🔔 PENSIÓN BAJA – IMSS\nWhatsApp: {phone}\n"
                           f"Pensión: ${data.get('pension', 'ND')}\n"
                           f"Origen: {data.get('origen', 'directo')}")
            send_msg(phone, "✅ ¡Listo! Un asesor te contactará con opciones para tu situación.")
        else:
            send_msg(phone, "Entendido 😊 Aquí estamos cuando lo necesites.")
        _imss_close(phone)
        return

    if state == "imss_q_revision":
        pension = data.get("pension", 0)

        if _is_imss_revision_followup(msg):
            monto_req, plazo_req, plazo_invalido = _imss_extract_monto_plazo(msg)
            if plazo_invalido is not None and plazo_req is None:
                # Plazo fuera del catalogo del cotizador ("40 meses", "72
                # meses"): se listan los plazos vigentes. Ese numero NUNCA
                # llega a la validacion de monto minimo y la propuesta activa
                # no se toca. El estado sigue siendo imss_q_revision, asi que
                # 1/2 conservan su significado.
                send_msg(phone,
                    f"Por ahora manejamos estos plazos para el Préstamo IMSS Ley 73:\n\n"
                    f"*{_imss_plazos_texto()}*\n\n"
                    "Escríbeme el que quieras revisar, por ejemplo: *36 meses*.")
                return
            if monto_req:
                # H-05: persistir siempre el monto que pide el prospecto,
                # sea o no viable -- es el dato comercial mas valioso de la
                # conversacion.
                data["monto_solicitado"] = monto_req
                propuesta_monto = data.get("propuesta_monto", 0)

                if monto_req < IMSS_MONTO_MINIMO:
                    # No viable: el prestamo no existe a ese monto. Nunca se
                    # degrada vrim_preeligible/basis (ya establecidos en
                    # propuesta_monto) y no se reenvia la oferta VRIM.
                    user_data[phone] = data
                    send_msg(phone,
                        "El monto mínimo del Préstamo IMSS Ley 73 es de *$40,000*.\n\n"
                        "¿Te gustaría que revisemos tu caso a partir de esa cifra?")
                    return

                # El tope se evalua SIEMPRE contra el plazo que se va a
                # cotizar, no contra el maximo a 60 meses: un monto que cabe a
                # 60 meses puede no caber a 24 y produciria una cuota por
                # encima del limite de descuento del 30%.
                plazo_calc = plazo_req or _imss_get_propuesta_activa(data)[2]
                monto_max_plazo = calcular_propuesta_imss(pension, plazo_calc)["monto"]

                if monto_req > monto_max_plazo:
                    # No cabe en ese plazo. Se conserva el monto que pidio el
                    # cliente y se busca el plazo mas corto donde SI cabe,
                    # igual que en la ruta de plazo no viable.
                    alternativa = _imss_primer_plazo_para_monto(pension, monto_req)
                    if alternativa is not None:
                        plazo_alt, _propuesta_alt = alternativa
                        cuota_alt = _imss_calcular_cuota(monto_req, plazo_alt)
                        data["vrim_eligibility_basis"] = "monto_solicitado"
                        _imss_set_propuesta_activa(
                            data, monto_req, cuota_alt, plazo_alt,
                            "monto_solicitado_plazo_ajustado")
                        user_data[phone] = data
                        send_msg(phone,
                            f"Con tu pensión de *${pension:,.0f}*, un préstamo de "
                            f"*${monto_req:,.0f}* a *{plazo_calc} meses* dejaría un pago "
                            "mensual por encima del descuento máximo permitido "
                            f"(*${pension * IMSS_LIMITE_DESCUENTO:,.0f}* al mes).\n\n"
                            f"Ese mismo monto sí es viable a *{plazo_alt} meses*:\n\n"
                            f"💰 *Monto aproximado:* *${monto_req:,.0f}*\n"
                            f"💳 *Pago aproximado:* *${cuota_alt:,.0f} al mes*\n"
                            f"📆 *Plazo:* *{plazo_alt} meses*\n\n"
                            "_Esta propuesta es informativa y está sujeta a validación final._\n\n"
                            + _IMSS_REVISION_CTA)
                        return

                    # No cabe en ningun plazo del catalogo: se muestra el
                    # maximo global (60 meses) como referencia, sin recalcular
                    # VRIM a la baja. Esa cifra SI es una propuesta valida,
                    # asi que pasa a ser la activa.
                    data["vrim_eligibility_basis"] = "propuesta_monto"
                    _imss_set_propuesta_activa(
                        data, propuesta_monto, data.get("propuesta_cuota", 0),
                        data.get("propuesta_plazo", IMSS_PLAZO_MESES), "propuesta_maxima")
                    user_data[phone] = data
                    send_msg(phone,
                        f"Con tu pensión de *${pension:,.0f}*, el monto máximo estimado que "
                        f"podemos ofrecerte es de *${propuesta_monto:,.0f}*, con un pago "
                        f"aproximado de *${data.get('propuesta_cuota', 0):,.0f}* al mes a "
                        f"{data.get('propuesta_plazo', IMSS_PLAZO_MESES)} meses.\n\n"
                        "_Esta información es estimada y está sujeta a validación final._\n\n"
                        + _IMSS_REVISION_CTA)
                    return

                # Viable: IMSS_MONTO_MINIMO <= monto_req <= monto_max_plazo.
                data["vrim_eligibility_basis"] = "monto_solicitado"
                cuota = _imss_calcular_cuota(monto_req, plazo_calc)
                _imss_set_propuesta_activa(
                    data, monto_req, cuota, plazo_calc,
                    "monto_y_plazo_solicitados" if plazo_req else "monto_solicitado")
                user_data[phone] = data
                send_msg(phone,
                    f"Con una pensión mensual de *${pension:,.0f}*, el descuento máximo estimado "
                    f"sería de *${pension * IMSS_LIMITE_DESCUENTO:,.0f}* al mes.\n\n"
                    f"Para un préstamo de *${monto_req:,.0f}* a *{plazo_calc} meses*, el pago "
                    f"aproximado sería de *${cuota:,.0f}* al mes.\n\n"
                    "_Esta información es estimada y está sujeta a validación final._\n\n"
                    + _IMSS_REVISION_CTA)
            elif plazo_req:
                propuesta = calcular_propuesta_imss(pension, plazo_req)
                if propuesta["monto"] < IMSS_MONTO_MINIMO:
                    # El plazo pedido no alcanza el minimo del producto: nunca
                    # se muestra la cifra invalida ni se ofrece un prestamo
                    # menor a $40,000. Se calcula y se muestra DIRECTAMENTE la
                    # alternativa viable mas corta, sin abrir estado
                    # intermedio y sin una pregunta previa que "1"/"2" pudieran
                    # confundir con el CTA.
                    viable = _imss_primer_plazo_viable(pension)
                    if viable is None:
                        # Ningun plazo disponible llega al minimo: salida
                        # segura de pension baja. No se inventa propuesta ni se
                        # sobrescribe la propuesta activa vigente.
                        send_msg(phone,
                            "Gracias 🙏 Con esa pensión, el monto estimado queda por debajo de "
                            "nuestro mínimo de *$40,000* en todos los plazos disponibles.\n\n"
                            "¿Deseas que un asesor te contacte para explorar otras opciones?")
                        user_state[phone] = "imss_pension_baja"
                        return
                    plazo_viable, propuesta_viable = viable
                    _imss_set_propuesta_activa(
                        data, propuesta_viable["monto"], propuesta_viable["cuota"],
                        plazo_viable, "plazo_viable_automatico")
                    user_data[phone] = data
                    send_msg(phone,
                        f"Con tu pensión, a *{plazo_req} meses* el monto estimado quedaría por "
                        "debajo del mínimo de *$40,000*.\n\n"
                        "La opción disponible con el plazo más corto sería:\n\n"
                        f"💰 *Monto aproximado:* *${propuesta_viable['monto']:,.0f}*\n"
                        f"💳 *Pago aproximado:* *${propuesta_viable['cuota']:,.0f} al mes*\n"
                        f"📆 *Plazo:* *{plazo_viable} meses*\n\n"
                        "_Esta propuesta es informativa y está sujeta a validación final._\n\n"
                        + _IMSS_REVISION_CTA)
                    return
                _imss_set_propuesta_activa(
                    data, propuesta["monto"], propuesta["cuota"], plazo_req,
                    "plazo_solicitado")
                user_data[phone] = data
                send_msg(phone,
                    f"A *{plazo_req} meses*, con tu pensión de *${pension:,.0f}*, el monto "
                    f"aproximado sería de *${propuesta['monto']:,.0f}* con un pago aproximado "
                    f"de *${propuesta['cuota']:,.0f}* al mes.\n\n"
                    "_Esta información es estimada y está sujeta a validación final._\n\n"
                    + _IMSS_REVISION_CTA)
            else:
                # Resumen sin cifras nuevas: repite la propuesta ACTIVA, no la
                # inicial -- no puede contradecir el ultimo mensaje visible.
                monto_act, cuota_act, plazo_act = _imss_get_propuesta_activa(data)
                send_msg(phone,
                    f"Con tu pensión de *${pension:,.0f}*, el monto aproximado sigue siendo "
                    f"*${monto_act or 0:,.0f}* con un pago aproximado de "
                    f"*${cuota_act or 0:,.0f}* al mes a {plazo_act} meses.\n\n"
                    + _IMSS_REVISION_CTA)
            return

        r = _imss_revision_choice(msg)
        if r == "si":
            data["desea_revision"] = "Sí"
            user_data[phone] = data
            send_msg(phone, "*¿Cuál es tu nombre completo?*")
            user_state[phone] = "imss_q_nombre_calc"
        elif r == "no":
            send_msg(phone,
                "De acuerdo. Si después quieres revisar una propuesta, escríbeme "
                "\"Préstamo IMSS\" o \"cuánto me prestan\".")
            _imss_close(phone)
        else:
            send_msg(phone, "Responde *1* si quieres que Christian revise tu caso, o *2* si no por ahora.")
        return

    if state == "imss_post_cierre":
        n_msg = norm(msg).strip()
        if _is_pure_courtesy_message(n_msg):
            if data.get("cierre_tipo") == "revision_aceptada":
                send_msg(phone, "Con gusto 😊\nChristian revisará tu caso y te contactará a la brevedad.")
            else:
                send_msg(phone,
                    "Con gusto 😊\nSi después quieres revisar una propuesta, escríbeme "
                    "\"Préstamo IMSS\" o \"cuánto me prestan\".")
        else:
            send_msg(phone, "¡Con gusto! Si necesitas algo más, aquí estoy 😊")
        reset(phone)
        return

    if state == "imss_q_nombre_calc":
        data["nombre"] = msg.strip().title()
        user_data[phone] = data
        send_msg(phone, "*¿En qué ciudad vives?*")
        user_state[phone] = "imss_q_ciudad_calc"
        return

    if state == "imss_q_ciudad_calc":
        data["ciudad"] = msg.strip().title()
        # Se calculan UNA sola vez, al construir el cierre, y se persisten:
        # la respuesta 1/2/3 se resolvera contra estas mismas etiquetas aunque
        # el cliente conteste horas despues. Un cierre nuevo las reemplaza.
        data["imss_horarios_ofrecidos"] = _imss_build_horario_opciones()
        user_data[phone] = data

        # Cierre + pregunta de horario en UNA sola burbuja (ya no se manda una
        # segunda pregunta suelta de horario).
        send_msg(phone, _imss_build_closing_statement(data))

        # Notificar al asesor ANTES de quedar a la espera del horario -- el
        # lead ya debe quedar calificado y registrado aunque el prospecto
        # abandone la conversacion sin responder (H-06).
        advisor_notify_ok = notify_advisor(_imss_build_advisor_notification(phone, data))
        data["advisor_notify_ok"] = advisor_notify_ok
        user_data[phone] = data
        if not advisor_notify_ok:
            _imss_log_lead_backup(phone, data)
        _notify_boardroom_lead_qualified(phone, "prestamo_imss_ley73", _ensure_user(phone))

        user_state[phone] = "imss_q_horario_calc"
        return

    if state == "imss_q_horario_calc":
        n_horario = norm(msg).strip()
        if _is_pure_courtesy_message(n_horario):
            # Cortesia pura (gracias/ok/listo/etc), no es un horario: no se
            # guarda horario_contacto ni se manda una actualizacion falsa al
            # asesor. Se cierra de forma segura (menor friccion) conservando
            # los datos comerciales ya capturados.
            send_msg(phone, "¡Con gusto! Christian López te contactará pronto. 😊")
            _imss_close(phone, tipo="revision_aceptada", data=data)
            return
        # 1/2/3 se resuelven contra las etiquetas que el cliente realmente vio;
        # cualquier otro texto valido se conserva como horario libre con el
        # limite existente.
        horario = _imss_normalize_horario(msg, _imss_horarios_ofrecidos(data))
        if horario is None:
            # Eligio "Otro día y horario específico": no se guarda "3" como
            # horario ni se notifica nada al asesor. Se sigue esperando el
            # texto libre en el mismo estado.
            send_msg(phone,
                "Claro 😊 Escríbeme el *día y el horario* que prefieras, por ejemplo:\n"
                "“El jueves a las 10:00 a. m.”")
            return
        data["horario_contacto"] = horario
        user_data[phone] = data
        send_msg(phone, "¡Perfecto! Ya quedó registrado. *Christian López te contactará "
                        "en el horario indicado.* 😊")
        notify_advisor(f"⏰ HORARIO DE CONTACTO — {data.get('nombre', 'ND')} — {horario}")
        _imss_close(phone, tipo="revision_aceptada", data=data)
        return


# ── Flujo Seguro Auto ─────────────────────────────────────────────────────────
def funnel_auto(phone: str, msg: str) -> None:
    state = user_state.get(phone, "auto_open")
    data = user_data.get(phone, {})

    if state == "auto_open":
        send_msg(phone,
            "🚗 *Seguro de Auto Inbursa*\n\n"
            "Te ayudo a solicitar tu cotización.\n"
            "*¿Tienes seguro actualmente?* _(Sí/No)_")
        user_state[phone] = "auto_q_tipo"
        return

    if state == "auto_q_tipo":
        r = yes_no(msg)
        data["tiene_seguro_actual"] = r if r in ("si", "no") else msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿Marca y modelo de tu vehículo?*")
        user_state[phone] = "auto_q_modelo"
        return

    if state == "auto_q_modelo":
        data["marca_modelo"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "*¿Año del vehículo?*")
        user_state[phone] = "auto_q_ano"
        return

    if state == "auto_q_ano":
        data["ano"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿Tu nombre completo?*")
        user_state[phone] = "auto_q_nombre"
        return

    if state == "auto_q_nombre":
        data["nombre"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "*¿Tu número de contacto?*\n_(Escribe \"mismo\" si es este WhatsApp)_")
        user_state[phone] = "auto_q_tel"
        return

    if state == "auto_q_tel":
        data["tel"] = phone if msg.strip().lower() in ("mismo", "este", "el mismo") else msg.strip()
        user_data[phone] = data
        send_msg(phone, "✅ Listo. El asesor *Christian López* te contactará para tu cotización de auto.")
        notify_advisor(
            f"🚗 PROSPECTO – SEGURO AUTO\n"
            f"Nombre: {data.get('nombre', 'ND')}\n"
            f"WhatsApp: {phone}\n"
            f"Teléfono: {data.get('tel', 'ND')}\n"
            f"Seguro actual: {data.get('tiene_seguro_actual', 'ND')}\n"
            f"Vehículo: {data.get('marca_modelo', 'ND')}\n"
            f"Año: {data.get('ano', 'ND')}"
        )
        _notify_boardroom_lead_qualified(phone, "seguro_vida", _ensure_user(phone))
        reset(phone)
        return

# ── Flujo Vida y Salud ────────────────────────────────────────────────────────
def funnel_vida(phone: str, msg: str) -> None:
    state = user_state.get(phone, "vida_open")
    data = user_data.get(phone, {})

    if state == "vida_open":
        send_msg(phone,
            "🏥 *Seguro de Vida y Salud Inbursa*\n\n"
            "Con gusto te ayudo a perfilar tu solicitud.\n"
            "*¿Qué tipo de cobertura te interesa?* _(Vida / GMM / Ambas)_")
        user_state[phone] = "vida_q_tipo"
        return

    if state == "vida_q_tipo":
        data["tipo_cobertura"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿Tu edad aproximada?*")
        user_state[phone] = "vida_q_edad"
        return

    if state == "vida_q_edad":
        data["edad"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿Tu nombre completo?*")
        user_state[phone] = "vida_q_nombre"
        return

    if state == "vida_q_nombre":
        data["nombre"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "*¿Tu número de contacto?*\n_(Escribe \"mismo\" si es este WhatsApp)_")
        user_state[phone] = "vida_q_tel"
        return

    if state == "vida_q_tel":
        data["tel"] = phone if msg.strip().lower() in ("mismo", "este", "el mismo") else msg.strip()
        user_data[phone] = data
        send_msg(phone, "✅ Listo. El asesor *Christian López* te contactará para revisar tu cobertura.")
        notify_advisor(
            f"🏥 PROSPECTO – VIDA Y SALUD\n"
            f"Nombre: {data.get('nombre', 'ND')}\n"
            f"WhatsApp: {phone}\n"
            f"Teléfono: {data.get('tel', 'ND')}\n"
            f"Cobertura: {data.get('tipo_cobertura', 'ND')}\n"
            f"Edad: {data.get('edad', 'ND')}"
        )
        _notify_boardroom_lead_qualified(phone, "seguro_vida", _ensure_user(phone))
        reset(phone)
        return

# ── Flujo VRIM ────────────────────────────────────────────────────────────────
def funnel_vrim(phone: str, msg: str) -> None:
    state = user_state.get(phone, "vrim_open")
    data = user_data.get(phone, {})

    if state == "vrim_open":
        send_msg(phone,
            "💳 *Tarjeta Médica VRIM*\n\n"
            "Te ayudo a registrar tu interés.\n"
            "*¿Para cuántas personas sería la membresía?*")
        user_state[phone] = "vrim_q_personas"
        return

    if state == "vrim_q_personas":
        data["personas"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿Tu nombre completo?*")
        user_state[phone] = "vrim_q_nombre"
        return

    if state == "vrim_q_nombre":
        data["nombre"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "*¿Tu número de contacto?*\n_(Escribe \"mismo\" si es este WhatsApp)_")
        user_state[phone] = "vrim_q_tel"
        return

    if state == "vrim_q_tel":
        data["tel"] = phone if msg.strip().lower() in ("mismo", "este", "el mismo") else msg.strip()
        user_data[phone] = data
        send_msg(phone, "✅ Listo. El asesor *Christian López* te contactará para tu membresía VRIM.")
        notify_advisor(
            f"💳 PROSPECTO – VRIM\n"
            f"Nombre: {data.get('nombre', 'ND')}\n"
            f"WhatsApp: {phone}\n"
            f"Teléfono: {data.get('tel', 'ND')}\n"
            f"Personas: {data.get('personas', 'ND')}"
        )
        _notify_boardroom_lead_qualified(phone, "seguro_vida", _ensure_user(phone))
        reset(phone)
        return

# ── Flujo Empresarial ─────────────────────────────────────────────────────────
def funnel_emp(phone: str, msg: str) -> None:
    state = user_state.get(phone, "emp_start")
    data = user_data.get(phone, {})

    if state == "emp_start":
        send_msg(phone,
            "🏢 *Crédito Empresarial Inbursa*\n"
            "💰 $100,000–$100,000,000 · Tasas preferenciales · Sin aval con buen historial\n\n"
            "¿Representas una empresa o eres empresario? _(Sí/No)_")
        user_state[phone] = "emp_q_confirm"
        return

    if state == "emp_q_confirm":
        r = yes_no(msg)
        if r == "si" or any(k in msg.lower() for k in ["empresario", "empresa", "negocio", "pyme", "comercio"]):
            send_msg(phone, "¿A qué *se dedica* tu empresa?")
            user_state[phone] = "emp_q_giro"
        elif r == "no":
            send_msg(phone, "Entendido 😊 ¿Hay algo más en que pueda ayudarte?")
            reset(phone)
        else:
            send_msg(phone, "Responde *sí* o *no* para continuar.")
        return

    if state == "emp_q_giro":
        data["giro"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "¿Qué *monto* necesitas? _(mínimo $100,000)_")
        user_state[phone] = "emp_q_monto"
        return

    if state == "emp_q_monto":
        m = extract_num(msg)
        if not m or m < 100000:
            send_msg(phone, "Indica el monto _(mínimo $100,000)_, ej. *250000*.")
            return
        data["monto"] = m
        user_data[phone] = data
        send_msg(phone, "*¿Tu nombre completo?*")
        user_state[phone] = "emp_q_nombre"
        return

    if state == "emp_q_nombre":
        data["nombre"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "*¿Tu número de contacto?*")
        user_state[phone] = "emp_q_tel"
        return

    if state == "emp_q_tel":
        data["tel"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿En qué ciudad está tu empresa?*")
        user_state[phone] = "emp_q_ciudad"
        return

    if state == "emp_q_ciudad":
        data["ciudad"] = msg.title()
        user_data[phone] = data
        send_msg(phone, "✅ Listo. El asesor *Christian López* te contactará a la brevedad.")
        notify_advisor(
            f"🔔 PROSPECTO – CRÉDITO EMPRESARIAL\n"
            f"Nombre: {data.get('nombre', 'ND')}\n"
            f"WA: {phone} · Tel: {data.get('tel', 'ND')}\n"
            f"Ciudad: {data.get('ciudad', 'ND')}\n"
            f"Giro:   {data.get('giro', 'ND')}\n"
            f"Monto:  ${data.get('monto', 0):,.0f}")
        _notify_boardroom_lead_qualified(phone, "nomina_empresarial", _ensure_user(phone))
        reset(phone)
        return

# ── Flujo Consigue Tu Crédito (CTC) ────────────────────────────────────────────
CTC_PRODUCT_LABEL = "Crédito empresarial sin garantía"
CTC_CAMPAIGN_LABEL = "CTC julio 2026"

def _ctc_tipo_label(msg: str) -> str:
    n = norm(msg).strip()
    if n == "1" or "tengo negocio" in n:
        return "Tiene negocio"
    if n == "2" or "independiente" in n:
        return "Actividad independiente"
    if n == "3" or "empezando" in n or "apenas" in n:
        return "Apenas está empezando"
    return msg.strip() or "ND"

def _ctc_factura_label(msg: str) -> str:
    n = norm(msg).strip()
    if n == "1" or n == "si":
        return "Sí"
    if n == "2" or n == "no":
        return "No"
    if n == "3" or "a veces" in n:
        return "A veces"
    return msg.strip() or "ND"

# ── Cortesia post-cierre CTC (independiente de la de IMSS, mismo patron) ──────
_CTC_CORTESIA_KW = {"gracias", "ok", "okay", "va", "sale", "perfecto", "excelente", "entendido", "listo"}
_CTC_CORTESIA_PHRASES = {"de acuerdo", "muy bien"}
_CTC_CORTESIA_FILLER = {"muchas", "vicky", "quedo", "pendiente", "espero", "su", "llamada", "tambien"}

def _is_ctc_pure_courtesy_message(n_msg: str) -> bool:
    """True solo si el mensaje, quitando cortesia y relleno, no deja nada
    sustantivo -- evita que 'gracias, tambien quiero cotizar auto' se trague
    como cortesia en vez de rutearse como nueva intencion."""
    n_msg = n_msg.strip()
    if not n_msg:
        return False
    working = n_msg
    for phrase in _CTC_CORTESIA_PHRASES:
        working = working.replace(phrase, " ")
    toks = set(working.split())
    has_courtesy = bool(toks & _CTC_CORTESIA_KW) or any(p in n_msg for p in _CTC_CORTESIA_PHRASES)
    if not has_courtesy:
        return False
    remaining = toks - _CTC_CORTESIA_KW - _CTC_CORTESIA_FILLER
    return len(remaining) == 0

CTC_POST_CLOSE_WINDOW_SECONDS = 30 * 60
_ctc_post_close_ctx: dict = {}

def _ctc_close(phone: str) -> None:
    """Cierra el funnel CTC normalmente (reset completo) pero registra un
    contexto post-cierre de corta duracion, independiente de user_state, para
    poder responder cortesia ('gracias'/'ok'/etc) sin caer en el fallback
    neutral de Boardroom ni volver a notificar al asesor -- incluso si llegan
    varios mensajes de cortesia seguidos."""
    reset(phone)
    _ctc_post_close_ctx[phone] = {"ts": time.time(), "acknowledged": False}

def _ctc_post_close_active(phone: str) -> bool:
    ctx = _ctc_post_close_ctx.get(phone)
    if not ctx:
        return False
    if time.time() - ctx["ts"] > CTC_POST_CLOSE_WINDOW_SECONDS:
        _ctc_post_close_ctx.pop(phone, None)
        return False
    return True

def _ctc_handle_post_close_courtesy(phone: str, msg: str) -> bool:
    """Si hay un cierre CTC reciente y el mensaje es cortesia pura, la absorbe
    localmente (primera vez con acuse, repeticiones en silencio, nunca
    Boardroom/fallback) y devuelve True. Si el mensaje no es cortesia pura,
    libera el contexto y devuelve False para que se enrute normalmente."""
    if not _ctc_post_close_active(phone):
        return False
    n_msg = norm(msg).strip()
    if not _is_ctc_pure_courtesy_message(n_msg):
        _ctc_post_close_ctx.pop(phone, None)
        return False
    ctx = _ctc_post_close_ctx[phone]
    if not ctx["acknowledged"]:
        send_msg(phone, "Con gusto 😊\nChristian revisará tu caso y te contactará a la brevedad.")
        ctx["acknowledged"] = True
    return True

def funnel_fp(phone: str, msg: str) -> None:
    state = user_state.get(phone, "fp_start")
    data = user_data.get(phone, {})

    if state == "fp_start":
        send_msg(phone,
            "💼 *Consigue Tu Crédito — COHIFIS*\n\n"
            "Te ayudo a revisar si puedes acceder a un *crédito empresarial sin garantía* "
            "para tu negocio o actividad independiente.\n\n"
            "No es una aprobación automática; primero hacemos una preevaluación rápida "
            "para saber si podemos avanzar.\n\n"
            "Para empezar:\n\n"
            "¿Tienes negocio o actividad independiente?\n\n"
            "Responde:\n"
            "1. Sí, tengo negocio\n"
            "2. Trabajo de forma independiente\n"
            "3. Apenas estoy empezando")
        user_state[phone] = "fp_tipo"
        return

    if state == "fp_tipo":
        data["tipo_actividad"] = _ctc_tipo_label(msg)
        user_data[phone] = data
        send_msg(phone, "¿Cuánto crédito necesitas aproximadamente?")
        user_state[phone] = "fp_monto"
        return

    if state == "fp_monto":
        data["monto"] = msg.strip()
        user_data[phone] = data
        send_msg(phone,
            "¿Para qué lo usarías?\n"
            "_(ej. inventario, capital de trabajo, maquinaria/equipo, expansión, "
            "pagar proveedores, otro)_")
        user_state[phone] = "fp_uso"
        return

    if state == "fp_uso":
        data["uso_credito"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "¿Cuál es el giro o actividad de tu negocio?")
        user_state[phone] = "fp_giro"
        return

    if state == "fp_giro":
        data["giro"] = msg.strip()
        user_data[phone] = data
        send_msg(phone, "¿Actualmente facturas?\n1. Sí\n2. No\n3. A veces")
        user_state[phone] = "fp_factura"
        return

    if state == "fp_factura":
        data["factura"] = _ctc_factura_label(msg)
        user_data[phone] = data
        send_msg(phone, "Para revisar tu caso, dime tu nombre completo.")
        user_state[phone] = "fp_nombre"
        return

    if state == "fp_nombre":
        data["nombre"] = msg.strip()
        user_data[phone] = data
        notify_advisor(
            "NUEVO LEAD — CONSIGUE TU CRÉDITO\n\n"
            f"Producto: {CTC_PRODUCT_LABEL}\n"
            f"Campaña: {CTC_CAMPAIGN_LABEL}\n"
            "Estado: Pendiente de calificación\n\n"
            f"Nombre: {data.get('nombre', 'ND')}\n"
            f"WhatsApp: {phone}\n"
            f"Tipo de actividad: {data.get('tipo_actividad', 'ND')}\n"
            f"Monto solicitado: {data.get('monto', 'ND')}\n"
            f"Uso del crédito: {data.get('uso_credito', 'ND')}\n"
            f"Giro: {data.get('giro', 'ND')}\n"
            f"Factura actualmente: {data.get('factura', 'ND')}\n\n"
            "Resumen: Lead interesado en crédito empresarial sin garantía. "
            "Requiere revisión manual para validar viabilidad.")
        send_msg(phone,
            "Listo, ya tengo tus datos iniciales.\n\n"
            "Christian revisará tu caso y te contactará para decirte si podemos avanzar "
            "con una opción de crédito empresarial sin garantía.\n\n"
            "Gracias por contactar a COHIFIS.")
        _ctc_close(phone)
        return

# ── Pregunta filtro para mensajes ambiguos relacionados a pensión/crédito ─────
_FILT_PHRASES = {
    "soy pensionado", "soy pensionada", "soy jubilado", "soy jubilada",
    "estoy pensionado", "estoy pensionada", "estoy jubilado", "estoy jubilada",
    "me interesa el prestamo", "me interesa el credito",
    "quiero saber si califico",
    "prestamo pensionado", "credito pensionado",
    "pension", "pensionado", "pensionada", "jubilado", "jubilada",
}

def _needs_filter(n: str) -> bool:
    return any(norm(k) in n for k in _FILT_PHRASES)

# ── Triggers de menú explícito ────────────────────────────────────────────────
_MENU_EXACT = {
    "menu", "memu", "inicio", "start",
    "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches",
    "servicios", "opciones", "catalogo", "productos",
    "que manejas", "que ofrecen", "que ofreces", "que tienes", "que tienen",
    "que servicios tienen", "que servicios ofrecen",
    "quiero ver opciones", "ver menu", "ver el menu", "mostrar menu",
}
_MENU_CONTAINS = {"que servicios", "ver el menu", "mostrar opciones", "ver opciones"}

_FIN_KW = {
    "seguro", "seguros", "cobertura", "coberturas", "poliza", "polizas",
    "prestamo", "prestamos", "credito", "creditos", "financiamiento",
    "inbursa", "pension", "pensionado", "pensionada", "jubilado", "jubilada",
    "cotizar", "califico", "requisito", "requisitos", "tarjeta",
    "medico", "medica", "medicos", "medicas", "gmm", "auto", "carro",
    "vehiculo", "vrim", "empresa", "empresarial", "ley", "73"
}

_Q_WORDS = {
    "que", "como", "cual", "cuales", "cuanto", "cuantos",
    "donde", "cuando", "por", "porque", "requisito", "requisitos",
    "duda", "explica", "explicas", "ayuda", "ayudar", "cotizar"
}

_Q_PHRASES = {
    "tengo duda", "me puedes ayudar", "me puedes explicar", "quiero saber",
    "quisiera saber", "tengo una duda", "me orientas", "me apoyas",
    "como funciona", "cuales son", "que incluye", "que cubre",
    "me puedes decir", "necesito informacion"
}

def _is_open_question(raw: str, n: str) -> bool:
    toks = set(n.split())
    if "?" in raw or "¿" in raw:
        return True
    if any(p in n for p in _Q_PHRASES):
        return True
    if "duda" in toks:
        return True
    if len(toks) >= 5 and toks & _Q_WORDS:
        return True
    return False

def _is_financial_context(n: str, svc: str | None = None) -> bool:
    toks = set(n.split())
    return bool(svc) or bool(toks & _FIN_KW)

# ── Procesamiento del mensaje ─────────────────────────────────────────────────
def handle(msg_obj: dict) -> None:
    phone = msg_obj.get("from", "")
    if not phone:
        return

    mid = msg_obj.get("id", "")
    if mid:
        with _id_lock:
            if mid in _seen_ids:
                return
            if len(_seen_dq) >= 3000:
                _seen_ids.discard(_seen_dq[0])
            _seen_dq.append(mid)
            _seen_ids.add(mid)
    _tl.mid = mid

    # Cualquier mensaje del asesor reabre su ventana de 24h en WhatsApp. Se
    # registra aqui, antes de cualquier ruteo, para que valga aunque el mensaje
    # termine en un funnel, en el menu o descartado. No altera el ruteo.
    if _is_advisor_phone(phone):
        _advisor_window_touch()

    mtype = msg_obj.get("type", "")
    text_for_boardroom = _message_text(msg_obj, mtype)
    if BOARDROOM_IS_AUTHORITY:
        logged_text = text_for_boardroom if text_for_boardroom else f"[{_canonical_message_type(mtype)}]"
        log.info(f"📱 {phone}: {logged_text[:80]}")
        # DEUDA TÉCNICA FASE 2: _log() registra lead en Sheets antes
        # de recibir decisión de Boardroom. Cuando Boardroom sea
        # responsable de Sheets (Fase 2), este log debe moverse
        # a _execute_boardroom_instruction() usando commercial_intent
        # y lead_status reales de la respuesta de Boardroom.
        _log(phone, _nombre(phone), logged_text, "entrante", "cliente", "", "", mid)

        # Cortesia post-cierre CTC: independiente de user_state (sobrevive un
        # reset() completo), asi que "gracias"/"ok"/etc despues de un cierre
        # exitoso de CTC nunca llega a Boardroom, sin importar cuantos mensajes
        # de cortesia seguidos lleguen dentro de la ventana.
        if _ctc_handle_post_close_courtesy(phone, text_for_boardroom):
            return

        # Pre-router local de estado activo: si el usuario esta a mitad de un
        # funnel local (imss_/auto_/vida_/vrim_/emp_/fp_), la respuesta debe
        # continuar ESE funnel, nunca ir a Boardroom. El contrato canonico de
        # Boardroom para Vicky hoy es Fase 1 (siempre responde con el mensaje
        # neutral generico -- ver audit.decision_reason=
        # phase_1_safe_response_no_commercial_decision en boardroom-engine),
        # asi que cualquier respuesta de continuacion de funnel que llegara
        # ahi terminaba mostrando el fallback en vez de avanzar la
        # conversacion (ej. opcion 6 -> "si" -> fallback en vez de la
        # siguiente pregunta del funnel).
        active_state = user_state.get(phone, "")
        if active_state == "imss_post_cierre" and not _is_pure_courtesy_message(norm(text_for_boardroom) if text_for_boardroom else ""):
            # Mensaje post-cierre que no es cortesia pura (ej. "gracias, tambien
            # quiero cotizar auto"): se libera el estado y se deja caer al resto
            # del pre-router (menu/opciones/intent IMSS/campana/Boardroom) en vez
            # de tragarlo como agradecimiento.
            reset(phone)
            active_state = ""
        elif active_state.startswith("imss_"):
            funnel_imss(phone, text_for_boardroom)
            return
        if active_state.startswith("auto_"):
            funnel_auto(phone, text_for_boardroom)
            return
        if active_state.startswith("vida_"):
            funnel_vida(phone, text_for_boardroom)
            return
        if active_state.startswith("vrim_"):
            funnel_vrim(phone, text_for_boardroom)
            return
        if active_state.startswith("emp_"):
            funnel_emp(phone, text_for_boardroom)
            return
        if active_state.startswith("fp_"):
            funnel_fp(phone, text_for_boardroom)
            return

        # Pre-router local: comandos de UX (menu, opciones 1-6) se resuelven
        # aqui mismo, sin pasar por Boardroom. Antes de este fix, TODO mensaje
        # -- incluido "menu" -- caia directo en _handle_boardroom_authority()
        # y, si Boardroom fallaba (ej. http_401), terminaba en el fallback
        # neutral "Recibi tu mensaje...". El menu es UX local pura, no debe
        # depender de la disponibilidad ni autenticacion de Boardroom.
        n_local = norm(text_for_boardroom) if text_for_boardroom else ""
        if n_local in _MENU_EXACT or any(p in n_local for p in _MENU_CONTAINS):
            reset(phone)
            show_menu(phone)
            return
        if n_local in _LOCAL_NUMERIC_OPTIONS:
            svc = detect_svc(text_for_boardroom)
            if svc:
                route(phone, svc)
                return

        # Intent de propuesta de prestamo IMSS (calculadora) fuera de un estado
        # activo: se resuelve localmente, antes de Boardroom, igual que el menu.
        if _is_imss_loan_proposal_intent(n_local):
            _imss_route_free_form(phone, text_for_boardroom)
            return
        if _is_ambiguous_bare_loan_ask(n_local):
            _imss_route_free_form(phone, "")
            return
        if _is_campaign(msg_obj, n_local):
            data = _ensure_user(phone)
            ref = msg_obj.get("referral") or {}
            if ref:
                hl = str(ref.get("headline") or "")[:200]
                sid = str(ref.get("source_id") or "")[:100]
                data["origen"] = "campana_IMSS" + (f" | {hl or sid}" if (hl or sid) else "")
                data["referral_headline"] = hl
                data["referral_source_id"] = sid
                data["referral_ad_id"] = str(ref.get("ad_id") or "")[:100]
                data["referral_campaign_id"] = str(ref.get("campaign_id") or "")[:100]
            else:
                data["origen"] = "interes_directo_IMSS"
            user_data[phone] = data
            user_state[phone] = "imss_open"
            funnel_imss(phone, "")
            return
        if _needs_filter(n_local):
            user_data.setdefault(phone, {})
            user_data[phone]["origen"] = "filtro_ambiguo"
            user_state[phone] = "imss_filtro"
            send_msg(phone, "Para orientarte bien: ¿tu pensión es del *IMSS bajo la Ley 73*? 😊")
            return

        # Ruteo por referral de anuncio Meta (Click to WhatsApp): el texto que
        # Meta genera ("Hello! Can I get more info on this?") es generico; el
        # producto viene en el referral del anuncio. Se rutea localmente ANTES
        # de Boardroom. El referral IMSS ya fue atendido por _is_campaign()
        # arriba, asi que aqui solo llegan referrals no-IMSS (emp/fp/otros).
        referral = msg_obj.get("referral") or {}
        if isinstance(referral, dict) and referral:
            svc = _detect_meta_referral_svc(msg_obj, text_for_boardroom)
            referral_text = _campaign_referral_text(msg_obj, text_for_boardroom)
            log.info("META_REFERRAL_DETECTED phone_last4=%s fields=%s detected_svc=%s",
                     phone[-4:], referral_text[:300], svc)
            if svc:
                data = _ensure_user(phone)
                data["origen"] = f"meta_referral_{svc}"
                if svc == "fp" and _is_ctc_meta_campaign_referral(msg_obj, text_for_boardroom):
                    data["origen"] = "meta_referral_ctc_campaign_override"
                data["referral_headline"] = str(referral.get("headline") or "")[:200]
                data["referral_source_id"] = str(referral.get("source_id") or "")[:100]
                data["referral_ad_id"] = str(referral.get("ad_id") or "")[:100]
                data["referral_campaign_id"] = str(referral.get("campaign_id") or "")[:100]
                user_data[phone] = data
                route(phone, svc)
                return
            # Hay referral pero sin producto detectable: pregunta aclaratoria
            # en vez de fallback neutro, para no matar el lead de Meta. La
            # respuesta (numero 1-6 o texto) la resuelve el pre-router local.
            send_msg(phone,
                "Con gusto te atiendo. ¿Te interesa *crédito empresarial*, *CTC*, "
                "*seguro de auto*, *vida* o *VRIM*?\n\n"
                "1️⃣ Préstamo IMSS pensionados\n"
                "2️⃣ Seguro de Auto\n"
                "3️⃣ Seguro de Vida y Salud\n"
                "4️⃣ Tarjeta Médica VRIM\n"
                "5️⃣ Financiamiento Empresarial\n"
                "6️⃣ Consigue Tu Crédito (CTC)\n\n"
                "Responde con el *número* o el nombre del servicio. 😊")
            return

        # Deteccion local de servicio por texto libre (ej. "ctc", "credito
        # empresarial"): mismo criterio que el resto del pre-router — si el
        # producto es claro, se atiende localmente y no depende de Boardroom.
        if text_for_boardroom:
            svc = detect_svc(text_for_boardroom)
            if svc:
                route(phone, svc)
                return

        _handle_boardroom_authority(phone, msg_obj, mtype, text_for_boardroom)
        return

    if mtype in ("image", "document"):
        media_id = (
            msg_obj.get("image", {}).get("id")
            or msg_obj.get("document", {}).get("id")
            or ""
        )
        if media_id:
            threading.Thread(
                target=_notify_boardroom_document,
                args=(phone, media_id, mtype),
                daemon=True
            ).start()
            send_msg(phone,
                "✅ Documento recibido. Christian López lo revisará "
                "y te confirmará en breve."
            )
            return jsonify({"ok": True}), 200

    if mtype and mtype != "text":
        _log(phone, _nombre(phone), f"[{mtype}]", "entrante", "cliente", "", "", mid)
        send_msg(phone, "Por ahora solo proceso mensajes de texto 📩")
        return

    text = (msg_obj.get("text") or {}).get("body", "").strip()[:500]
    if not text:
        return

    log.info(f"📱 {phone}: {text[:80]}")
    _log(phone, _nombre(phone), text, "entrante", "cliente", "", "", mid)

    n = norm(text)

    if text.lower().startswith("sgpt:"):
        p = text[5:].strip()
        if p:
            send_msg(phone, ask_gpt(p))
        return

    _emit_bus_event(phone=phone, text=text)

    if _ctc_handle_post_close_courtesy(phone, text):
        return

    state = user_state.get(phone, "")
    if state == "imss_post_cierre" and not _is_pure_courtesy_message(n):
        reset(phone)
        state = ""
    elif state.startswith("imss_"):
        funnel_imss(phone, text)
        return
    if state.startswith("auto_"):
        funnel_auto(phone, text)
        return
    if state.startswith("vida_"):
        funnel_vida(phone, text)
        return
    if state.startswith("vrim_"):
        funnel_vrim(phone, text)
        return
    if state.startswith("emp_"):
        funnel_emp(phone, text)
        return
    if state.startswith("fp_"):
        funnel_fp(phone, text)
        return

    if _is_imss_loan_proposal_intent(n):
        _imss_route_free_form(phone, text)
        return
    if _is_ambiguous_bare_loan_ask(n):
        _imss_route_free_form(phone, "")
        return

    if _is_campaign(msg_obj, n):
        user_data.setdefault(phone, {})
        ref = msg_obj.get("referral") or {}
        if ref:
            hl = ref.get("headline", "")
            sid = ref.get("source_id", "")
            origen = "campaña_IMSS" + (f" | {hl or sid}" if hl or sid else "")
        else:
            origen = "interes_directo_IMSS"
        log.info(f"📌 {phone}: origen={origen!r}")
        user_data[phone]["origen"] = origen
        user_state[phone] = "imss_open"
        funnel_imss(phone, "")
        return

    if _needs_filter(n):
        log.info(f"🔍 {phone}: filtro IMSS activado para: {n[:60]!r}")
        user_data.setdefault(phone, {})
        user_data[phone]["origen"] = "filtro_ambiguo"
        user_state[phone] = "imss_filtro"
        send_msg(phone, "Para orientarte bien: ¿tu pensión es del *IMSS bajo la Ley 73*? 😊")
        return

    if n in _MENU_EXACT or any(p in n for p in _MENU_CONTAINS):
        reset(phone)
        show_menu(phone)
        return

    _adv = {"hablar con un asesor", "contactar asesor", "que me llamen", "llamame",
            "quiero que me llamen", "hablar con un ejecutivo", "comunicame con alguien"}
    if any(t in n for t in _adv):
        send_msg(phone, "📞 Avisaré a nuestro asesor *Christian López* para que te contacte.\n"
                        "¿Hay algo en que pueda orientarte mientras tanto?")
        notify_advisor(f"📣 CONTACTO DIRECTO\nWhatsApp: {phone}\nMensaje: {text}")
        return

    svc = detect_svc(text)
    is_question = _is_open_question(text, n)
    in_finance = _is_financial_context(n, svc)

    if svc and is_question:
        send_msg(phone, ask_gpt(text, svc))
        return

    if svc:
        route(phone, svc)
        return

    if is_question and in_finance:
        send_msg(phone, ask_gpt(text, svc))
        return

    if in_finance:
        send_msg(phone, ask_gpt(text, svc))
        return

    show_menu(phone)

# ── Estados asíncronos de Meta (value.statuses[]) ─────────────────────────────
# Meta reporta aquí lo que la respuesta síncrona nunca dice: si el mensaje se
# entregó de verdad (`sent`/`delivered`/`read`) o falló después de haber sido
# aceptado con HTTP 200 (`failed` + `errors[]`). Antes se descartaba en silencio,
# que es la razón de fondo por la que una alerta podía perderse sin dejar rastro.
_ADV_STATUS_SEEN: deque = deque(maxlen=2000)
_ADV_STATUS_SET: set = set()


def _format_status_errors(errors) -> str:
    if not errors:
        return ""
    parts = []
    for e in errors[:3]:
        if not isinstance(e, dict):
            continue
        details = ""
        ed = e.get("error_data")
        if isinstance(ed, dict):
            details = str(ed.get("details") or "")[:160]
        parts.append("code=%s title=%s message=%s details=%s" % (
            e.get("code"),
            str(e.get("title") or "")[:80],
            str(e.get("message") or "")[:120],
            details,
        ))
    return (" errors=[" + " | ".join(parts) + "]") if parts else ""


def _advisor_handle_failed(wamid: str, tracked: dict, err_txt: str) -> None:
    """Meta confirma que una alerta al asesor NO se entregó: reenvía por template."""
    log.error("asesor_alerta_no_entregada: wamid=%s nivel=%s%s",
              wamid[:24], tracked.get("level") or "?", err_txt)
    # El veredicto de Meta manda sobre la contabilidad local: si creíamos la
    # ventana abierta, estábamos equivocados.
    _advisor_window_expire()
    if tracked.get("level") == "template":
        log.error("asesor_reenvio_omitido: el template también falló, no hay nivel superior")
        return
    if not ADV_TPL:
        log.error("asesor_reenvio_omitido: ADVISOR_TEMPLATE_NAME no configurado")
        return
    body = _state_store.aux_get(f"adv_retry:{wamid}")
    if not body:
        log.error("asesor_reenvio_omitido: sin cuerpo correlacionado para wamid=%s", wamid[:24])
        return
    _notify_advisor_via_template(body, motivo="status_failed")


def _handle_statuses(statuses) -> None:
    """Procesa value.statuses[] de Meta.

    Solo observabilidad y reenvío reactivo de la alerta al asesor: no toca
    user_state, user_data ni ningún funnel. Un webhook de estados jamás debe
    producir efectos comerciales ni mensajes al prospecto.
    """
    if not statuses:
        return
    for st in statuses:
        try:
            if not isinstance(st, dict):
                continue
            wamid = str(st.get("id") or "")
            status = str(st.get("status") or "")
            if not wamid or not status:
                continue
            # Dedup propio por (wamid, status), separado del de messages[]:
            # Meta puede reentregar el mismo estado y el reenvío reactivo no
            # debe dispararse dos veces por el mismo `failed`.
            with _id_lock:
                seen_key = f"{wamid}:{status}"
                if seen_key in _ADV_STATUS_SET:
                    continue
                if len(_ADV_STATUS_SEEN) >= _ADV_STATUS_SEEN.maxlen:
                    _ADV_STATUS_SET.discard(_ADV_STATUS_SEEN[0])
                _ADV_STATUS_SEEN.append(seen_key)
                _ADV_STATUS_SET.add(seen_key)
            tracked = _advisor_wamid_lookup(wamid)
            err_txt = _format_status_errors(st.get("errors"))
            log.info("wa_status: estado=%s wamid=%s destino=%s alerta_asesor=%s%s",
                     status, wamid[:24], _mask_phone(st.get("recipient_id")),
                     bool(tracked), err_txt)
            if status == "failed" and tracked:
                _advisor_handle_failed(wamid, tracked, err_txt)
        except Exception:
            log.exception("💥 _handle_statuses")


# ── Verificación de firma Meta (HMAC-SHA256) ──────────────────────────────────
_WARNED_NO_APP_SECRET = False

def _verify_sig(raw: bytes, hdr: str) -> bool:
    global _WARNED_NO_APP_SECRET
    if not APP_SECRET:
        if not _WARNED_NO_APP_SECRET:
            log.error("❌ META_APP_SECRET no configurado. Webhook bloqueado hasta que se configure la firma de Meta.")
            _WARNED_NO_APP_SECRET = True
        return False
    if not hdr.startswith("sha256="):
        return False
    exp = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, hdr)

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "online", "service": "Vicky Bot Inbursa",
                    "sheets": _srdy, "ts": now_mx()}), 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        m = request.args.get("hub.mode")
        t = request.args.get("hub.verify_token")
        c = request.args.get("hub.challenge")
        if m == "subscribe" and VERIFY_TOKEN and t == VERIFY_TOKEN:
            return c, 200
        return "forbidden", 403
    try:
        raw = request.get_data()
        if not _verify_sig(raw, request.headers.get("X-Hub-Signature-256", "")):
            return jsonify({"status": "forbidden"}), 403
        data = request.get_json(force=True, silent=True) or {}
        for entry in data.get("entry", []):
            for chg in entry.get("changes", []):
                value = chg.get("value") or {}
                for msg in value.get("messages", []):
                    handle(msg)
                # Bucle hermano, no alternativo: un webhook mixto trae messages[]
                # y statuses[] a la vez y ambos deben procesarse. Hasta ahora la
                # clave `statuses` simplemente no se leia nunca.
                _handle_statuses(value.get("statuses", []))
        return jsonify({"status": "ok"}), 200
    except Exception:
        log.exception("❌ webhook POST")
        return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "sheets": _srdy}), 200


@app.route("/ext/boardroom/instruct", methods=["POST"])
def boardroom_instruct():
    """Recibe instrucciones de Boardroom para ejecutar en Vicky Redes."""
    token = request.headers.get("X-Internal-Token", "").strip()
    internal_token = os.getenv("INTERNAL_TOKEN", "").strip()
    if not internal_token or token != internal_token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    phone = str(body.get("phone", "") or "").strip()
    instruction = str(body.get("instruction", "") or "").strip()
    payload = body.get("payload", {})

    if not phone or not instruction:
        return jsonify({
            "ok": False,
            "error": "phone e instruction requeridos"
        }), 400

    if instruction == "hot_transfer":
        asesor_origen = payload.get("asesor_origen", "don_chiwy")
        sub_campana = payload.get("sub_campana", "")
        nombre = payload.get("nombre", "")
        send_msg(phone,
            f"Hola {nombre} 👋 Veo que ya eres parte de nuestra "
            f"familia COHIFIS "
            f"{'en la campaña ' + sub_campana if sub_campana else ''}. "
            f"Tu asesor asignado te contactará en breve."
        )
        notify_advisor(
            f"🔥 HOT TRANSFER — Cliente existente SII SECOM\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre}\n"
            f"Sub-campaña: {sub_campana}\n"
            f"Asesor origen: {asesor_origen}\n"
            f"⚡ Requiere atención inmediata"
        )

    elif instruction == "existing_client_greeting":
        nombre = payload.get("nombre", "")
        producto = payload.get("producto", "")
        send_msg(phone,
            f"Hola {nombre} 😊 Es un gusto verte de nuevo. "
            f"Recuerdo que estuviste interesado en {producto}. "
            f"¿En qué te puedo ayudar hoy?"
        )

    elif instruction == "escalate_chiwy":
        motivo = payload.get("motivo", "Solicitud especial")
        nombre = payload.get("nombre", "")
        notify_advisor(
            f"⚡ ESCALACIÓN DIRECTA — {nombre}\n"
            f"WhatsApp: {phone}\n"
            f"Motivo: {motivo}"
        )
        send_msg(phone,
            "✅ Tu solicitud es importante. Christian López te "
            "contactará personalmente en breve."
        )

    elif instruction == "resume_funnel":
        funnel = payload.get("funnel", "")
        if funnel == "imss": funnel_imss(phone, "")
        elif funnel == "auto": funnel_auto(phone, "")
        elif funnel == "vida": funnel_vida(phone, "")
        elif funnel == "vrim": funnel_vrim(phone, "")
        elif funnel in ("emp", "pyme"): funnel_emp(phone, "")

    elif instruction == "handle_message":
        text = str(payload.get("text") or "").strip()
        nombre = str(payload.get("nombre") or "").strip()
        mtype = str(payload.get("mtype") or "text").strip()
        media_id = str(payload.get("media_id") or "").strip()

        if nombre:
            ud = dict(user_data.get(phone) or {})
            ud["nombre"] = nombre
            user_data[phone] = ud

        msg_obj = {
            "from": phone,
            "id": f"boardroom-{uuid.uuid4().hex[:12]}",
            "type": mtype,
            "text": {"body": text},
            "image": {"id": media_id} if mtype == "image" else {},
            "document": {"id": media_id} if mtype == "document" else {},
        }
        threading.Thread(target=handle, args=(msg_obj,), daemon=True).start()

    else:
        return jsonify({
            "ok": False,
            "error": f"Instrucción desconocida: {instruction}"
        }), 400

    return jsonify({
        "ok": True,
        "instruction": instruction,
        "phone": phone
    }), 200


def _lead_payload_to_service(data: dict) -> str:
    """Deriva el servicio ('imss', 'auto', …) de un payload de /ext/lead sin mutarlo."""
    raw_interest = str(data.get("interes") or data.get("producto_interes") or "").strip()
    raw_service = str(data.get("servicio") or "").strip()

    interes = norm(str(data.get("interes") or ""))
    producto_interes = norm(str(data.get("producto_interes") or ""))
    servicio = norm(str(data.get("servicio") or ""))

    if (interes == "prestamo_imss" or producto_interes == "prestamo_imss"
            or "imss" in servicio or "ley 73" in servicio):
        return "imss"

    return detect_svc(raw_interest or raw_service) or ""


@app.route("/ext/lead", methods=["POST"])
def ext_lead():
    try:
        if not INTERNAL_TOKEN:
            log.error("❌ INTERNAL_TOKEN no configurado")
            return jsonify({"ok": False, "error": "internal_token_not_configured"}), 500
        if not _is_internal_request(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        data = request.get_json(force=True, silent=True) or {}
        lead_id = str(data.get("lead_id") or "").strip()
        nombre = str(data.get("nombre", "")).strip() or "Sin nombre"
        telefono = re.sub(r"\D", "", str(data.get("telefono", "")))[-10:]
        interest = str(data.get("interest") or data.get("interes") or "").strip() or "sin_especificar"
        source = str(data.get("source", "")).strip() or "desconocido"

        if not lead_id:
            lead_id = f"cohifis-{telefono}-{int(time.time())}"
        if len(telefono) != 10:
            return jsonify({"ok": False, "error": "invalid_telefono"}), 422

        advisor_msg = (
            f"🔔 Lead nuevo desde cohifis.com\n"
            f"Nombre: {nombre}\n"
            f"Teléfono: {telefono}\n"
            f"Interés: {interest}\n"
            f"Fuente: {source}\n"
            f"Lead ID: {lead_id}"
        )
        ok = notify_advisor(advisor_msg)
        if not ok:
            log.warning("⚠️ /ext/lead notify_advisor falló [lead_id=%s]", lead_id)
            return jsonify({"ok": False, "error": "advisor_notify_failed"}), 502

        svc = _lead_payload_to_service(data)
        product_code = _service_to_product_code(svc)
        threading.Thread(
            target=_notify_boardroom_lead_qualified,
            args=(telefono, product_code, {
                "lead_id": lead_id,
                "nombre": nombre,
                "telefono": telefono,
                "interest": interest,
                "source": source,
                "service_hint": svc or "general",
            }),
            daemon=True
        ).start()

        log.info("✅ /ext/lead OK [lead_id=%s product=%s]", lead_id, product_code)
        return jsonify({
            "ok": True,
            "lead_id": lead_id,
            "product_code": product_code,
        }), 200
    except Exception as exc:
        log.exception("❌ Error en /ext/lead: %s", exc)
        return jsonify({"ok": False, "error": "internal_server_error"}), 500

# ── Arranque ──────────────────────────────────────────────────────────────────
_sheets_init()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"🚀 Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
