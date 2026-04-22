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
from types import SimpleNamespace

try:
    from authority_matrix import AuthorityActor, EventSeverity
    from hydra_orchestrator import ExecutionPlan, HydraOrchestrator, PlanDisposition
    from policy_envelope import (
        Agent,
        Channel as BoardroomChannel,
        Criticality,
        FallbackMode,
        PolicyEnvelope,
        Source,
        SystemOfRecord,
        TaskType,
    )
    from task import Task, TaskState
    from fallback_manager import FallbackManager, FallbackTrigger
    from flow_gemma_decision_layer import GemmaDecisionLayerFlow
    from notifier import BoardroomNotifier, NotificationConfig
    from valkey_store import (
        AgentTraceRecord,
        TraceStage,
        TraceStatus,
        ValkeyStore,
        ValkeyStoreConfig,
    )
    from sheets_ledger import IncidentCode, LedgerIncident, SheetsLedger, SheetsLedgerConfig
    _boardroom_libs = True
except Exception:
    _boardroom_libs = False

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

BOARDROOM_LEDGER_SHEET_ID = os.getenv("BOARDROOM_LEDGER_SHEET_ID", "").strip()
BOARDROOM_VALKEY_URL = (
    os.getenv("VALKEY_URL", "").strip()
    or os.getenv("REDIS_URL", "").strip()
    or os.getenv("KV_URL", "").strip()
)

if _boardroom_libs:
    # Shim de compatibilidad:
    # activation_criteria.py y flow_gemma_decision_layer.py consumen aliases que no
    # están expuestos por hydra_orchestrator.py en todos los ambientes cerrados v1.
    # Se agregan aquí sin alterar los módulos auditados.
    if not hasattr(HydraOrchestrator, "route_policy_envelope"):
        def _route_policy_envelope_compat(self, policy_envelope, *, trace_id=None,
                                          conversation_id=None, request_id=None,
                                          metadata=None):
            task = self.create_task(
                policy_envelope,
                trace_id=trace_id,
                conversation_id=conversation_id,
                request_id=request_id,
                metadata=metadata or {},
            )
            execution_plan = self.build_execution_plan(task)
            return SimpleNamespace(task=task, execution_plan=execution_plan)
        HydraOrchestrator.route_policy_envelope = _route_policy_envelope_compat

    if not hasattr(ExecutionPlan, "primary_agent"):
        ExecutionPlan.primary_agent = property(
            lambda self: Agent(self.selected_actor.value)
            if self.selected_actor.value in Agent._value2member_map_
            else None
        )
    if not hasattr(ExecutionPlan, "authority_events"):
        ExecutionPlan.authority_events = property(lambda self: list(self.events))
    if not hasattr(ExecutionPlan, "conflicts"):
        ExecutionPlan.conflicts = property(lambda self: [])
    if not hasattr(PlanDisposition, "ROUTED"):
        PlanDisposition.ROUTED = PlanDisposition.EXECUTE


STATE_TTL = 24 * 60 * 60

class StateStore:
    def __init__(self, ttl: int = STATE_TTL):
        self.ttl = ttl
        self._redis = None
        self._state_mem = {}
        self._data_mem = {}
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

# ── Boardroom Engine v1 / Hydra local ────────────────────────────────────────
_BOARDROOM_OWNER = "app_vicky_hydra_v2"
_BOARDROOM_MODULE = "app_vicky_hydra_v2"
_BOARDROOM_CONTRACT_VERSION = "boardroom.v1.agent-contract"

_boardroom_hydra = None
_boardroom_fallback = None
_boardroom_gemma_flow = None
_boardroom_notifier = None
_boardroom_valkey = None
_boardroom_ledger = None


def _boardroom_enabled() -> bool:
    return _boardroom_libs and _boardroom_hydra is not None


