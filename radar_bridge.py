"""Independent Redes connector. No changes to WhatsApp payloads or replies."""
import json
import logging
import os
import re
import threading
import uuid

from radar_events import (EventLog, RadarClient, EVENTS_HEADER, EVENTS_TAB,
                          build_event, canonical_ts, statuses_from_value, STATUS_TO_EVENT)
from radar_outbox import OutboxWorker, SheetsOutbox

log = logging.getLogger("vicky-redes.radar")
LEAD_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://cohifis.com.mx/vicky/redes/leads")


def normalize_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 13 and digits.startswith("521"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("52"):
        digits = digits[2:]
    if len(digits) != 10:
        raise ValueError("Se requiere un telefono mexicano valido")
    return "521" + digits


def lead_id_for(phone):
    return "RS-" + str(uuid.uuid5(LEAD_NAMESPACE, normalize_phone(phone)))


class RadarBridge:
    def __init__(self, *, phone_id, advisor, credentials, sheet_id, record_enabled=False, client=None):
        self.phone_id, self.advisor = str(phone_id or ""), str(advisor or "")
        self.credentials, self.sheet_id = credentials, sheet_id
        self.record_enabled = record_enabled
        self._local = threading.local()
        self._tab_lock = threading.Lock()
        self._tab_ready = False
        self.client = client or RadarClient(url="", token="", hmac_secret="")
        self.log = EventLog(self._append)
        self.worker = OutboxWorker(lambda: SheetsOutbox(self._service(), self.sheet_id), self.client)

    def _service(self):
        if not self.credentials or not self.sheet_id:
            raise RuntimeError("Sheets no configurado para la bitacora Redes")
        if not hasattr(self._local, "service"):
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
            credentials = Credentials.from_service_account_info(json.loads(self.credentials), scopes=["https://www.googleapis.com/auth/spreadsheets"])
            self._local.service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._local.service

    def _append(self, tab, row):
        service = self._service()
        with self._tab_lock:
            if not self._tab_ready:
                meta = service.spreadsheets().get(spreadsheetId=self.sheet_id, fields="sheets.properties.title").execute()
                if tab not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
                    service.spreadsheets().batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
                header = service.spreadsheets().values().get(spreadsheetId=self.sheet_id, range=f"{tab}!A1:R1").execute().get("values", [])
                if header and header[0] != EVENTS_HEADER:
                    raise ValueError("Cabecera de bitacora incompatible")
                if not header:
                    service.spreadsheets().values().update(spreadsheetId=self.sheet_id, range=f"{tab}!A1:R1", valueInputOption="RAW", body={"values": [EVENTS_HEADER]}).execute()
                self._tab_ready = True
        response = service.spreadsheets().values().append(spreadsheetId=self.sheet_id, range=f"{tab}!A:R", valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [row]}).execute()
        match = re.search(r"!\D+(\d+)", (response.get("updates") or {}).get("updatedRange", ""))
        return int(match.group(1)) if match else None

    def start(self):
        try:
            self.worker.start()
        except Exception as exc:
            log.warning("Recuperacion Radar pendiente (%s)", type(exc).__name__)

    def record(self, event_type, phone, **kwargs):
        if not self.record_enabled or not self.phone_id:
            return
        try:
            phone = normalize_phone(phone)
            if self.advisor and phone == normalize_phone(self.advisor):
                return
            event = build_event(event_type, lead_id=lead_id_for(phone), phone_e164=phone,
                                phone_last10=phone[-10:], phone_number_id=self.phone_id, **kwargs)
            if self.log.record(event) is not None:
                self.start()
        except Exception as exc:
            log.warning("Registro Radar pendiente (%s)", type(exc).__name__)

    def requested(self, payload, request_id):
        try:
            self.record("message_requested", payload.get("to"), request_id=request_id,
                        delivery_status="requested", template=(payload.get("template") or {}).get("name"))
        except Exception as exc:
            log.warning("Solicitud sin registrar en Radar (%s)", type(exc).__name__)

    def response(self, payload, request_id, response):
        if not self.record_enabled:
            return
        try:
            body = response.json()
            messages = body.get("messages") or []
            if response.status_code in (200, 201) and messages and messages[0].get("id"):
                self.record("message_sent", payload.get("to"), request_id=request_id, wamid=messages[0]["id"], delivery_status="sent", template=(payload.get("template") or {}).get("name"))
            elif response.status_code not in (200, 201):
                error = body.get("error") or {}
                self.record("message_failed", payload.get("to"), request_id=request_id, delivery_status="failed", error_code=error.get("code") if isinstance(error.get("code"), int) else None, error_title=str(error.get("message") or "")[:200])
        except Exception as exc:
            log.warning("Acuse Meta sin interpretar para Radar (%s)", type(exc).__name__)

    def webhook(self, value):
        if not self.record_enabled or str((value.get("metadata") or {}).get("phone_number_id") or "") != self.phone_id:
            return
        for msg in value.get("messages") or []:
            try:
                self.record("message_inbound", msg.get("from"), wamid=msg.get("id"), direction="inbound", occurred_at=canonical_ts(msg.get("timestamp")), text=(msg.get("text") or {}).get("body"))
            except Exception as exc:
                log.warning("Mensaje entrante sin registrar en Radar (%s)", type(exc).__name__)
        for status in statuses_from_value(value):
            event_type = STATUS_TO_EVENT.get(status["status"])
            if event_type:
                self.record(event_type, status["recipient"], wamid=status["wamid"], occurred_at=status["occurred_at"], delivery_status=status["status"], error_code=status["error_code"], error_title=status["error_title"])


def from_environment(*, phone_id, advisor, credentials, sheet_id):
    truth = lambda key: os.getenv(key, "false").strip().lower() in ("1", "true", "yes", "on")
    return RadarBridge(phone_id=phone_id, advisor=advisor, credentials=credentials, sheet_id=sheet_id,
                       record_enabled=truth("RADAR_RECORD_ENABLED"), client=RadarClient(
                           url=os.getenv("RADAR_EVENTS_URL", ""), token=os.getenv("RADAR_VICKY_TOKEN", ""),
                           hmac_secret=os.getenv("RADAR_VICKY_HMAC_SECRET", ""), dispatch_token=os.getenv("RADAR_SITE_DISPATCH_TOKEN", ""), enabled=truth("RADAR_EMIT_ENABLED")))
