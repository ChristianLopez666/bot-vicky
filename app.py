import os
import json
import logging
import re
import hmac
import hashlib
import threading
import unicodedata
import uuid
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

# ── Variables de entorno ──────────────────────────────────────────────────────
load_dotenv()

META_TOKEN   = os.getenv("META_TOKEN")
WABA_ID      = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUM  = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_KEY   = os.getenv("OPENAI_API_KEY")
APP_SECRET   = os.getenv("META_APP_SECRET", "").strip()
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "").strip()

HYDRA_URL   = os.getenv("HYDRA_URL", "https://boardroom-engine.onrender.com/boardroom/tasks/commercial").strip()
HYDRA_TOKEN = os.getenv("BOARDROOM_API_TOKEN", "").strip()
try:
    HYDRA_TIMEOUT = int(os.getenv("HYDRA_TIMEOUT", "8").strip() or "8")
except Exception:
    HYDRA_TIMEOUT = 8

# Notificación al asesor fuera de ventana 24h:
# Crea un template aprobado en Meta Business Manager con un parámetro {{1}}.
# Configura: ADVISOR_TEMPLATE_NAME=nombre_del_template
ADV_TPL      = os.getenv("ADVISOR_TEMPLATE_NAME", "").strip()
ADV_TPL_LANG = os.getenv("ADVISOR_TEMPLATE_LANG", "es_MX").strip()

GG_CREDS  = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEET_ID  = os.getenv("SHEETS_ID_CONVERSACIONES", "").strip()
SHEET_TAB = os.getenv("SHEETS_TAB_CONVERSACIONES", "Conversaciones").strip()

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
user_state: dict = {}
user_data: dict = {}

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

def notify_advisor(msg: str) -> bool:
    """
    Nivel 1 — texto libre (funciona dentro de ventana 24h del asesor).
    Nivel 2 — template aprobada (ADVISOR_TEMPLATE_NAME) si el texto libre falla.
    Sin template, la notificación fallará fuera de ventana 24h.
    """
    if not ADVISOR_NUM:
        return False
    try:
        r = _wa_post({"messaging_product": "whatsapp", "to": ADVISOR_NUM,
                      "type": "text", "text": {"body": msg}})
        if r.status_code in (200, 201):
            log.info("✅ Asesor notificado (texto libre)")
            _log(ADVISOR_NUM, "Asesor", msg, "saliente", "asesor", "ok", "", _mid())
            return True

        err1 = f"HTTP {r.status_code}: {r.text[:150]}"
        log.warning(f"⚠️ Texto libre al asesor falló ({err1}). Reintentando con template...")

        if not ADV_TPL:
            log.warning("⚠️ ADVISOR_TEMPLATE_NAME no configurado. "
                        "Define esta variable con el template aprobado en Meta para "
                        "notificaciones fuera de ventana 24h.")
            _log(ADVISOR_NUM, "Asesor", msg, "saliente", "asesor", "error", err1, _mid())
            return False

        r2 = _wa_post({"messaging_product": "whatsapp", "to": ADVISOR_NUM,
                       "type": "template", "template": {
                           "name": ADV_TPL, "language": {"code": ADV_TPL_LANG},
                           "components": [{"type": "body",
                                           "parameters": [{"type": "text", "text": msg[:1024]}]}]}})
        ok = r2.status_code in (200, 201)
        _log(ADVISOR_NUM, "Asesor", msg, "saliente", "asesor",
             "ok" if ok else "error", "" if ok else r2.text[:200], _mid())
        if ok:
            log.info("✅ Asesor notificado vía template")
        else:
            log.error(f"❌ Template falló: {r2.text[:200]}")
        return ok

    except Exception:
        log.exception("💥 notify_advisor")
        return False

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

def reset(phone: str):
    user_state.pop(phone, None)
    user_data.pop(phone, None)

