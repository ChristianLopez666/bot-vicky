"""PR-1: circuito persistente para 401/403 de Radar y motivo sanitizado."""
import copy
import unittest
from unittest.mock import Mock

from radar_events import ENVIADO, PENDIENTE, RECHAZADO_AUTH, EventLog, RadarClient, build_event
from radar_outbox import ABIERTO, CERRADO, AuthCircuit, OutboxPump

REQUEST_ID = "b3b82a80-59a0-4cb5-b8dc-138a830098ef"
NOW = 1789084800.0


def event(suffix):
    return build_event("message_sent", lead_id="RS-x", wamid="wamid." + suffix,
                       phone_number_id="876953768824165", request_id=REQUEST_ID)


class Store:
    """Bitacora + pestana del circuito en memoria; sobrevive a 'reinicios'."""

    def __init__(self, events):
        self.rows, self.circuit, self.puts = [], None, 0
        log = EventLog(lambda tab, row: (self.rows.append(row), len(self.rows) + 1)[1])
        for e in events:
            log.record(e)

    def page(self, start, size):
        return copy.deepcopy(self.rows[start - 2:start - 2 + size])

    def correlations(self):
        return {}

    def mark(self, number, event_id, state, attempts, now):
        self.rows[number - 2][14:17] = [state, str(attempts), ""]

    def circuit_get(self):
        return dict(self.circuit) if self.circuit else None

    def circuit_put(self, record):
        self.puts += 1
        self.circuit = dict(record)


class Resp:
    def __init__(self, code, body):
        import json
        self.status_code, self._body = code, body
        self.text = json.dumps(body, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._body


def client(responses):
    it = iter(responses)
    return RadarClient(url="https://radar.test/api/v1/vicky/events", token="t", hmac_secret="h",
                       enabled=True, poster=lambda *a, **k: next(it))


def pump(store, c, fingerprint="huella-A", now=NOW):
    return OutboxPump(store, c, clock=lambda: now,
                      circuit=AuthCircuit(store, fingerprint, probe_seconds=3600))


FORBIDDEN = Resp(403, {"ok": False, "error": "phone_number_id_not_authorized",
                       "detail": "phone_number_id no autorizado para esta fuente"})


def ok(e):
    return Resp(200, {"ok": True, "event_id": e["event_id"]})


class CircuitTests(unittest.TestCase):
    def test_403_abre_el_circuito_marca_la_fila_y_no_sigue_enviando(self):
        s = Store([event("A"), event("B")])
        self.assertEqual(pump(s, client([FORBIDDEN])).run_once(), 1)
        self.assertEqual(s.rows[0][14], RECHAZADO_AUTH)
        self.assertEqual(s.rows[1][14], PENDIENTE)
        self.assertEqual(s.circuit["estado"], ABIERTO)
        self.assertEqual(s.circuit["http"], 403)
        self.assertEqual(s.circuit["error"], "phone_number_id_not_authorized")
        self.assertEqual(s.circuit["detalle"], "phone_number_id no autorizado para esta fuente")

    def test_un_reinicio_no_vuelve_a_golpear_a_radar(self):
        s = Store([event("A"), event("B")])
        pump(s, client([FORBIDDEN])).run_once()
        poster = Mock()
        reinicio = RadarClient(url="https://radar.test/x", token="t", hmac_secret="h", enabled=True, poster=poster)
        self.assertEqual(pump(s, reinicio, now=NOW + 60).run_once(), 0)
        poster.assert_not_called()

    def test_la_prueba_espaciada_manda_un_solo_evento_y_cierra_si_entra(self):
        s = Store([event("A"), event("B"), event("C")])
        pump(s, client([FORBIDDEN])).run_once()
        c = client([ok(event("A")), ok(event("B")), ok(event("C"))])
        self.assertEqual(pump(s, c, now=NOW + 3600).run_once(), 1)
        self.assertEqual(s.rows[0][14], ENVIADO)
        self.assertEqual(s.circuit["estado"], CERRADO)
        # Cerrado: el siguiente ciclo drena el resto normalmente.
        p = pump(s, c, now=NOW + 3700)
        p.cursor = 2
        self.assertEqual(p.run_once(), 2)
        self.assertEqual([r[14] for r in s.rows], [ENVIADO] * 3)

    def test_cambiar_la_configuracion_cierra_el_circuito_sin_esperar(self):
        s = Store([event("A")])
        pump(s, client([FORBIDDEN]), fingerprint="huella-A").run_once()
        c = client([ok(event("A"))])
        self.assertEqual(pump(s, c, fingerprint="huella-B", now=NOW + 60).run_once(), 1)
        self.assertEqual(s.rows[0][14], ENVIADO)

    def test_la_huella_cambia_con_la_config_y_no_contiene_secretos(self):
        a = RadarClient(url="u", token="secreto-1", hmac_secret="h").fingerprint("pid")
        b = RadarClient(url="u", token="secreto-2", hmac_secret="h").fingerprint("pid")
        self.assertNotEqual(a, b)
        self.assertNotIn("secreto", a)
        self.assertEqual(len(a), 16)

    def test_sin_circuito_el_comportamiento_de_siempre(self):
        s = Store([event("A")])
        self.assertEqual(OutboxPump(s, client([ok(event("A"))]), clock=lambda: NOW).run_once(), 1)
        self.assertIsNone(s.circuit)


if __name__ == "__main__":
    unittest.main()
