"""Vicky Redes atendiendo GastroRadar: recorrido completo y conexiones.

Los mensajes entran por /webhook firmado. Corren las funciones reales de app.py
(estado, bitácora y reporte en Sheets, envío, aviso al asesor con plantilla,
estados de entrega). Solo se simula lo externo: Meta, Google Sheets, Redis y Radar.
"""
import hashlib
import hmac
import itertools
import json

import pytest

import app
import imss_flow
from test_imss_dynamic_flow_route import _decrypt_response, _encrypt_request, keypair  # noqa: F401

USER, OTHER, ADVISOR, PHONE_ID, SECRET = "5215550000001", "5215550000002", "5215550000099", "111", "s3cr3t"
ids = itertools.count(1)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeMeta:
    def __init__(self):
        self.payloads, self.reject_text_to, self.reject_all_to = [], set(), set()
        self.wamids = []                                # en el mismo orden que payloads

    def post(self, url, headers=None, json=None, timeout=None):
        to, kind = json.get("to"), json.get("type")
        if to in self.reject_all_to or (to in self.reject_text_to and kind == "text"):
            return Resp(400, {"error": {"code": 131047}})
        self.payloads.append(json)
        self.wamids.append(f"wamid.OUT{next(ids)}")
        return Resp(200, {"messages": [{"id": self.wamids[-1]}]})


class Run:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeSheets:
    """spreadsheets().values().append/get/update por pestaña."""

    def __init__(self):
        self.tabs = {app.SHEET_TAB: [list(app._HDR)], app.SHEET_TAB_REPORTE: [list(app._HDR_REPORTE)]}

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):
        return Run({"values": [list(r) for r in self.tabs[range.split("!")[0]]]})

    def append(self, spreadsheetId, range, body, **_):
        self.tabs[range.split("!")[0]].append(list(body["values"][0]))
        return Run({})

    def update(self, spreadsheetId, range, body, **_):
        tab, cells = range.split("!")
        self.tabs[tab][int(cells.split(":")[0][1:]) - 1] = list(body["values"][0])
        return Run({})


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def setex(self, k, ttl, v):
        self.data[k] = v

    def expire(self, k, ttl):
        pass

    def delete(self, k):
        self.data.pop(k, None)


class Radar:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)


class World:
    def __init__(self, monkeypatch):
        self.meta, self.sheets, self.radar, self.redis = FakeMeta(), FakeSheets(), Radar(), FakeRedis()
        store = app.StateStore()
        store._redis = self.redis                       # misma ruta de código que con KV_URL
        for name, value in {
            "GASTRORADAR_MENU": True, "META_TOKEN": "token-falso", "WABA_ID": PHONE_ID,
            "APP_SECRET": SECRET, "ADVISOR_NUM": ADVISOR, "ADV_TPL": "plantilla_prueba",
            "_state_store": store, "user_state": app._StateMap(store), "user_data": app._DataMap(store),
            "_svc": self.sheets, "_srdy": True, "_radar": self.radar, "INTERNAL_TOKEN": "interno",
        }.items():
            monkeypatch.setattr(app, name, value)
        monkeypatch.setattr(app.requests, "post", self.meta.post)
        self.boardroom = []
        monkeypatch.setattr(app, "_emit_boardroom_observation", lambda e: self.boardroom.append(e))
        monkeypatch.setattr(app, "_notify_boardroom_lead_qualified", lambda *a, **k: False)
        monkeypatch.setenv("INTERNAL_TOKEN", "interno")
        self.client = app.app.test_client()

    def webhook(self, value, secret=SECRET, phone_id=PHONE_ID):
        raw = json.dumps({"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_id}, **value}}]}]}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post("/webhook", data=raw, headers={"X-Hub-Signature-256": sig,
                                                              "Content-Type": "application/json"})

    def say(self, text, user=USER, kind="text", mid=None, **extra):
        msg = {"from": user, "id": mid or f"wamid.IN{next(ids)}", "type": kind, **extra}
        if kind == "text":
            msg["text"] = {"body": text}
        before = len(self.meta.payloads)
        assert self.webhook({"messages": [msg]}).status_code == 200
        return "\n".join(p["text"]["body"] for p in self.meta.payloads[before:]
                         if p["to"] == user and p["type"] == "text")

    def advisor(self):
        out = []
        for p in self.meta.payloads:
            if p["to"] == ADVISOR:
                out.append(p["text"]["body"] if p["type"] == "text"
                           else p["template"]["components"][0]["parameters"][0]["text"])
        return out

    def report(self, user=USER):
        return [dict(zip(app._HDR_REPORTE, r)) for r in self.sheets.tabs[app.SHEET_TAB_REPORTE][1:]
                if r[0] == user]

    def everything_to_users(self):
        return " ".join(json.dumps(p, ensure_ascii=False) for p in self.meta.payloads if p["to"] != ADVISOR)