def _init_boardroom_runtime() -> None:
    global _boardroom_hydra, _boardroom_fallback, _boardroom_gemma_flow
    global _boardroom_notifier, _boardroom_valkey, _boardroom_ledger

    if not _boardroom_libs:
        log.warning("⚠️ Boardroom Engine v1 no disponible; Hydra local deshabilitado.")
        return

    try:
        _boardroom_hydra = HydraOrchestrator(default_owner=_BOARDROOM_OWNER)
        _boardroom_fallback = FallbackManager(orchestrator=_boardroom_hydra)
        log.info("✅ Hydra local Boardroom v1 inicializado.")
    except Exception:
        _boardroom_hydra = None
        _boardroom_fallback = None
        log.exception("❌ No se pudo inicializar Hydra local Boardroom v1.")
        return

    if BOARDROOM_VALKEY_URL:
        try:
            _boardroom_valkey = ValkeyStore(
                ValkeyStoreConfig(redis_url=BOARDROOM_VALKEY_URL)
            )
            log.info("✅ Boardroom ValkeyStore inicializado.")
        except Exception:
            _boardroom_valkey = None
            log.exception("❌ No se pudo inicializar Boardroom ValkeyStore.")
    else:
        log.warning("⚠️ VALKEY_URL/REDIS_URL/KV_URL no configurado para Boardroom ValkeyStore.")

    if META_TOKEN and WABA_ID and ADVISOR_NUM:
        try:
            _boardroom_notifier = BoardroomNotifier(
                NotificationConfig(
                    meta_token=META_TOKEN,
                    waba_phone_id=WABA_ID,
                    approver_whatsapp=ADVISOR_NUM,
                    valkey_url=BOARDROOM_VALKEY_URL or None,
                    dry_run=False,
                )
            )
            log.info("✅ BoardroomNotifier inicializado.")
        except Exception:
            _boardroom_notifier = None
            log.exception("❌ No se pudo inicializar BoardroomNotifier.")

    if _boardroom_valkey and BOARDROOM_LEDGER_SHEET_ID and GG_CREDS:
        try:
            _boardroom_ledger = SheetsLedger(
                config=SheetsLedgerConfig(
                    sheet_id=BOARDROOM_LEDGER_SHEET_ID,
                    credentials_json=GG_CREDS,
                    dry_run=False,
                ),
                valkey_store=_boardroom_valkey,
            )
            log.info("✅ Boardroom SheetsLedger inicializado.")
        except Exception:
            _boardroom_ledger = None
            log.exception("❌ No se pudo inicializar Boardroom SheetsLedger.")

    try:
        _boardroom_gemma_flow = GemmaDecisionLayerFlow(
            fallback_manager=_boardroom_fallback,
            notifier=_boardroom_notifier,
            auto_init_notifier=False,
        )
        log.info("✅ GemmaDecisionLayerFlow inicializado.")
    except Exception:
        _boardroom_gemma_flow = None
        log.exception("❌ No se pudo inicializar GemmaDecisionLayerFlow.")


def _boardroom_trace(task: Task | None, actor: AuthorityActor,
                     stage: TraceStage, status: TraceStatus, summary: str,
                     metadata: dict | None = None) -> None:
    if not (_boardroom_valkey and task):
        return
    try:
        _boardroom_valkey.append_agent_trace(
            AgentTraceRecord(
                task_id=task.task_id,
                actor=actor,
                stage=stage,
                status=status,
                summary=summary,
                metadata=metadata or {},
            )
        )
    except Exception:
        log.exception("❌ Error persistiendo AgentTraceRecord en Valkey.")


def _boardroom_record_task(task: Task | None, authority_events: list | None = None) -> None:
    if task is None:
        return
    authority_events = authority_events or []

    if _boardroom_valkey:
        try:
            _boardroom_valkey.write_task_ledger(task)
            for event in authority_events:
                _boardroom_valkey.append_authority_event(event)
        except Exception:
            log.exception("❌ Error persistiendo task/eventos en Boardroom ValkeyStore.")

    if _boardroom_ledger:
        try:
            _boardroom_ledger.record_task_snapshot(task)
            for event in authority_events:
                _boardroom_ledger.append_authority_event(event)
        except Exception:
            log.exception("❌ Error persistiendo task/eventos en Boardroom SheetsLedger.")


