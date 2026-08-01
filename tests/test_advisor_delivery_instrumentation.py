"""Entrega verificable de la alerta al asesor (ventana 24h + statuses de Meta).

Contexto del defecto que estas pruebas blindan:

Meta acepta con HTTP 200/201 un mensaje de texto libre dirigido a un numero cuya
ventana de 24h esta cerrada, y lo descarta despues sin error sincrono. El codigo
registraba `✅ Asesor notificado (texto libre)` y devolvia True, asi que un lead
podia completarse sin que la alerta llegara nunca al asesor y sin dejar rastro.

Tres carriles cubiertos aqui:
  1. Instrumentacion — extraer y correlacionar el `wamid` que antes se perdia.
  2. Ventana local — decidir el canal ANTES de enviar, no despues de un error
     que nunca llega.
  3. `value.statuses[]` — procesar los estados asincronos que se descartaban en
     silencio, y reenviar por template cuando Meta confirma un `failed`.

Cero I/O real: `_wa_post`, `_log` y la firma del webhook estan mockeados; no se
envia ningun mensaje de WhatsApp ni se escribe en Sheets.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


ADVISOR = "5216682478005"
PROSPECTO = "5216681620521"
WAMID = "wamid.HBgNNTIxNjY4MTYyMDUyMRUCABEYEjKPRUEBA"

LEAD_MSG = (
    "NUEVO LEAD — PRESTAMO IMSS LEY 73\n\n"
    "Nombre: Juan Prueba\n"
    "Pension: $5,000\n"
    "Propuesta: $50,126 a 60 meses"
)


class FakeResp:
    def __init__(self, status_code=200, text="", body=None, raise_json=False):
        self.status_code = status_code
        self.text = text
        self._body = body
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("respuesta no es JSON")
        if self._body is None:
            raise ValueError("sin cuerpo")
        return self._body


def resp_with_wamid(wamid=WAMID, status_code=200):
    return FakeResp(status_code, body={
        "messaging_product": "whatsapp",
        "contacts": [{"input": ADVISOR, "wa_id": ADVISOR}],
        "messages": [{"id": wamid}],
    })


def resp_without_wamid(status_code=200):
    return FakeResp(status_code, body={"messaging_product": "whatsapp"})


@pytest.fixture(autouse=True)
def aislar_estado(monkeypatch):
    """Aisla el almacen auxiliar y el dedup de estados entre pruebas."""
    vicky_app._state_store._aux_mem.clear()
    vicky_app._ADV_STATUS_SEEN.clear()
    vicky_app._ADV_STATUS_SET.clear()
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "ADVISOR_NUM", ADVISOR)
    monkeypatch.setattr(vicky_app, "ADV_TPL", "alerta_lead_asesor")
    monkeypatch.setattr(vicky_app, "ADV_TPL_LANG", "es_MX")
    yield
    vicky_app._state_store._aux_mem.clear()
    vicky_app._ADV_STATUS_SEEN.clear()
    vicky_app._ADV_STATUS_SET.clear()


def cola_wa(monkeypatch, respuestas):
    """Mockea _wa_post con una cola de respuestas; devuelve los payloads enviados."""
    calls = []

    def fake_wa_post(payload):
        calls.append(payload)
        return respuestas[min(len(calls) - 1, len(respuestas) - 1)]

    monkeypatch.setattr(vicky_app, "_wa_post", fake_wa_post)
    return calls


# ── 1. Respuesta sincrona: extraccion del wamid ───────────────────────────────

def test_200_con_wamid_lo_extrae_y_correlaciona(monkeypatch):
    cola_wa(monkeypatch, [resp_with_wamid()])
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    tracked = vicky_app._advisor_wamid_lookup(WAMID)
    assert tracked is not None
    assert tracked["level"] == "texto_libre"


def test_200_sin_wamid_sigue_devolviendo_true(monkeypatch):
    """Contrato critico: devolver False aqui rompe /ext/lead con HTTP 502."""
    cola_wa(monkeypatch, [resp_without_wamid()])
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert vicky_app._advisor_wamid_lookup(WAMID) is None


def test_json_invalido_no_rompe_el_envio(monkeypatch):
    cola_wa(monkeypatch, [FakeResp(200, raise_json=True)])
    assert vicky_app.notify_advisor(LEAD_MSG) is True


def test_respuesta_vacia_no_rompe_el_envio(monkeypatch):
    cola_wa(monkeypatch, [FakeResp(201, body=None)])
    assert vicky_app.notify_advisor(LEAD_MSG) is True


def test_400_con_error_json_escala_a_template(monkeypatch):
    calls = cola_wa(monkeypatch, [
        FakeResp(400, text='{"error":{"code":131047}}'),
        resp_with_wamid(),
    ])
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert len(calls) == 2
    assert calls[1]["type"] == "template"


def test_500_escala_a_template(monkeypatch):
    calls = cola_wa(monkeypatch, [FakeResp(500), resp_with_wamid()])
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert len(calls) == 2


def test_timeout_devuelve_false(monkeypatch):
    def revienta(payload):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(vicky_app, "_wa_post", revienta)
    assert vicky_app.notify_advisor(LEAD_MSG) is False


def test_template_usa_limite_con_holgura(monkeypatch):
    calls = cola_wa(monkeypatch, [FakeResp(400), resp_with_wamid()])
    vicky_app.notify_advisor("x" * 5000)
    param = calls[1]["template"]["components"][0]["parameters"][0]["text"]
    assert len(param) == vicky_app._TPL_PARAM_LIMIT
    assert vicky_app._TPL_PARAM_LIMIT < 1024


# ── 2. Ventana local del asesor ───────────────────────────────────────────────

def test_ventana_desconocida_conserva_texto_libre_primero(monkeypatch):
    """Sin registro previo se mantiene el comportamiento historico."""
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    assert vicky_app._advisor_window_state() == "unknown"
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert calls[0]["type"] == "text"


def test_ventana_abierta_usa_texto_libre(monkeypatch):
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app._advisor_window_touch()
    assert vicky_app._advisor_window_state() == "open"
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert len(calls) == 1
    assert calls[0]["type"] == "text"
    assert calls[0]["text"]["body"] == LEAD_MSG


def test_ventana_cerrada_va_directo_a_template(monkeypatch):
    """El nucleo del arreglo: no se gasta el intento que Meta descartaria."""
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app._advisor_window_expire()
    assert vicky_app._advisor_window_state() == "closed"
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert len(calls) == 1
    assert calls[0]["type"] == "template"


def test_ventana_cerrada_sin_template_conserva_intento_texto_libre(monkeypatch):
    """Sin template configurado no hay nivel 2: se intenta lo unico disponible."""
    monkeypatch.setattr(vicky_app, "ADV_TPL", "")
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app._advisor_window_expire()
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert calls[0]["type"] == "text"


def test_ventana_caduca_pasadas_24h(monkeypatch):
    vicky_app._advisor_window_touch()
    assert vicky_app._advisor_window_state() == "open"
    ahora = vicky_app.time.time()
    monkeypatch.setattr(vicky_app.time, "time", lambda: ahora + 24 * 60 * 60 + 1)
    assert vicky_app._advisor_window_state() == "closed"


def test_registro_corrupto_se_trata_como_desconocido():
    vicky_app._state_store.aux_set(vicky_app._ADV_WINDOW_KEY, "no-es-un-numero", 60)
    assert vicky_app._advisor_window_state() == "unknown"


# ── 3. handle(): el asesor abre su propia ventana ─────────────────────────────

def test_mensaje_del_asesor_abre_la_ventana(monkeypatch):
    monkeypatch.setattr(vicky_app, "send_msg", lambda *a, **k: True)
    monkeypatch.setattr(vicky_app, "_handle_boardroom_authority", lambda *a, **k: None,
                        raising=False)
    assert vicky_app._advisor_window_state() == "unknown"
    vicky_app.handle({"from": ADVISOR, "id": "mid.asesor.1", "type": "text",
                      "text": {"body": "ok"}})
    assert vicky_app._advisor_window_state() == "open"


def test_mensaje_de_prospecto_no_abre_la_ventana_del_asesor(monkeypatch):
    monkeypatch.setattr(vicky_app, "send_msg", lambda *a, **k: True)
    monkeypatch.setattr(vicky_app, "_handle_boardroom_authority", lambda *a, **k: None,
                        raising=False)
    vicky_app.handle({"from": PROSPECTO, "id": "mid.prospecto.1", "type": "text",
                      "text": {"body": "hola"}})
    assert vicky_app._advisor_window_state() == "unknown"


def test_reconoce_al_asesor_con_formato_distinto():
    assert vicky_app._is_advisor_phone("+52 1 668 247 8005") is True
    assert vicky_app._is_advisor_phone(PROSPECTO) is False


# ── 4. value.statuses[] ───────────────────────────────────────────────────────

def _status(estado, wamid=WAMID, errors=None):
    st = {"id": wamid, "status": estado, "recipient_id": ADVISOR}
    if errors:
        st["errors"] = errors
    return st


@pytest.mark.parametrize("estado", ["sent", "delivered", "read"])
def test_estados_de_exito_se_registran_sin_efectos(monkeypatch, estado, caplog):
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    with caplog.at_level("INFO"):
        vicky_app._handle_statuses([_status(estado)])
    assert f"estado={estado}" in caplog.text
    assert calls == []  # ningun envio provocado por un estado de exito


def test_failed_de_alerta_correlacionada_reenvia_por_template(monkeypatch):
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app.notify_advisor(LEAD_MSG)
    assert len(calls) == 1 and calls[0]["type"] == "text"

    vicky_app._handle_statuses([_status("failed", errors=[{
        "code": 131047,
        "title": "Re-engagement message",
        "message": "Message failed to send",
        "error_data": {"details": "More than 24 hours since the last reply"},
    }])])

    assert len(calls) == 2
    assert calls[1]["type"] == "template"
    param = calls[1]["template"]["components"][0]["parameters"][0]["text"]
    assert "Juan Prueba" in param
    # Meta manda sobre la contabilidad local.
    assert vicky_app._advisor_window_state() == "closed"


def test_failed_no_correlacionado_no_reenvia(monkeypatch):
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app._handle_statuses([_status("failed", wamid="wamid.DESCONOCIDO")])
    assert calls == []


def test_failed_de_un_template_no_vuelve_a_reenviar(monkeypatch):
    calls = cola_wa(monkeypatch, [FakeResp(400), resp_with_wamid()])
    vicky_app.notify_advisor(LEAD_MSG)
    assert len(calls) == 2
    vicky_app._handle_statuses([_status("failed")])
    assert len(calls) == 2  # no hay nivel superior al template


def test_failed_duplicado_reenvia_una_sola_vez(monkeypatch):
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app.notify_advisor(LEAD_MSG)
    vicky_app._handle_statuses([_status("failed")])
    vicky_app._handle_statuses([_status("failed")])
    assert len(calls) == 2


def test_errores_de_status_se_registran_saneados(caplog):
    with caplog.at_level("INFO"):
        vicky_app._handle_statuses([_status("failed", errors=[{
            "code": 131026,
            "title": "Message undeliverable",
            "message": "detalle",
            "error_data": {"details": "Receiver is incapable"},
        }])])
    assert "code=131026" in caplog.text
    assert "Receiver is incapable" in caplog.text


def test_statuses_malformados_no_revientan():
    vicky_app._handle_statuses([None, "texto", {}, {"id": "x"}, {"status": "sent"}])
    vicky_app._handle_statuses(None)
    vicky_app._handle_statuses([])


def test_no_expone_el_numero_completo_en_logs(caplog):
    with caplog.at_level("INFO"):
        vicky_app._handle_statuses([_status("delivered")])
    assert ADVISOR not in caplog.text
    assert "8005" in caplog.text


# ── 5. Webhook: statuses solo, mixto y continuidad de messages ────────────────

@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setattr(vicky_app, "_verify_sig", lambda raw, hdr: True)
    vicky_app.app.config["TESTING"] = True
    return vicky_app.app.test_client()


def _payload(value):
    return {"entry": [{"changes": [{"value": value}]}]}


def test_webhook_solo_con_statuses_los_procesa(cliente, monkeypatch):
    vistos = []
    monkeypatch.setattr(vicky_app, "_handle_statuses", lambda s: vistos.append(list(s)))
    monkeypatch.setattr(vicky_app, "handle", lambda m: pytest.fail("no debe haber messages"))
    r = cliente.post("/webhook", json=_payload({"statuses": [_status("delivered")]}))
    assert r.status_code == 200
    assert len(vistos) == 1 and vistos[0][0]["status"] == "delivered"


def test_webhook_mixto_procesa_ambos(cliente, monkeypatch):
    mensajes, estados = [], []
    monkeypatch.setattr(vicky_app, "handle", lambda m: mensajes.append(m))
    monkeypatch.setattr(vicky_app, "_handle_statuses", lambda s: estados.append(list(s)))
    r = cliente.post("/webhook", json=_payload({
        "messages": [{"from": PROSPECTO, "id": "mid.1", "type": "text",
                      "text": {"body": "hola"}}],
        "statuses": [_status("sent")],
    }))
    assert r.status_code == 200
    assert len(mensajes) == 1
    assert len(estados) == 1 and len(estados[0]) == 1


def test_webhook_solo_con_messages_sigue_funcionando(cliente, monkeypatch):
    mensajes = []
    monkeypatch.setattr(vicky_app, "handle", lambda m: mensajes.append(m))
    r = cliente.post("/webhook", json=_payload({
        "messages": [{"from": PROSPECTO, "id": "mid.2", "type": "text",
                      "text": {"body": "menu"}}]
    }))
    assert r.status_code == 200
    assert len(mensajes) == 1


def test_webhook_sin_firma_valida_sigue_bloqueado(monkeypatch):
    monkeypatch.setattr(vicky_app, "_verify_sig", lambda raw, hdr: False)
    vicky_app.app.config["TESTING"] = True
    c = vicky_app.app.test_client()
    r = c.post("/webhook", json=_payload({"statuses": [_status("sent")]}))
    assert r.status_code == 403


# ── 6. Degradacion: Redis/Valkey caido ────────────────────────────────────────

class RedisMuerto:
    def get(self, *a, **k):
        raise ConnectionError("Redis caido")

    def setex(self, *a, **k):
        raise ConnectionError("Redis caido")


def test_redis_caido_no_impide_la_notificacion(monkeypatch):
    monkeypatch.setattr(vicky_app._state_store, "_redis", RedisMuerto())
    calls = cola_wa(monkeypatch, [resp_with_wamid()])
    assert vicky_app.notify_advisor(LEAD_MSG) is True
    assert calls[0]["type"] == "text"


def test_redis_caido_deja_la_ventana_en_desconocida(monkeypatch):
    monkeypatch.setattr(vicky_app._state_store, "_redis", RedisMuerto())
    vicky_app._advisor_window_touch()
    assert vicky_app._advisor_window_state() == "unknown"


def test_aux_store_nunca_propaga_excepciones(monkeypatch):
    monkeypatch.setattr(vicky_app._state_store, "_redis", RedisMuerto())
    assert vicky_app._state_store.aux_set("k", "v", 60) is False
    assert vicky_app._state_store.aux_get("k") is None


# ── 7. Correlacion: TTL y contenido ───────────────────────────────────────────

def test_la_correlacion_no_guarda_datos_personales(monkeypatch):
    cola_wa(monkeypatch, [resp_with_wamid()])
    vicky_app.notify_advisor(LEAD_MSG)
    crudo = vicky_app._state_store.aux_get(f"adv_wamid:{WAMID}")
    assert "Juan Prueba" not in crudo
    assert ADVISOR not in crudo
    assert set(json.loads(crudo)) == {"ts", "level"}


def test_el_cuerpo_de_reintento_vive_menos_que_la_correlacion():
    assert vicky_app._ADV_RETRY_TTL < vicky_app._ADV_WAMID_TTL
    assert vicky_app._ADV_WAMID_TTL >= 24 * 60 * 60