@pytest.fixture
def w(monkeypatch):
    return World(monkeypatch)


FINANCIAL = ("Servicios Financieros", "Préstamo", "pensión", "Ley 73", "Seguro de", "VRIM", "CTC",
             "COHIFIS", "crédito")


def assert_no_financial(text):
    assert not [word for word in FINANCIAL if word.lower() in text.lower()], text


def test_recorrido_completo(w):
    assert "asistente virtual de GastroRadar" in w.say("hola")
    assert "$499" in w.say("4")
    assert "hasta 4 anunciantes" in w.say("¿cuánto cuesta?")          # seguimiento del tema
    assert "no tengo una respuesta confirmada" in w.say("¿hacen diseños?").lower()
    assert "ASESOR" in w.say("sí")
    assert "Encontrar comida" in w.say("menu")
    assert "$89.90" in w.say("3")
    assert "Registrarte es gratis" in w.say("quiero registrar mi negocio")
    assert w.advisor() == []
    reply = w.say("asesor")
    assert "Envié tu solicitud" in reply and "nombre" in reply
    assert "Registro de negocio" in w.advisor()[0] and USER in w.advisor()[0]
    assert w.report()[0]["Estado"] == "Solicita asesor"
    assert "envié tus datos" in w.say("Ana Prueba, Tacos Ficticios").lower()
    assert "Ana Prueba" in w.advisor()[1]
    [row] = w.report()
    assert row["Nombre"] == "Ana Prueba, Tacos Ficticios" and row["Producto"] == "GastroRadar: Registro de negocio"
    assert "Encontrar comida" in w.say("menu")
    assert "$39.90" in w.say("3")
    assert_no_financial(w.everything_to_users())
    assert w.boardroom == []


@pytest.mark.parametrize("option,expected", [
    ("1", "Cerca de mí"), ("2", "verifica por teléfono"), ("3", "aún no está disponible"),
    ("4", "Mercado Pago"), ("5", "terminal"), ("6", "reportar un problema"),
])
def test_cada_opcion_y_regreso(w, option, expected):
    w.say("hola")
    reply = w.say(option)
    assert expected.lower() in reply.lower()
    assert_no_financial(reply)
    assert "Encontrar comida" in w.say("menu")


def test_terminal_no_inventa_condiciones(w):
    w.say("hola")
    reply = w.say("5") + w.say("¿cuánto cuesta?")
    assert "$" not in reply and "sin costo" not in reply


def test_asesor_conserva_tema_y_no_se_repite(w):
    w.say("hola"); w.say("5"); w.say("asesor")
    assert "Terminal" in w.advisor()[0]
    w.say("omitir")
    assert "no la repito" in w.say("asesor")
    assert len(w.advisor()) == 1


def test_en_la_pregunta_de_datos_se_puede_seguir_navegando(w):
    w.say("hola"); w.say("asesor")
    assert "$499" in w.say("4")
    w.say("asesor")
    assert "$39.90" in w.say("¿qué planes hay?")
    assert not [m for m in w.advisor() if "datos de quien" in m]


@pytest.mark.parametrize("word", ["gracias", "ok", "omitir", "no gracias"])
def test_cortesias_no_se_envian_como_datos_ni_cancelan(w, word):
    w.say("hola"); w.say("asesor"); w.say(word)
    assert len(w.advisor()) == 1 and w.report()[0]["Estado"] == "Solicita asesor"


