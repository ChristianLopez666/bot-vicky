"""PR-1: un rechazo de Radar nunca deja cuerpos ni texto libre en el log ni en
la pestana del circuito. Solo allowlist, codigo HTTP y huella no reversible."""
import copy
import hashlib
import json
import logging
import re

import pytest

from radar_events import (DETALLE_OMITIDO, KNOWN_DETAILS, KNOWN_ERRORS, RESPUESTA_NO_ESTRUCTURADA,
                          EventLog, RadarClient, build_event, rejection_summary)
from radar_outbox import CIRCUIT_HEADER, AuthCircuit, OutboxPump

SECRETO_CANAL = "hmac-del-canal"

# Cada uno es algo que no puede salir del proceso.
SENSIBLES = {
    "bearer": "Bearer eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2lnbmF0dXJh",
    "api_key": "api_key=CLAVE-FALSA-DE-PRUEBA-AAAAAAAA",
    "correo": "cliente.prueba@example.com",
    "url_query": "https://radar.example/api?token=abc123&phone=5216871350410",
    "uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
    "telefono": "5216871350410",
}
HTML = "<html><body><h1>403 Forbidden</h1><p>cliente.prueba@example.com 5216871350410</p></body></html>"


class Resp:
    def __init__(self, code, body=None, raw=None):
        self.status_code = code
        self._body = body
        self.content = raw.encode("utf-8") if raw is not None else json.dumps(body).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _cadenas_prohibidas(valor):
    """El valor completo y sus fragmentos que identifican a alguien."""
    fragmentos = {valor}
    fragmentos.update(re.findall(r"[A-Za-z0-9_.@-]{8,}", valor))
    return {f for f in fragmentos if f not in ("Bearer", "api_key")}


def _todo_lo_escrito(caplog, resumen, circuito):
    return "\n".join([caplog.text, json.dumps(resumen), json.dumps(circuito or {})])


def _enviar(resp, caplog):
    c = RadarClient(url="https://radar.test/api/v1/vicky/events", token="t", hmac_secret=SECRETO_CANAL,
                    enabled=True, poster=lambda *a, **k: resp)
    store = _Store()
    with caplog.at_level(logging.DEBUG):
        OutboxPump(store, c, clock=lambda: 1789084800.0,
                   circuit=AuthCircuit(store, "huella", probe_seconds=3600)).run_once()
    return c.last_rejection, store.circuit


class _Store:
    def __init__(self):
        self.rows, self.circuit = [], None
        ev = build_event("message_sent", lead_id="RS-x", wamid="wamid.A", phone_number_id="876953768824165",
                         request_id="b3b82a80-59a0-4cb5-b8dc-138a830098ef")
        EventLog(lambda tab, row: (self.rows.append(row), len(self.rows) + 1)[1]).record(ev)

    def page(self, start, size):
        return copy.deepcopy(self.rows[start - 2:start - 2 + size])

    def correlations(self):
        return {}

    def mark(self, *a):
        pass

    def circuit_get(self):
        return self.circuit

    def circuit_put(self, record):
        self.circuit = dict(record)


@pytest.mark.parametrize("campo", ["error", "detail"])
@pytest.mark.parametrize("nombre", sorted(SENSIBLES))
def test_contenido_sensible_en_error_o_detail_se_omite(nombre, campo, caplog):
    cuerpo = {"ok": False, "error": "invalid_payload", "detail": "Firma invalida"}
    cuerpo[campo] = f"fallo con {SENSIBLES[nombre]}"
    resumen, circuito = _enviar(Resp(403, cuerpo), caplog)

    assert resumen["error" if campo == "error" else "detalle"] == DETALLE_OMITIDO
    escrito = _todo_lo_escrito(caplog, resumen, circuito)
    for prohibido in _cadenas_prohibidas(SENSIBLES[nombre]):
        assert prohibido not in escrito, (nombre, prohibido)