def _boardroom_record_incident(task: Task | None, incident_code: IncidentCode,
                               summary: str, *, severity: EventSeverity = EventSeverity.HIGH,
                               reason_code: str | None = None,
                               actor: str | None = None,
                               metadata: dict | None = None) -> None:
    if not (_boardroom_ledger and task):
        return
    try:
        _boardroom_ledger.append_incident(
            LedgerIncident(
                incident_code=incident_code,
                task_id=task.task_id,
                summary=summary,
                severity=severity,
                source=_BOARDROOM_MODULE,
                current_state=task.current_state.value,
                reason_code=reason_code,
                actor=actor,
                metadata=metadata or {},
            )
        )
    except Exception:
        log.exception("❌ Error registrando incidente Boardroom.")


def _boardroom_notify(task: Task | None, *, authority_events: list | None = None,
                      summary: str | None = None) -> None:
    authority_events = authority_events or []
    delivered = False

    if _boardroom_notifier and task is not None:
        try:
            for event in authority_events:
                result = _boardroom_notifier.notify_authority_event(
                    task, event, source_module=_BOARDROOM_MODULE
                )
                if result is not None and getattr(result.status, "value", "") in {"sent", "persisted_only"}:
                    delivered = True

            result = _boardroom_notifier.notify_task_state(
                task,
                triggered_by=_BOARDROOM_OWNER,
                source_module=_BOARDROOM_MODULE,
                summary=summary,
            )
            if result is not None and getattr(result.status, "value", "") in {"sent", "persisted_only"}:
                delivered = True
        except Exception:
            log.exception("❌ Error enviando notificación Boardroom.")

    if not delivered and summary:
        notify_advisor(f"📣 BOARDROOM\nTarea: {task.task_id if task else 'ND'}\n{summary}")


def _boardroom_apply_failure_fallback(task: Task | None, summary: str,
                                      *, metadata: dict | None = None) -> None:
    if task is None or _boardroom_fallback is None:
        return
    try:
        resolution = _boardroom_fallback.handle_event(
            task,
            AuthorityActor.HYDRA,
            FallbackTrigger.FAILURE,
            summary=summary,
            metadata=metadata or {},
        )
        _boardroom_record_task(task, authority_events=list(resolution.record.authority_events))
        _boardroom_notify(
            task,
            authority_events=list(resolution.record.authority_events),
            summary=summary,
        )
        incident_code = IncidentCode.HOLD if task.current_state == TaskState.HOLD else IncidentCode.FAILED
        _boardroom_record_incident(
            task,
            incident_code,
            summary,
            reason_code=resolution.record.code.value,
            actor=AuthorityActor.HYDRA.value,
            metadata=metadata or {},
        )
    except Exception:
        log.exception("❌ Error aplicando fallback Boardroom.")


def _service_to_product_code(svc: str | None) -> str:
    return {
        "imss": "prestamo_imss",
        "auto": "seguro_auto",
        "vida": "vida_salud",
        "vrim": "vrim",
        "emp": "credito_empresarial",
        "fp": "financiamiento_practico",
    }.get((svc or "").strip(), "general")


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


def _build_conversation_policy(phone: str, text: str, svc: str | None = None) -> PolicyEnvelope:
    return PolicyEnvelope(
        task_id=str(uuid.uuid4()),
        source=Source.CLIENT,
        channel=BoardroomChannel.WHATSAPP,
        task_type=TaskType.CONVERSATION,
        intent=_conversation_intent(text),
        criticality=Criticality.LOW,
        confidence=0.92,
        requires_google_state=False,
        requires_audit=False,
        requires_human_approval=False,
        fast_path=False,
        allowed_agents=[Agent.GEMMA],
        system_of_record=SystemOfRecord.INTERNAL,
        fallback_mode=FallbackMode.SAFE_REPLY,
        audit_required_for_release=False,
        notify_on_waiting_audit=False,
    )


def _build_lead_policy(phone: str, product_code: str, confidence: float = 0.95) -> PolicyEnvelope:
    # Supuesto operativo:
    # Boardroom v1 no define task_type comercial. Para no alterar módulos cerrados,
    # lead_new se modela como conversación escalada a autoridad humana, conservando
    # el contrato semántico real en metadata: hydra_event_type=lead_new.
    return PolicyEnvelope(
        task_id=str(uuid.uuid4()),
        source=Source.CLIENT,
        channel=BoardroomChannel.WHATSAPP,
        task_type=TaskType.CONVERSATION,
        intent="schedule_followup",
        criticality=Criticality.MEDIUM,
        confidence=round(float(confidence), 4),
        requires_google_state=False,
        requires_audit=False,
        requires_human_approval=True,
        fast_path=False,
        allowed_agents=[Agent.GEMMA],
        system_of_record=SystemOfRecord.INTERNAL,
        fallback_mode=FallbackMode.MANUAL_REVIEW,
        audit_required_for_release=False,
        notify_on_waiting_audit=False,
    )