@pytest.mark.parametrize("phrase", ["cancelar", "no quiero un asesor", "ya no, gracias", "BAJA",
                                    "no me llamen", "no me interesa", "No, no quiero un asesor"])
def test_negativa_despues_de_pedir_asesor(w, phrase):
    w.say("hola"); w.say("asesor")
    reply = w.say(phrase)
    assert "no puedo deshacerlo" in reply
    assert len(w.advisor()) == 2 and "NO CONTACTAR" in w.advisor()[1]
    assert w.report()[0]["Estado"] == "No contactar"
    w.say(phrase if phrase != "BAJA" else "cancelar")
    assert len([m for m in w.advisor() if "piden un asesor" in m]) == 1


@pytest.mark.parametrize("phrase", ["no quiero un asesor", "cancelar", "sin asesor", "no necesito un asesor"])
def test_negativa_sin_solicitud_no_avisa(w, phrase):
    w.say("hola")
    assert "no pediré" in w.say(phrase)
    assert w.advisor() == []


def test_baja_sin_solicitud_reciente_se_comunica_y_queda_registrada(w):
    reply = w.say("BAJA")
    assert "no contactarte" in reply and "no puedo deshacer" not in reply
    assert "NO CONTACTAR" in w.advisor()[0] and w.report()[0]["Estado"] == "No contactar"


def test_aviso_usa_plantilla_si_el_texto_libre_falla(w):
    w.meta.reject_text_to.add(ADVISOR)
    assert "Envié tu solicitud" in w.say("asesor")
    assert [p["type"] for p in w.meta.payloads if p["to"] == ADVISOR] == ["template"]


def test_si_el_aviso_falla_queda_registrado_y_se_puede_reintentar(w):
    w.meta.reject_all_to.add(ADVISOR)
    assert "No pude enviar" in w.say("asesor")
    assert w.report()[0]["Estado"] == "Solicita asesor - aviso fallido"
    w.meta.reject_all_to.clear()
    assert "Envié tu solicitud" in w.say("asesor")


def test_estado_failed_de_meta_reenvia_por_plantilla(w):
    app._advisor_window_touch()
    w.say("asesor")
    first = [p for p in w.meta.payloads if p["to"] == ADVISOR]
    assert [p["type"] for p in first] == ["text"]
    wamid = next(k for k in w.redis.data if k.startswith("vicky:adv_retry:")).split(":", 2)[2]
    assert w.webhook({"statuses": [{"id": wamid, "status": "failed", "recipient_id": ADVISOR,
                                    "errors": [{"code": 131047}]}]}).status_code == 200
    assert [p["type"] for p in w.meta.payloads if p["to"] == ADVISOR] == ["text", "template"]


# ── Sesiones que ya existían ────────────────────────────────────────────────
OLD_STATES = ["imss_open", "imss_q_pension_calc", "imss_q_nombre_calc", "imss_post_cierre", "auto_inicio",
              "vida_q", "vrim_q", "emp_q", "fp_q", "imss_filtro"]


@pytest.mark.parametrize("old", OLD_STATES)
@pytest.mark.parametrize("answer", ["1", "sí", "8500", "Juan Pérez", "gracias", "menu"])
def test_sesion_financiera_pasa_al_menu_nuevo_sin_borrar_datos(w, old, answer):
    w.redis.data[f"vicky:state:{USER}"] = old
    w.redis.data[f"vicky:data:{USER}"] = json.dumps({"nombre": "Dato previo", "pension": 8500})
    reply = w.say(answer)
    assert "GastroRadar" in reply and "Encontrar comida" in reply
    assert_no_financial(reply)
    assert json.loads(w.redis.data[f"vicky:data:{USER}"]) == {"nombre": "Dato previo", "pension": 8500}
    assert w.redis.data[f"vicky:state:{USER}"] == "gr_menu"
    assert "$39.90" in w.say("3")