# ── Menú general ──────────────────────────────────────────────────────────────
_MENU = (
    "🏦 *Servicios Financieros Inbursa*\n"
    "────────────────────────────\n"
    "1️⃣  *Préstamo IMSS Pensionados Ley 73*\n"
    "     💰 $40,000–$650,000 · Sin aval · Descuento vía pensión\n\n"
    "2️⃣  *Seguro de Auto*\n"
    "     🚗 Cobertura amplia · Asistencia 24/7\n\n"
    "3️⃣  *Seguro de Vida y Salud*\n"
    "     🏥 Vida · GMM · Hospitalización\n\n"
    "4️⃣  *Tarjeta Médica VRIM*\n"
    "     💳 Consultas ilimitadas · Labs · Descuentos\n\n"
    "5️⃣  *Financiamiento Empresarial*\n"
    "     🏢 $100K–$100M · PYMES y empresas\n\n"
    "6️⃣  *Financiamiento Práctico Empresarial*\n"
    "     ⚡ Aprobación desde 24 hrs · Sin garantía\n"
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

    if any(norm(k) in n for k in _IMSS_STRONG):
        return True

    return False

# ── GPT ───────────────────────────────────────────────────────────────────────
_SYS = (
    "Eres Vicky, asistente comercial de Christian López, asesor financiero de Inbursa. "
    "Orientas sobre 6 servicios: (1) Préstamo IMSS Pensionados Ley 73 $40K–$650K sin aval, "
    "(2) Seguro Auto, (3) Seguro Vida/GMM, (4) VRIM tarjeta médica, "
    "(5) Financiamiento Empresarial $100K–$100M, (6) Financiamiento Práctico 24hrs sin garantía. "
    "Responde en español mexicano, máximo 100 palabras, tono profesional y cálido. "
    "Resuelve dudas reales del cliente. Si la pregunta es abierta, contesta de forma útil; "
    "no mandes al menú salvo que el cliente lo pida. "
    "Termina con UNA sola pregunta cuando ayude a avanzar. "
    "No inventes tasas, requisitos ni condiciones no confirmadas."
)

_SERVICE_LABELS = {
    "imss": "Préstamo IMSS Ley 73",
    "auto": "Seguro de Auto",
    "vida": "Seguro de Vida y Salud",
    "vrim": "Tarjeta Médica VRIM",
    "emp": "Financiamiento Empresarial",
    "fp": "Financiamiento Práctico Empresarial",
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

def _extract_hydra_reply(data):
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return None

    direct_keys = (
        "reply", "response", "response_text", "answer", "message",
        "assistant_message", "text", "output", "content"
    )
    for key in direct_keys:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    nested = data.get("result")
    if isinstance(nested, dict):
        for key in direct_keys:
            val = nested.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    actions = data.get("actions")
    if isinstance(actions, list):
        for item in actions:
            if isinstance(item, dict):
                for key in ("message", "text", "body"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return None

def ask_hydra(phone: str, text: str, svc: str | None = None):
    if not HYDRA_URL:
        return None

    product_code = {
        "imss": "prestamo_imss",
        "auto": "seguro_auto",
        "vida": "vida_salud",
        "vrim": "vrim",
        "emp": "credito_empresarial",
        "fp": "financiamiento_practico",
    }.get(svc or "", "general")

    payload = {
        "event_id": str(uuid.uuid4()),
        "lead_id": phone,
        "event_type": "open_question",
        "product_code": product_code,
        "classification": {"intent": "open_question", "confidence": 0.93},
        "advisor_id": "don_chiwy",
        "channel": "whatsapp",
        "source": "bot_vicky_redes",
        "message_text": text,
        "metadata": {
            "nombre": _nombre(phone),
            "service_hint": svc or "general",
            "state": user_state.get(phone, ""),
        }
    }

    headers = {"Content-Type": "application/json"}
    if HYDRA_TOKEN:
        headers["X-Boardroom-Token"] = HYDRA_TOKEN

    try:
        log.info("hydra_trigger_start lead=%s svc=%s", phone, svc or "general")
        resp = requests.post(HYDRA_URL, json=payload, headers=headers, timeout=HYDRA_TIMEOUT)
        log.info("hydra_trigger status=%s lead=%s", resp.status_code, phone)
        if resp.status_code not in (200, 201, 202):
            log.warning("hydra_trigger_bad_status lead=%s body=%s", phone, resp.text[:250])
            return None
        try:
            data = resp.json()
        except Exception:
            log.warning("hydra_trigger_non_json lead=%s body=%s", phone, resp.text[:250])
            return None
        reply = _extract_hydra_reply(data)
        if reply:
            return reply
        log.warning("hydra_trigger_no_reply lead=%s", phone)
        return None
    except Exception as e:
        log.exception("hydra_trigger_failed lead=%s err=%s", phone, e)
        return None

def ask_hydra_or_gpt(phone: str, text: str, svc: str | None = None) -> str:
    hydra_reply = ask_hydra(phone, text, svc)
    if hydra_reply:
        return hydra_reply
    return ask_gpt(text, svc)

# ── Detección de servicio ─────────────────────────────────────────────────────
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
}

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
        send_msg(phone, "🚗 *Seguro de Auto Inbursa*\n\n"
                        "✅ Cobertura amplia  ✅ Robo total/parcial\n"
                        "✅ Responsabilidad civil  ✅ Asistencia vial 24/7\n\n"
                        "📞 Un asesor te contactará para cotizar.")
        notify_advisor(f"🚗 SEGURO AUTO\nWhatsApp: {phone}")
    elif svc == "vida":
        send_msg(phone, "🏥 *Seguro de Vida y Salud Inbursa*\n\n"
                        "✅ Seguro de vida  ✅ Gastos Médicos Mayores\n"
                        "✅ Hospitalización  ✅ Atención 24/7\n\n"
                        "📞 Un asesor te contactará.")
        notify_advisor(f"🏥 VIDA/SALUD\nWhatsApp: {phone}")
    elif svc == "vrim":
        send_msg(phone, "💳 *Tarjeta Médica VRIM*\n\n"
                        "✅ Consultas ilimitadas  ✅ Especialistas\n"
                        "✅ Laboratorios  ✅ Descuentos en medicamentos\n\n"
                        "📞 Un asesor te contactará.")
        notify_advisor(f"💳 VRIM\nWhatsApp: {phone}")

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
            user_state[phone] = "imss_q_pension"
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
            "💰 *Préstamo para Pensionados IMSS (Ley 73)*\n\n"
            "✅ Montos desde *$40,000 hasta $650,000*\n"
            "✅ Sin aval ni garantía\n"
            "✅ Descuento directo vía tu pensión\n"
            "✅ Depósito a tu cuenta en días\n\n"
            "*¿Ya eres pensionado o jubilado del IMSS bajo la Ley 73?*")
        user_state[phone] = "imss_q_califica"
        return

    if state == "imss_q_califica":
        r = yes_no(msg)
        if r == "si":
            send_msg(phone, "¡Perfecto! 👏\n"
                            "*¿Cuánto recibes al mes por concepto de pensión?* _(ej. 7500)_")
            user_state[phone] = "imss_q_pension"
        elif r == "no":
            send_msg(phone,
                "Entendido 🙏 Este financiamiento aplica para pensionados IMSS Ley 73.\n\n"
                "¿Te gustaría que un asesor te oriente sobre otras opciones?")
            user_state[phone] = "imss_no_califica"
        else:
            send_msg(phone, "Por favor responde *sí* o *no*. 😊")
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
        reset(phone)
        return

    if state == "imss_q_pension":
        m = extract_num(msg)
        if m is None:
            send_msg(phone, "Indícame el monto mensual de tu pensión _(ej. 6500)_.")
            return
        data["pension"] = m
        user_data[phone] = data
        if m < 5000:
            send_msg(phone,
                "Gracias 🙏 Por ahora los créditos aplican a pensiones desde *$5,000 mensuales*.\n\n"
                "¿Deseas que un asesor te contacte para explorar otras opciones?")
            user_state[phone] = "imss_pension_baja"
            return
        send_msg(phone, "Excelente 💪\n"
                        "*¿Qué monto deseas solicitar?* _(mínimo $40,000 — máximo $650,000)_")
        user_state[phone] = "imss_q_monto"
        return

    if state == "imss_pension_baja":
        if yes_no(msg) == "si":
            notify_advisor(f"🔔 PENSIÓN BAJA – IMSS\nWhatsApp: {phone}\n"
                           f"Pensión: ${data.get('pension', 'ND')}\n"
                           f"Origen: {data.get('origen', 'directo')}")
            send_msg(phone, "✅ ¡Listo! Un asesor te contactará con opciones para tu situación.")
        else:
            send_msg(phone, "Entendido 😊 Aquí estamos cuando lo necesites.")
        reset(phone)
        return

    if state == "imss_q_monto":
        m = extract_num(msg)
        if m is None or m < 40000:
            send_msg(phone, "Indica el monto deseado _(mínimo $40,000)_, ej. *65000*.")
            return
        data["monto"] = m
        user_data[phone] = data
        send_msg(phone, f"Anotado: *${m:,.0f}* ✅\n\n*¿Cuál es tu nombre completo?*")
        user_state[phone] = "imss_q_nombre"
        return

    if state == "imss_q_nombre":
        data["nombre"] = msg.title()
        user_data[phone] = data
        send_msg(phone, f"Mucho gusto, *{data['nombre']}* 😊\n\n"
                        "*¿Tu número de contacto?*\n"
                        "_(Escribe \"mismo\" si es este WhatsApp)_")
        user_state[phone] = "imss_q_tel"
        return

    if state == "imss_q_tel":
        data["tel"] = phone if msg.strip().lower() in ("mismo", "este", "el mismo") else msg.strip()
        user_data[phone] = data
        send_msg(phone, "*¿Ya recibes tu pensión en Inbursa?* "
                        "_(Sí / No — si es en otro banco está bien)_")
        user_state[phone] = "imss_q_inbursa"
        return

    if state == "imss_q_inbursa":
        r = yes_no(msg)
        if r == "?":
            send_msg(phone, "Por favor responde *sí* o *no*.")
            return
        data["inbursa"] = r
        user_data[phone] = data
        send_msg(phone,
            "✅ *¡Todo listo!* Solicitud registrada.\n"
            "Nuestro asesor *Christian López* te contactará a la brevedad. 🙌")
        notify_advisor(
            f"🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS LEY 73\n"
            f"────────────────────────\n"
            f"Nombre:   {data.get('nombre', 'ND')}\n"
            f"WhatsApp: {phone}\n"
            f"Teléfono: {data.get('tel', 'ND')}\n"
            f"Pensión:  ${data.get('pension', 0):,.0f}/mes\n"
            f"Monto:    ${data.get('monto', 0):,.0f}\n"
            f"Inbursa:  {data.get('inbursa', 'ND')}\n"
            f"Origen:   {data.get('origen', 'directo')}\n"
            "────────────────────────")
        # Auto-trigger Hydra al completar prospecto IMSS
        try:
            if HYDRA_URL:
                _hydra_payload = {
                    "event_id": str(uuid.uuid4()),
                    "lead_id": phone,
                    "event_type": "lead_new",
                    "product_code": "prestamo_imss",
                    "product_config": {
                        "product_code": "prestamo_imss",
                        "product_name": "Préstamo IMSS Ley 73",
                        "priority": "A",
                        "requirements": ["ine", "estado_de_cuenta"],
                        "stage_scripts": {
                            "qualification": "Hola, te comparto los requisitos para tu Préstamo IMSS.",
                            "default": "Seguimos con tu proceso COHIFIS."
                        },
                        "commission_rate": 0.08
                    },
                    "classification": {"intent": "lead_new", "confidence": 0.95},
                    "advisor_id": "don_chiwy",
                    "channel": "whatsapp",
                    "source": "bot_vicky_redes",
                    "metadata": {
                        "nombre": data.get("nombre", ""),
                        "pension": data.get("pension", ""),
                        "monto": data.get("monto", ""),
                        "inbursa": data.get("inbursa", "")
                    }
                }
                _headers = {"Content-Type": "application/json"}
                if HYDRA_TOKEN:
                    _headers["X-Boardroom-Token"] = HYDRA_TOKEN
                log.info("hydra_trigger_start lead=%s svc=imss_prospect", phone)
                _resp = requests.post(HYDRA_URL, json=_hydra_payload, headers=_headers, timeout=HYDRA_TIMEOUT)
                log.info("hydra_trigger status=%s lead=%s", _resp.status_code, phone)
                if _resp.status_code not in (200, 201, 202):
                    log.warning("hydra_trigger_bad_status lead=%s body=%s", phone, _resp.text[:250])
        except Exception as _e:
            log.exception("hydra_trigger_failed lead=%s err=%s", phone, _e)
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
        reset(phone)
        return

# ── Flujo Financiamiento Práctico ─────────────────────────────────────────────
_FP_STEPS = [
    ("fp_q1", "fp_q2", "¿Antigüedad fiscal de la empresa?"),
    ("fp_q2", "fp_q3", "¿Persona física con actividad empresarial o persona moral?"),
    ("fp_q3", "fp_q4", "¿Edad del representante legal?"),
    ("fp_q4", "fp_q5", "¿Buró de crédito empresa y accionistas al día? _(positivo/negativo)_"),
    ("fp_q5", "fp_q6", "¿Facturación anual aproximada?"),
    ("fp_q6", "fp_q7", "¿Facturación constante en los últimos 6 meses? _(Sí/No)_"),
    ("fp_q7", "fp_q8", "¿Monto de financiamiento requerido?"),
    ("fp_q8", "fp_q9", "¿Cuenta con opinión de cumplimiento SAT positiva?"),
    ("fp_q9", "fp_q10", "¿Qué tipo de financiamiento requiere?"),
    ("fp_q10", "fp_q11", "¿Tiene financiamiento activo actualmente? ¿Con quién?"),
    ("fp_q11", "fp_end", "📝 ¿Algún comentario adicional para el asesor?"),
]

def funnel_fp(phone: str, msg: str) -> None:
    state = user_state.get(phone, "fp_start")
    data = user_data.get(phone, {})

    if state == "fp_start":
        send_msg(phone,
            "💼 *Financiamiento Práctico Empresarial – Inbursa*\n\n"
            "⚡ Aprobación desde *24 horas* · Sin garantía · Desde *$100,000 MXN*\n"
            "Para empresas y personas físicas con actividad empresarial.\n\n"
            "¿Deseas saber si puedes acceder? _(Sí/No)_")
        user_state[phone] = "fp_q_interes"
        return

    if state == "fp_q_interes":
        r = yes_no(msg)
        if r == "si":
            send_msg(phone, "Excelente 🙌 Empecemos.\n*¿Cuál es el giro de tu empresa?*")
            user_state[phone] = "fp_q1"
        elif r == "no":
            notify_advisor(f"📩 NO INTERESADO – Financiamiento Práctico\nWhatsApp: {phone}")
            send_msg(phone, "Entendido 👍 Si deseas otro servicio, con gusto te oriento.")
            reset(phone)
        else:
            send_msg(phone, "Responde *sí* o *no*.")
        return

    for (cur, nxt, nxt_q) in _FP_STEPS:
        if state == cur:
            data[cur] = msg
            user_data[phone] = data
            user_state[phone] = nxt
            send_msg(phone, nxt_q)
            return

    if state == "fp_end":
        data["comentario"] = msg
        notify_advisor(
            f"🔔 PROSPECTO – FINANCIAMIENTO PRÁCTICO\n"
            f"WhatsApp: {phone}\n"
            f"Giro:           {data.get('fp_q1', 'ND')}\n"
            f"Antigüedad:     {data.get('fp_q2', 'ND')}\n"
            f"Tipo persona:   {data.get('fp_q3', 'ND')}\n"
            f"Edad rep legal: {data.get('fp_q4', 'ND')}\n"
            f"Buró:           {data.get('fp_q5', 'ND')}\n"
            f"Facturación:    {data.get('fp_q6', 'ND')}\n"
            f"Constante 6m:   {data.get('fp_q7', 'ND')}\n"
            f"Monto req.:     {data.get('fp_q8', 'ND')}\n"
            f"Opinión SAT:    {data.get('fp_q9', 'ND')}\n"
            f"Tipo financ.:   {data.get('fp_q10', 'ND')}\n"
            f"Financ. actual: {data.get('fp_q11', 'ND')}\n"
            f"Comentario:     {data.get('comentario', 'Ninguno')}")
        send_msg(phone, "✅ Listo. El asesor *Christian López* te contactará a la brevedad.")
        reset(phone)
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

    if msg_obj.get("type") != "text":
        _log(phone, _nombre(phone), f"[{msg_obj.get('type')}]", "entrante", "cliente", "", "", mid)
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

    state = user_state.get(phone, "")
    if state.startswith("imss_"):
        funnel_imss(phone, text)
        return
    if state.startswith("emp_"):
        funnel_emp(phone, text)
        return
    if state.startswith("fp_"):
        funnel_fp(phone, text)
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
        send_msg(phone, ask_hydra_or_gpt(phone, text, svc))
        return

    if svc:
        route(phone, svc)
        return

    if is_question and in_finance:
        send_msg(phone, ask_hydra_or_gpt(phone, text, svc))
        return

    if in_finance:
        send_msg(phone, ask_hydra_or_gpt(phone, text, svc))
        return

    show_menu(phone)

# ── Verificación de firma Meta (HMAC-SHA256) ──────────────────────────────────
def _verify_sig(raw: bytes, hdr: str) -> bool:
    if not APP_SECRET:
        return True
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
                for msg in (chg.get("value") or {}).get("messages", []):
                    handle(msg)
        return jsonify({"status": "ok"}), 200
    except Exception:
        log.exception("❌ webhook POST")
        return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "sheets": _srdy}), 200

@app.route("/ext/lead", methods=["POST"])
def ext_lead():
    try:
        if not INTERNAL_TOKEN:
            log.error("❌ INTERNAL_TOKEN no configurado")
            return jsonify({"ok": False, "error": "internal_token_not_configured"}), 500
        if not _is_internal_request(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        data = request.get_json(force=True, silent=True) or {}
        lead_id = str(data.get("lead_id", "")).strip()
        nombre = str(data.get("nombre", "")).strip() or "Sin nombre"
        telefono = re.sub(r"\D", "", str(data.get("telefono", "")))[-10:]
        interest = str(data.get("interest") or data.get("interes") or "").strip() or "sin_especificar"
        source = str(data.get("source", "")).strip() or "desconocido"
        if not lead_id:
            return jsonify({"ok": False, "error": "missing_lead_id"}), 422
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
        log.info("✅ /ext/lead OK [lead_id=%s]", lead_id)
        return jsonify({"ok": True}), 200
    except Exception as exc:
        log.exception("❌ Error en /ext/lead: %s", exc)
        return jsonify({"ok": False, "error": "internal_server_error"}), 500

# ── Arranque ──────────────────────────────────────────────────────────────────
_sheets_init()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"🚀 Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