def _build_gemma_output_payload(task: Task, text: str, svc: str | None = None) -> dict:
    service_hint = (svc or "").strip()
    intent, response_text = _safe_reply_for_service(text, service_hint)
    business_action_requested, persistent_state_mutation_requested, business_data = _requires_human_guardrail(text)
    decision = "conversation_request_clarification" if service_hint == "" else "conversation_safe_reply"
    result_data = {
        "intent": intent,
        "catalog_intent": intent,
        "catalog_match": True,
        "out_of_catalog": False,
        "business_action_requested": business_action_requested,
        "persistent_state_mutation_requested": persistent_state_mutation_requested,
        "business_data": business_data,
        "sensitive_action": business_action_requested or persistent_state_mutation_requested,
        "service_hint": service_hint or "general",
    }
    if decision == "conversation_request_clarification":
        result_data["clarification"] = response_text
    else:
        result_data["reply"] = response_text

    return {
        "task_id": task.task_id,
        "agent": Agent.GEMMA.value,
        "contract_version": _BOARDROOM_CONTRACT_VERSION,
        "decision": decision,
        "confidence": 0.92,
        "status": "completed",
        "result_data": result_data,
    }


def _build_standard_hydra_response(task: Task, plan, *, reply_text: str | None,
                                   mode: str, summary: str,
                                   metadata: dict | None = None) -> dict:
    return {
        "task_id": task.task_id,
        "status": "ok",
        "response": {
            "reply_text": reply_text,
            "mode": mode,
            "summary": summary,
        },
        "task": task.to_dict(),
        "execution_plan": plan.to_dict() if plan is not None else None,
        "metadata": metadata or {},
    }


def _conversation_hold_reply() -> str:
    return (
        "Gracias por tu mensaje. Para darte una respuesta exacta lo canalicé con el asesor. "
        "¿Me compartes tu nombre completo para seguimiento?"
    )


_init_boardroom_runtime()

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
    if not isinstance(data, dict):
        return None
    response = data.get("response")
    if not isinstance(response, dict):
        return None
    for key in ("reply_text", "safe_reply", "clarification"):
        val = response.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _hydra_post_with_retry(url: str, payload: dict, headers: dict, timeout: int, lead: str):
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            log.info("hydra_trigger status=%s lead=%s attempt=%s", resp.status_code, lead, attempt + 1)
            if resp.status_code in (200, 201, 202):
                return resp
            log.warning("hydra_trigger_bad_status lead=%s attempt=%s body=%s", lead, attempt + 1, resp.text[:250])
        except Exception as e:
            if attempt == 2:
                log.exception("hydra_trigger_failed lead=%s err=%s", lead, e)
            else:
                log.warning("hydra_trigger_retry lead=%s attempt=%s err=%s", lead, attempt + 1, e)
        if attempt < 2:
            time.sleep(1)
    return None