def test_numero_sin_menu_nuevo_mostrado_no_se_interpreta(w):
    assert "Encontrar comida" in w.say("2")          # estado vencido: vio el menú viejo
    assert "Registrarte es gratis" in w.say("2")


@pytest.mark.parametrize("item", ["menu_imss", "menu_auto", "lo_que_sea"])
def test_botones_del_menu_viejo(w, item):
    reply = w.say("", kind="interactive", interactive={
        "type": "list_reply", "list_reply": {"id": item, "title": "Préstamo IMSS"}})
    assert "GastroRadar" in reply
    assert_no_financial(reply)


@pytest.mark.parametrize("text", ["préstamo imss", "seguro de auto", "ctc", "crédito empresarial", "vrim"])
def test_pedir_un_producto_anterior_no_abre_su_flujo(w, text):
    w.say("hola")
    assert_no_financial(w.say(text))
    assert w.redis.data[f"vicky:state:{USER}"].startswith("gr_")


def test_anuncio_con_referral_financiero(w):
    reply = w.say("Hello! Can I get more info on this?", referral={"headline": "Préstamo IMSS"})
    assert "GastroRadar" in reply
    assert_no_financial(reply)


def test_archivos_y_reacciones(w):
    assert "solo puedo leer" in w.say("", kind="image", image={"id": "x"})
    assert w.say("", kind="reaction", reaction={"emoji": "ok"}) == ""


# ── Conexiones existentes ───────────────────────────────────────────────────
def test_sheets_radar_y_aislamiento_de_secom(w):
    w.say("hola")
    rows = w.sheets.tabs[app.SHEET_TAB][1:]
    assert [r[4] for r in rows] == ["entrante", "saliente"] and rows[1][6] == "gastroradar"
    assert {"webhook", "requested", "response"} <= set(w.radar.calls)
    def seen():  # "start" corre antes de cualquier peticion; no es un evento
        return len(w.meta.payloads), len([c for c in w.radar.calls if c != "start"])
    before = seen()
    msg = {"from": USER, "id": "wamid.SECOM", "type": "text", "text": {"body": "hola"}}
    assert w.webhook({"messages": [msg]}, phone_id="999-otro-numero").status_code == 200
    assert seen() == before
    assert w.webhook({"messages": [msg]}, secret="mala").status_code == 403


def test_reporte_no_sobrescribe_el_historial_financiero(w):
    old = [USER, "Cliente IMSS", "Culiacán", "Préstamo IMSS", "8500", "100000", "2500", "60",
           "Propuesta enviada", "ayer", "ayer"]
    w.sheets.tabs[app.SHEET_TAB_REPORTE].append(list(old))
    w.say("asesor"); w.say("Ana, Tacos"); w.say("cancelar")
    assert w.sheets.tabs[app.SHEET_TAB_REPORTE][1] == old
    new = w.report()[1:]
    assert len(new) == 1 and new[0]["Producto"].startswith("GastroRadar")
    app._report_upsert_lead(USER, estado="Seguimiento", producto="Préstamo IMSS")
    assert len(w.report()) == 2 and w.report()[1] == new[0]


def test_mensaje_duplicado(w):
    w.say("asesor", mid="wamid.DUP-GR2")
    w.say("asesor", mid="wamid.DUP-GR2")
    assert len(w.advisor()) == 1