@pytest.mark.parametrize("nombre", sorted(SENSIBLES))
def test_contenido_sensible_en_campos_extra_nunca_sale(nombre, caplog):
    cuerpo = {"ok": False, "error": "phone_number_id_not_authorized", "debug": SENSIBLES[nombre],
              "request": {"headers": {"Authorization": SENSIBLES["bearer"]}}}
    resumen, circuito = _enviar(Resp(403, cuerpo), caplog)
    escrito = _todo_lo_escrito(caplog, resumen, circuito)
    for prohibido in _cadenas_prohibidas(SENSIBLES[nombre]) | _cadenas_prohibidas(SENSIBLES["bearer"]):
        assert prohibido not in escrito


def test_respuesta_html_queda_como_no_estructurada(caplog):
    resumen, circuito = _enviar(Resp(403, raw=HTML), caplog)
    assert resumen["http"] == 403
    assert resumen["error"] == RESPUESTA_NO_ESTRUCTURADA
    assert resumen["detalle"] == RESPUESTA_NO_ESTRUCTURADA
    escrito = _todo_lo_escrito(caplog, resumen, circuito)
    for prohibido in ("<html", "Forbidden", "cliente.prueba@example.com", "5216871350410", "6871350410"):
        assert prohibido not in escrito


def test_texto_plano_tampoco_sale(caplog):
    resumen, circuito = _enviar(Resp(401, raw=SENSIBLES["url_query"]), caplog)
    assert resumen["error"] == RESPUESTA_NO_ESTRUCTURADA
    assert "abc123" not in _todo_lo_escrito(caplog, resumen, circuito)


def test_la_huella_es_hmac_sha256_no_un_sha256_simple():
    raw = '{"error":"x","detail":"5216871350410"}'
    resumen = rejection_summary(Resp(403, raw=raw), SECRETO_CANAL)
    assert re.fullmatch(r"[0-9a-f]{64}", resumen["huella"])
    # Un SHA-256 simple de un cuerpo con telefono se reconstruye por fuerza bruta.
    assert resumen["huella"] != hashlib.sha256(raw.encode()).hexdigest()
    # Radar, con el mismo secreto, si puede recalcularla.
    import hmac
    assert resumen["huella"] == hmac.new(SECRETO_CANAL.encode(), raw.encode(), hashlib.sha256).hexdigest()


def test_codigos_y_mensajes_conocidos_si_se_conservan(caplog):
    for error in KNOWN_ERRORS:
        assert rejection_summary(Resp(400, {"error": error}), "k")["error"] == error
    for detalle in KNOWN_DETAILS:
        assert rejection_summary(Resp(400, {"detail": detalle}), "k")["detalle"] == detalle


def test_la_pestana_del_circuito_solo_guarda_campos_cerrados(caplog):
    _, circuito = _enviar(Resp(403, {"error": "invalid_signature", "detail": SENSIBLES["correo"]}), caplog)
    assert list(circuito) == CIRCUIT_HEADER
    assert circuito["error"] == "invalid_signature"
    assert circuito["detalle"] == DETALLE_OMITIDO
    assert re.fullmatch(r"[0-9a-f]{64}", circuito["huella_respuesta"])
    permitidos = KNOWN_ERRORS | KNOWN_DETAILS | {DETALLE_OMITIDO, RESPUESTA_NO_ESTRUCTURADA}
    assert circuito["error"] in permitidos and circuito["detalle"] in permitidos


def test_el_400_tambien_usa_la_allowlist(caplog):
    c = RadarClient(url="https://radar.test/x", token="t", hmac_secret=SECRETO_CANAL, enabled=True,
                    poster=lambda *a, **k: Resp(400, {"error": "invalid_payload", "detail": SENSIBLES["correo"]}))
    with caplog.at_level(logging.ERROR):
        c.send(build_event("message_sent", lead_id="RS-x", wamid="wamid.B", request_id="r"))
    assert "invalid_payload" in caplog.text
    assert SENSIBLES["correo"] not in caplog.text
    assert DETALLE_OMITIDO in caplog.text
