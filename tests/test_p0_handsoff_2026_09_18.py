"""Pruebas que DEBEN FALLAR hoy (SHA b1978e1) — auditoria hands-off 18-sep, P0-1.

Evidencia: 17-sep 18:54:20-18:56:09 UTC, siete "Radar rechazo la autenticacion
(403); emisor apagado", uno por cada worker que revivia tras un SIGKILL. El log
no dice POR QUE Radar rechazo: el cuerpo de la respuesta se descarta.
"""
import logging

from radar_events import PENDIENTE, RadarClient, build_event
from radar_outbox import OutboxPump
from radar_events import EVENTS_HEADER


class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text

    def json(self):
        import json
        return json.loads(self.text)


def _client(code, text):
    return RadarClient(url="https://radar.test/api/v1/vicky/events", token="t", hmac_secret="h",
                       enabled=True, poster=lambda *a, **k: _Resp(code, text))


def _evento():
    return build_event("message_inbound", lead_id="RS-x", phone_e164="5216871350410",
                       phone_last10="6871350410", phone_number_id="876953768824165",
                       wamid="wamid.IN", direction="inbound")


def test_p0_1_el_403_deja_rastro_del_motivo_que_dio_radar(caplog):
    motivo = '{"ok":false,"error":"forbidden","detail":"phone_number_id no autorizado para esta fuente"}'
    with caplog.at_level(logging.ERROR):
        _client(403, motivo).send(_evento())
    assert "phone_number_id no autorizado" in caplog.text, caplog.text


def test_p0_1_un_403_no_deja_el_evento_como_pendiente_indistinguible():
    """Tras un 403 la fila queda PENDIENTE igual que un timeout: nadie ve en la
    bitacora que Radar la rechazo por identidad."""
    assert _client(403, "{}").send(_evento()) != PENDIENTE


class _Repo:
    def __init__(self):
        self.calls = 0

    def page(self, start, size):
        self.calls += 1
        raise RuntimeError("HttpError simulado de Sheets")


def test_p0_1_el_barrido_no_reconstruye_nada_en_cada_fallo():
    """Barrido Radar pendiente (HttpError) cada 60 s desde el 14-sep; en esa
    ventana el worker murio por memoria cada ~43 min (22 SIGKILL el 16/17-sep).
    Un fallo de lectura debe hacer backoff, no martillar cada minuto."""
    from radar_outbox import OutboxWorker
    fabrica = []
    worker = OutboxWorker(lambda: fabrica.append(1) or _Repo(), _client(200, "{}"), interval=0)
    import threading
    t = threading.Thread(target=worker._run, daemon=True)
    t.start()
    t.join(0.3)
    worker._stop.set()
    t.join(1)
    assert len(fabrica) <= 3, f"reconstruyo el repositorio {len(fabrica)} veces sin backoff"
