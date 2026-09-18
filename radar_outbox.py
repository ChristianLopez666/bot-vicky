"""Replay the existing durable Sheets log; never send WhatsApp messages.

Runs inside the existing web process, only with RADAR_EMIT_ENABLED. Each
thread owns its Google client. No new service, dependency or paid queue.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from radar_events import (DETALLE_OMITIDO, EVENTS_HEADER, EVENTS_TAB, PENDIENTE, ENVIADO,
                          RECHAZADO, RECHAZADO_AUTH, canonical_ts)

log = logging.getLogger("vicky-redes.radar.outbox")
OUTBOUND = {"message_requested", "message_sent", "message_delivered", "message_read", "message_failed", "advisor_notified"}


def request_key(event):
    return (event.get("source"), (event.get("channel") or {}).get("phone_number_id"),
            (event.get("lead") or {}).get("lead_id"), (event.get("message") or {}).get("wamid"))


def correlation_index(rows):
    result = {}
    for row in rows:
        if len(row) < 9 or not row[8] or not row[7]:
            continue
        try:
            if uuid.UUID(row[7]).version != 4:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        result.setdefault((row[3], row[4], row[5], row[8]), set()).add(row[7])
    return result


def correlated_event(event, index):
    event = copy.deepcopy(event)
    message = event.setdefault("message", {})
    if event.get("event_type") in OUTBOUND and not event.get("backfill") and not message.get("request_id"):
        candidates = index.get(request_key(event), set())
        if len(candidates) != 1:
            return None
        message["request_id"] = next(iter(candidates))
    return event


def retry_due(row, now):
    # RECHAZADO_AUTH no es terminal: el evento era valido, lo que fallo fue la
    # credencial. Solo llega aqui si el circuito ya permite enviar.
    if row.get("radar_state") not in (PENDIENTE, RECHAZADO_AUTH):
        return False
    try:
        attempts = max(0, int(row.get("radar_attempts") or 0))
        last = row.get("radar_last_try")
        if not last:
            return True
        elapsed = now - datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        return elapsed >= min(900, 60 * 2 ** min(attempts, 4))
    except (ValueError, TypeError, OverflowError):
        return True


CIRCUIT_TAB = "RADAR_CIRCUITO_REDES"
# Sin cuerpos ni texto libre: codigos de allowlist, huellas y fechas.
CIRCUIT_HEADER = ["estado", "huella_config", "http", "error", "detalle", "huella_respuesta",
                  "abierto_en", "proximo_intento"]
ABIERTO = "ABIERTO"
CERRADO = "CERRADO"


class SheetsOutbox:
    def __init__(self, service, spreadsheet_id):
        self.service = service
        self.values = service.spreadsheets().values()
        self.spreadsheet_id = spreadsheet_id
        self.index = None

    # -- Circuito de autenticacion: una fila en su propia pestana --
    def circuit_get(self):
        try:
            rows = self.read(f"{CIRCUIT_TAB}!A1:H2")
        except Exception as exc:
            # Pestana inexistente = el circuito nunca se abrio. Cualquier otro
            # error se propaga: sin conocer el estado no se envia nada.
            if "Unable to parse range" in str(exc):
                return None
            raise
        if len(rows) < 2 or rows[0] != CIRCUIT_HEADER:
            return None
        return dict(zip(CIRCUIT_HEADER, rows[1] + [""] * (len(CIRCUIT_HEADER) - len(rows[1]))))

    def circuit_put(self, record):
        meta = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id,
                                               fields="sheets.properties.title").execute()
        if CIRCUIT_TAB not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": CIRCUIT_TAB}}}]}).execute()
        fila = [str(record.get(k, "")) for k in CIRCUIT_HEADER]
        self.values.update(spreadsheetId=self.spreadsheet_id, range=f"{CIRCUIT_TAB}!A1:H2",
                           valueInputOption="RAW", body={"values": [CIRCUIT_HEADER, fila]}).execute()

    def read(self, range_name):
        return self.values.get(spreadsheetId=self.spreadsheet_id, range=range_name).execute().get("values", [])

    def page(self, start, size):
        self.index = None
        header = self.read(f"{EVENTS_TAB}!A1:R1")
        if not header or header[0] != EVENTS_HEADER:
            raise ValueError("La cabecera de EVENTOS_RADAR no coincide; no se modifica.")
        return self.read(f"{EVENTS_TAB}!A{start}:R{start + size - 1}")

    def correlations(self):
        if self.index is None:
            self.index = correlation_index(self.read(f"{EVENTS_TAB}!A2:I"))
        return self.index

    def mark(self, number, event_id, state, attempts, now):
        # Do not write to a different event if a human moved/deleted a row.
        latest = self.read(f"{EVENTS_TAB}!A{number}:R{number}")
        if not latest or latest[0][0] != event_id:
            raise ValueError("La fila cambio de identidad; se releera en el siguiente ciclo.")
        row = dict(zip(EVENTS_HEADER, latest[0]))
        if row.get("radar_state") in (ENVIADO, RECHAZADO):
            return  # terminales; RECHAZADO_AUTH si se reescribe
        attempts = max(attempts, int(row.get("radar_attempts") or 0))
        self.values.update(spreadsheetId=self.spreadsheet_id,
                           range=f"{EVENTS_TAB}!O{number}:Q{number}", valueInputOption="RAW",
                           body={"values": [[state, str(attempts), canonical_ts(now)]]}).execute()


def _probe_seconds():
    try:
        return max(300, int(os.getenv("RADAR_AUTH_PROBE_SECONDS", "21600")))
    except ValueError:
        return 21600


class AuthCircuit:
    """Circuito persistente para los 401/403 de Radar.

    Un rechazo de autenticacion abre el circuito y lo escribe en la hoja, asi
    que reiniciar el worker ya no vuelve a golpear a Radar con la misma
    configuracion rota (17-sep: siete reinicios = siete 403). Se cierra solo
    si cambia la configuracion (huella distinta) o si una prueba espaciada
    (un evento cada RADAR_AUTH_PROBE_SECONDS, 6 h por defecto) entra con
    exito, por si la correccion se hizo del lado de Radar.
    """

    def __init__(self, store, fingerprint, probe_seconds=None):
        self.store, self.fingerprint = store, fingerprint
        self.probe_seconds = probe_seconds or _probe_seconds()
        self._record = store.circuit_get()

    def is_open(self):
        r = self._record or {}
        return r.get("estado") == ABIERTO and r.get("huella_config") == self.fingerprint

    def allows(self, now):
        if not self.is_open():
            return True
        try:
            return now >= float(self._record.get("proximo_intento") or 0)
        except (TypeError, ValueError):
            return True

    def opened(self, now, resumen):
        resumen = resumen or {}
        self._record = {"estado": ABIERTO, "huella_config": self.fingerprint,
                        "http": resumen.get("http", ""), "error": resumen.get("error", DETALLE_OMITIDO),
                        "detalle": resumen.get("detalle", DETALLE_OMITIDO),
                        "huella_respuesta": resumen.get("huella", ""),
                        "abierto_en": canonical_ts(now),
                        "proximo_intento": str(int(now + self.probe_seconds))}
        self.store.circuit_put(self._record)
        log.error("RADAR_CIRCUITO_ABIERTO http=%s error=%s detalle=%s proxima_prueba_en_s=%s",
                  self._record["http"], self._record["error"], self._record["detalle"], self.probe_seconds)

    def succeeded(self):
        if (self._record or {}).get("estado") != ABIERTO:
            return
        self._record = dict(self._record, estado=CERRADO)
        self.store.circuit_put(self._record)
        log.warning("RADAR_CIRCUITO_CERRADO: Radar volvio a aceptar eventos de Redes")


class OutboxPump:
    def __init__(self, repository, client, page_size=200, limit=10, clock=time.time, circuit=None):
        self.repository, self.client = repository, client
        self.page_size, self.limit, self.clock = page_size, limit, clock
        self.circuit = circuit
        self.cursor = 2

    def run_once(self):
        if not self.client.configured():
            return 0
        limit = self.limit
        if self.circuit is not None:
            if not self.circuit.allows(self.clock()):
                return 0
            if self.circuit.is_open():
                limit = 1  # prueba espaciada: un solo evento
        start = self.cursor
        rows = self.repository.page(start, self.page_size)
        attempts = 0
        for offset, values in enumerate(rows):
            if attempts >= limit or not self.client.configured():
                self.cursor = start + offset
                return attempts
            row = dict(zip(EVENTS_HEADER, values))
            now = self.clock()
            if not retry_due(row, now):
                continue
            try:
                event = json.loads(row.get("payload_json") or "")
                if not isinstance(event, dict) or event.get("event_id") != row.get("event_id") or event.get("source") != "vicky_redes":
                    raise ValueError("Identidad del payload inconsistente")
            except (ValueError, TypeError, AttributeError):
                state = RECHAZADO
                log.error("Payload de bitacora invalido en fila %s", start + offset)
            else:
                needs_link = (event.get("event_type") in OUTBOUND and not event.get("backfill")
                              and not (event.get("message") or {}).get("request_id"))
                # Storage lookup failures propagate to the next cycle; they
                # must never turn a valid event into a terminal rejection.
                event = correlated_event(event, self.repository.correlations() if needs_link else {})
                state = self.client.send(event) if event is not None else PENDIENTE
            attempts += 1
            try:
                previous_attempts = max(0, int(row.get("radar_attempts") or 0))
            except (ValueError, TypeError):
                previous_attempts = 0
            self.repository.mark(start + offset, row.get("event_id"), state, previous_attempts + 1, now)
            if self.circuit is not None:
                if state == RECHAZADO_AUTH:
                    self.circuit.opened(now, getattr(self.client, "last_rejection", None))
                    self.cursor = start + offset + 1
                    return attempts
                if state == ENVIADO:
                    self.circuit.succeeded()
        self.cursor = 2 if len(rows) < self.page_size else start + len(rows)
        return attempts


FAILURE_BACKOFF_BASE = 60
FAILURE_BACKOFF_MAX = 900


class OutboxWorker:
    def __init__(self, factory, client, interval=60, fingerprint=None):
        self.factory, self.client, self.interval = factory, client, interval
        self.fingerprint = fingerprint
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if not self.client.configured():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="RadarOutbox", daemon=True)
            self._thread.start()

    def _new_pump(self):
        repository = self.factory()
        circuit = None
        if self.fingerprint and hasattr(repository, "circuit_get"):
            circuit = AuthCircuit(repository, self.fingerprint)
        return OutboxPump(repository, self.client, circuit=circuit)

    def _run(self):
        pump = None
        failures = 0
        while not self._stop.is_set():
            wait = self.interval
            try:
                if self.client.configured():
                    if pump is None:
                        pump = self._new_pump()
                    pump.run_once()
                failures = 0
            except Exception as exc:
                failures += 1
                # Espera creciente e independiente del intervalo normal: hasta
                # el 17-sep el barrido fallaba cada 60 s durante dias.
                wait = min(FAILURE_BACKOFF_MAX, FAILURE_BACKOFF_BASE * 2 ** (failures - 1))
                log.warning("Barrido Radar pendiente (%s); reintento en %ss", type(exc).__name__, wait)
                pump = None
            self._stop.wait(wait)