def _trigger_hydra_lead(phone: str, product_code: str, metadata: dict | None = None,
                        product_name: str | None = None, product_config: dict | None = None,
                        confidence: float = 0.95, source: str = "bot_vicky_redes",
                        svc_log: str | None = None):
    if not _boardroom_enabled():
        log.error("❌ Hydra local Boardroom v1 no está disponible para lead_new.")
        return None

    metadata = dict(metadata or {})
    metadata.update({
        "hydra_event_type": "lead_new",
        "product_code": product_code,
        "product_name": product_name or product_code,
        "product_config": product_config or {},
        "channel": "whatsapp",
        "source": source,
        "service_log": svc_log or product_code,
        "phone": phone,
    })

    task = None
    try:
        policy = _build_lead_policy(phone, product_code, confidence=confidence)
        task, plan = _boardroom_hydra.orchestrate_policy(
            policy,
            trace_id=str(uuid.uuid4()),
            conversation_id=re.sub(r"\D", "", str(phone)),
            request_id=_mid() or str(uuid.uuid4()),
            metadata=metadata,
            apply_plan=True,
        )
        _boardroom_trace(
            task,
            AuthorityActor.HYDRA,
            TraceStage.ROUTING,
            TraceStatus.SUCCEEDED,
            f"Lead '{product_code}' registrado en Hydra local.",
            metadata={"svc_log": svc_log or product_code, "hydra_event_type": "lead_new"},
        )
        _boardroom_record_task(task, authority_events=list(plan.events))
        _boardroom_notify(
            task,
            authority_events=list(plan.events),
            summary=f"Lead '{product_code}' escalado para seguimiento comercial.",
        )
        return _build_standard_hydra_response(
            task,
            plan,
            reply_text=None,
            mode="manual_review",
            summary="Lead registrado y escalado a revisión humana.",
            metadata={"hydra_event_type": "lead_new"},
        )
    except Exception as exc:
        log.exception("❌ Error registrando lead_new en Hydra local: %s", exc)
        if task is not None:
            _boardroom_trace(
                task,
                AuthorityActor.HYDRA,
                TraceStage.ROUTING,
                TraceStatus.FAILED,
                "Fallo registrando lead_new en Hydra local.",
                metadata={"error": str(exc)[:300], "hydra_event_type": "lead_new"},
            )
            _boardroom_apply_failure_fallback(
                task,
                "Falló el registro del lead en Hydra local.",
                metadata={"error": str(exc)[:300], "hydra_event_type": "lead_new"},
            )
        return None


def ask_hydra(phone: str, text: str, svc: str | None = None):
    if not _boardroom_enabled():
        return None

    task = None
    try:
        policy = _build_conversation_policy(phone, text, svc)
        task, plan = _boardroom_hydra.orchestrate_policy(
            policy,
            trace_id=str(uuid.uuid4()),
            conversation_id=re.sub(r"\D", "", str(phone)),
            request_id=_mid() or str(uuid.uuid4()),
            metadata={
                "hydra_event_type": "conversation_message",
                "message_text": text,
                "service_hint": svc or "general",
                "nombre": _nombre(phone),
                "state": user_state.get(phone, ""),
                "phone": phone,
                "product_code": _service_to_product_code(svc),
            },
            apply_plan=True,
        )
        _boardroom_trace(
            task,
            AuthorityActor.HYDRA,
            TraceStage.ROUTING,
            TraceStatus.SUCCEEDED,
            "Conversación enrutada por Hydra local.",
            metadata={"service_hint": svc or "general", "hydra_event_type": "conversation_message"},
        )
        _boardroom_record_task(task, authority_events=list(plan.events))

        if _boardroom_gemma_flow is None:
            raise RuntimeError("GemmaDecisionLayerFlow no está disponible.")

        gemma_payload = _build_gemma_output_payload(task, text, svc)
        flow_result = _boardroom_gemma_flow.receive_gemma_capture(task, gemma_payload)
        authority_events = list(flow_result.authority_events)
        if flow_result.fallback_resolution is not None:
            authority_events.extend(list(flow_result.fallback_resolution.record.authority_events))

        _boardroom_record_task(task, authority_events=authority_events)

        if task.current_state == TaskState.DONE and task.metadata.get("safe_reply_text"):
            payload = _build_standard_hydra_response(
                task,
                plan,
                reply_text=str(task.metadata.get("safe_reply_text")).strip(),
                mode="safe_reply",
                summary="Conversation_message resuelta por flow_gemma_decision_layer.",
                metadata={"hydra_event_type": "conversation_message"},
            )
            return _extract_hydra_reply(payload)

        if task.current_state in {TaskState.ESCALATED, TaskState.HOLD, TaskState.WAITING_AUDIT, TaskState.FAILED}:
            summary = (
                "La conversación requiere intervención del asesor."
                if task.current_state == TaskState.ESCALATED
                else "La conversación quedó en revisión por Hydra."
            )
            _boardroom_notify(task, authority_events=authority_events, summary=summary)
            payload = _build_standard_hydra_response(
                task,
                plan,
                reply_text=_conversation_hold_reply(),
                mode=task.current_state.value.lower(),
                summary=summary,
                metadata={"hydra_event_type": "conversation_message"},
            )
            return _extract_hydra_reply(payload)

        return None
    except Exception as exc:
        log.exception("❌ Error en conversación Hydra local: %s", exc)
        if task is not None:
            _boardroom_trace(
                task,
                AuthorityActor.HYDRA,
                TraceStage.ROUTING,
                TraceStatus.FAILED,
                "Fallo procesando conversation_message en Hydra local.",
                metadata={"error": str(exc)[:300], "service_hint": svc or "general"},
            )
            _boardroom_apply_failure_fallback(
                task,
                "Falló el procesamiento conversacional en Hydra local.",
                metadata={"error": str(exc)[:300], "hydra_event_type": "conversation_message"},
            )
        return None