def test_formulario_cohifis_sigue_avisando_solo_al_asesor(w):
    r = w.client.post("/ext/lead", json={"nombre": "Prueba", "telefono": "6680000000", "interes": "auto"},
                      headers={"X-Internal-Token": "interno"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert "cohifis.com" in w.advisor()[0]
    assert {p["to"] for p in w.meta.payloads} == {ADVISOR}


@pytest.mark.parametrize("instruction", ["resume_funnel", "existing_client_greeting"])
def test_boardroom_no_puede_reabrir_conversacion_financiera(w, instruction):
    r = w.client.post("/ext/boardroom/instruct", headers={"X-Internal-Token": "interno"},
                      json={"phone": USER, "instruction": instruction, "payload": {"funnel": "imss"}})
    assert r.status_code == 409 and w.meta.payloads == []


@pytest.mark.parametrize("instruction", ["hot_transfer", "escalate_chiwy"])
def test_boardroom_conserva_aviso_al_asesor_sin_escribir_al_usuario(w, instruction):
    r = w.client.post("/ext/boardroom/instruct", headers={"X-Internal-Token": "interno"},
                      json={"phone": USER, "instruction": instruction, "payload": {}})
    assert r.status_code == 200 and {p["to"] for p in w.meta.payloads} == {ADVISOR}


@pytest.mark.parametrize("action,screen", [("ping", ""), ("INIT", ""), ("data_exchange", "IMSS_PROFILE"),
                                           ("data_exchange", "IMSS_PENSION"), ("data_exchange", "IMSS_HANDOFF")])
def test_flow_imss_antiguo_no_envia_ni_modifica(w, monkeypatch, keypair, action, screen):  # noqa: F811
    public_key, private_pem = keypair
    monkeypatch.setattr(app, "IMSS_FLOW_PRIVATE_KEY", private_pem)
    w.redis.data["vicky:imss_flow_token:tok"] = USER
    before = dict(w.redis.data)
    body, aes, iv = _encrypt_request(public_key, {
        "version": "3.0", "action": action, "screen": screen, "flow_token": "tok",
        "data": {"profile": "1", "pension": "9000", "name": "X", "city": "Y"}})
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = w.client.post("/ext/flow/imss", data=raw, headers={"X-Hub-Signature-256": sig,
                                                          "Content-Type": "application/json"})
    assert r.status_code == 200
    out = _decrypt_response(r.get_data(as_text=True), aes, iv)
    if action == "ping":
        assert out == imss_flow.build_health_check_response()
    else:
        assert "ya no se atiende" in json.dumps(out, ensure_ascii=False)
    assert w.meta.payloads == [] and w.redis.data == before


def test_interruptor_apagado_conserva_el_bot_anterior(w, monkeypatch):
    monkeypatch.setattr(app, "GASTRORADAR_MENU", False)
    assert "Servicios Financieros" in w.say("menu")


@pytest.mark.parametrize("phrase", ["quiero hablar con alguien", "necesito atención humana",
                                    "que me atienda alguien", "No, quiero un asesor"])
def test_otras_formas_de_pedir_persona(w, phrase):
    w.say("hola")
    assert "Envié tu solicitud" in w.say(phrase) and len(w.advisor()) == 1


@pytest.mark.parametrize("phrase", ["ya no lo quiero", "ya no es necesario", "siempre no", "cancélalo",
                                    "cancela el asesor", "prefiero que no me contacten"])
def test_otras_formas_de_cancelar(w, phrase):
    w.say("hola"); w.say("asesor")
    w.say(phrase)
    assert w.report()[0]["Estado"] == "No contactar" and "NO CONTACTAR" in w.advisor()[-1]


def test_no_a_secas_no_es_archivo(w):
    w.say("hola"); w.say("asesor")
    assert "solo puedo leer" not in w.say("No.") and len(w.advisor()) == 1


@pytest.mark.parametrize("option,expected", [("4", "Mercado Pago"), ("3", "aún no está disponible"),
                                             ("5", "terminal")])
def test_como_contratar_respeta_el_tema(w, option, expected):
    w.say("hola"); w.say(option)
    assert expected.lower() in w.say("¿cómo lo contrato?").lower()


def test_negacion_ambigua_con_asesor_no_avisa(w):
    w.say("hola")
    assert "escribe ASESOR" in w.say("no sé si hablar con un asesor") and w.advisor() == []


# ── RGR-01: preguntar por un asesor no es pedirlo ───────────────────────────
@pytest.mark.parametrize("question", [
    "¿Necesito un asesor para registrar mi negocio?", "¿Qué hace un asesor?", "¿Cuánto cuesta un asesor?",
    "necesito un asesor para registrar mi negocio?", "¿Puedo hablar con un asesor?",
    "necesito saber si necesito un asesor", "el asesor cobra", "que es un asesor",
    "quiero saber cuánto cuesta un asesor", "hay una persona que atienda",
])
@pytest.mark.parametrize("option", ["menu", "2"])
def test_pregunta_sobre_asesor_no_avisa(w, question, option):
    w.say("hola")
    w.say(option)
    reply = w.say(question)
    assert w.advisor() == [] and w.report() == []
    assert "No he enviado" in reply and "ASESOR" in reply
    assert "Envié tu solicitud" in w.say("asesor")        # la confirmación sí avisa
    assert len(w.advisor()) == 1


@pytest.mark.parametrize("request_text", [
    "ASESOR", "Asesor por favor", "quiero que me contacte un asesor", "quiero un asesor",
    "hola, quiero un asesor", "asesor para la terminal", "pásame con un asesor",
    "quiero hablar con una persona", "necesito que me llamen", "que me contacten",
    "me gustaría hablar con un asesor", "un asesor",
])
def test_peticion_explicita_si_avisa(w, request_text):
    w.say("hola")
    assert "Envié tu solicitud" in w.say(request_text)
    assert len(w.advisor()) == 1


@pytest.mark.parametrize("text", ["quiero dar de baja mi negocio", "cómo dar de baja mi ficha"])
def test_baja_de_ficha_no_es_baja_de_contacto(w, text):
    w.say("hola")
    assert "/ayuda" in w.say(text) and w.advisor() == []


# ── RGR-02: un "failed" tardío no revive una solicitud retirada ─────────────
def advisor_wamids(w, contains):
    out = []
    for payload, wamid in zip(w.meta.payloads, w.meta.wamids):
        if payload["to"] == ADVISOR:
            body = (payload["text"]["body"] if payload["type"] == "text"
                    else payload["template"]["components"][0]["parameters"][0]["text"])
            if contains in body:
                out.append(wamid)
    return out


def failed(w, wamid):
    assert w.webhook({"statuses": [{"id": wamid, "status": "failed", "recipient_id": ADVISOR,
                                    "errors": [{"code": 131047}]}]}).status_code == 200


def count(w, text):
    return len([m for m in w.advisor() if text in m])


@pytest.mark.parametrize("cancel", ["cancelar", "no quiero un asesor", "BAJA"])
def test_failed_tardio_no_reenvia_solicitud_cancelada(w, cancel):
    app._advisor_window_touch()
    w.say("asesor"); w.say("Ana, Tacos"); w.say(cancel)
    first, details = advisor_wamids(w, "piden un asesor")[0], advisor_wamids(w, "datos de quien")[0]
    total = len(w.advisor())
    for wamid in (first, first, details):
        failed(w, wamid)
    app._ADV_STATUS_SET.clear(); app._ADV_STATUS_SEEN.clear()   # Meta reentrega tras un reinicio
    failed(w, first)
    assert len(w.advisor()) == total
    assert w.report()[0]["Estado"] == "No contactar"


def test_failed_del_aviso_no_contactar_si_se_reenvia(w):
    app._advisor_window_touch()
    w.say("asesor"); w.say("cancelar")
    failed(w, advisor_wamids(w, "NO CONTACTAR")[0])
    assert count(w, "NO CONTACTAR") == 2 and w.meta.payloads[-1]["type"] == "template"


def test_nueva_solicitud_tras_cancelar_conserva_su_reintento(w):
    app._advisor_window_touch()
    w.say("asesor"); w.say("cancelar")
    old = advisor_wamids(w, "piden un asesor")[0]
    app._advisor_window_touch()
    w.say("asesor")
    new = advisor_wamids(w, "piden un asesor")[-1]
    assert old != new
    failed(w, old)
    assert count(w, "piden un asesor") == 2
    failed(w, new); failed(w, new)
    assert count(w, "piden un asesor") == 3


def test_solicitud_vigente_de_otro_tema_se_sigue_reintentando(w):
    app._advisor_window_touch()
    w.say("hola"); w.say("4"); w.say("asesor"); w.say("menu"); w.say("5"); w.say("asesor")
    failed(w, advisor_wamids(w, "Publicidad y banners")[0])
    assert count(w, "Publicidad y banners") == 2


def test_reintento_de_otros_avisos_no_cambia(w):
    w.say("cancelar"); w.say("BAJA")
    app._advisor_window_touch()
    w.client.post("/ext/lead", json={"nombre": "Prueba", "telefono": "6680000000", "interes": "auto"},
                  headers={"X-Internal-Token": "interno"})
    failed(w, advisor_wamids(w, "cohifis.com")[0])
    assert count(w, "cohifis.com") == 2


# ── RGR-03: la negativa vale aunque la sesión haya vencido ──────────────────
def age(w, session):
    """Más de 24 h: la sesión sigue presente pero vieja, o Redis ya la borró."""
    if session == "presente":
        key = f"vicky:data:{USER}"
        data = json.loads(w.redis.data[key])
        data["gr_asesor"]["ts"] -= 86401
        w.redis.data[key] = json.dumps(data)
    else:
        w.redis.data.clear()


SESSIONS = ["presente", "vencida"]


@pytest.mark.parametrize("session", SESSIONS)
@pytest.mark.parametrize("phrase", ["No quiero un asesor", "cancelar", "ya no me interesa"])
def test_negativa_despues_de_24h(w, session, phrase):
    w.say("hola"); w.say("4"); w.say("asesor")
    age(w, session)
    reply = w.say(phrase)
    assert count(w, "NO CONTACTAR") == 1
    [row] = w.report()
    assert row["Estado"] == "No contactar" and row["Producto"] == "GastroRadar: Publicidad y banners"
    assert "no puedo deshacerlo" in reply
    w.say(phrase)
    assert count(w, "NO CONTACTAR") == 1                  # no se repite


@pytest.mark.parametrize("session", SESSIONS)
def test_negativa_tardia_con_aviso_fallido_no_se_da_por_resuelta(w, session):
    w.say("asesor")
    age(w, session)
    w.meta.reject_all_to.add(ADVISOR)
    reply = w.say("no quiero un asesor")
    assert "No pude avisar" in reply and "no pediré" not in reply
    assert w.report()[0]["Estado"] == "No contactar - aviso fallido"
    w.meta.reject_all_to.clear()
    w.say("no quiero un asesor")
    assert count(w, "NO CONTACTAR") == 1 and w.report()[0]["Estado"] == "No contactar"


def test_si_sheets_no_se_puede_leer_la_negativa_igual_se_comunica(w, monkeypatch):
    w.say("asesor")
    age(w, "vencida")

    def broken(**_):
        raise TimeoutError()
    monkeypatch.setattr(w.sheets, "get", broken)
    reply = w.say("no quiero un asesor")
    assert count(w, "NO CONTACTAR") == 1 and "no pediré" not in reply


def test_negativa_tardia_no_toca_el_historial_financiero(w):
    old = [USER, "Cliente IMSS", "Culiacán", "Préstamo IMSS", "8500", "", "", "",
           "Solicita asesor", "ayer", "ayer"]
    w.sheets.tabs[app.SHEET_TAB_REPORTE].append(list(old))
    w.say("hola"); w.say("no quiero un asesor")
    assert w.advisor() == [] and w.sheets.tabs[app.SHEET_TAB_REPORTE][1] == old


def test_despues_de_24h_se_puede_pedir_otra_vez(w):
    w.say("asesor")
    age(w, "presente")
    assert "Envié tu solicitud" in w.say("asesor") and count(w, "piden un asesor") == 2


@pytest.mark.parametrize("text", [
    "necesito una persona para mi restaurante", "quiero alguien que cocine", "ocupo una persona de mesero",
    "necesito un asesor o puedo solo", "quiero ser asesor", "me puede llamar un asesor",
])
def test_frases_ambiguas_no_avisan(w, text):
    w.say("hola")
    w.say(text)
    assert w.advisor() == [] and w.report() == []


def test_pregunta_con_solicitud_vigente_no_duplica(w):
    w.say("asesor")
    assert "no he enviado otra" in w.say("¿cuándo me llama el asesor?")
    assert len(w.advisor()) == 1