def ask_hydra_or_gpt(phone: str, text: str, svc: str | None = None) -> str:
    hydra_reply = ask_hydra(phone, text, svc)
    if hydra_reply:
        return hydra_reply
    notify_advisor(
        f"📣 BOARDROOM HOLD – CONVERSACIÓN\n"
        f"WhatsApp: {phone}\n"
        f"Servicio: {svc or 'general'}\n"
        f"Mensaje: {text[:400]}"
    )
    return _conversation_hold_reply()

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
        _trigger_hydra_lead(
            phone,
            "prestamo_imss",
            metadata={
                "nombre": data.get("nombre", ""),
                "pension": data.get("pension", ""),
                "monto": data.get("monto", ""),
                "inbursa": data.get("inbursa", "")
            },
            product_config={
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
            confidence=0.95,
            source="bot_vicky_redes",
            svc_log="imss_prospect"
        )
        reset(phone)
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
        _trigger_hydra_lead(
            phone,
            "seguro_auto",
            metadata={
                "nombre": data.get("nombre", ""),
                "tel": data.get("tel", ""),
                "tiene_seguro_actual": data.get("tiene_seguro_actual", ""),
                "marca_modelo": data.get("marca_modelo", ""),
                "ano": data.get("ano", "")
            },
            product_name="Seguro de Auto Inbursa",
            source="bot_vicky_redes",
            svc_log="auto_prospect"
        )
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
        _trigger_hydra_lead(
            phone,
            "vida_salud",
            metadata={
                "nombre": data.get("nombre", ""),
                "tel": data.get("tel", ""),
                "tipo_cobertura": data.get("tipo_cobertura", ""),
                "edad": data.get("edad", "")
            },
            product_name="Seguro de Vida y Salud Inbursa",
            source="bot_vicky_redes",
            svc_log="vida_prospect"
        )
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
        _trigger_hydra_lead(
            phone,
            "vrim",
            metadata={
                "nombre": data.get("nombre", ""),
                "tel": data.get("tel", ""),
                "personas": data.get("personas", "")
            },
            product_name="Tarjeta Médica VRIM",
            source="bot_vicky_redes",
            svc_log="vrim_prospect"
        )
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

        svc = detect_svc(interest) or ""
        hydra_result = _trigger_hydra_lead(
            telefono,
            _service_to_product_code(svc),
            metadata={
                "lead_id": lead_id,
                "nombre": nombre,
                "telefono": telefono,
                "interest": interest,
                "source": source,
                "hydra_event_type": "lead_new",
                "service_hint": svc or "general",
            },
            product_name=interest,
            source=source,
            svc_log="ext_lead",
        )
        if hydra_result is None:
            log.error("❌ /ext/lead no pudo registrar la tarea en Hydra local [lead_id=%s]", lead_id)
            return jsonify({"ok": False, "error": "hydra_task_registration_failed"}), 502

        task_id = str(hydra_result.get("task_id") or "").strip()
        execution_plan = hydra_result.get("execution_plan") or {}
        disposition = execution_plan.get("disposition") if isinstance(execution_plan, dict) else None

        log.info("✅ /ext/lead OK [lead_id=%s task_id=%s]", lead_id, task_id or "nd")
        return jsonify({
            "ok": True,
            "task_id": task_id,
            "disposition": disposition,
            "hydra_event_type": "lead_new",
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
