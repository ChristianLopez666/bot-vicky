"""Menú visual, respuestas por id, mensajes repetidos y memoria del cliente de Sheets.

Entran por /webhook firmado, con los mismos simuladores que el resto de la conversación.
Un payload correcto no equivale a ver el mensaje en un WhatsApp real.
"""
import json

import pytest

import app
import gastroradar_redes as gr
import sheets_client
from test_gastroradar_menu import (ADVISOR, USER, World, Resp, body_of, restart_process,  # noqa: F401
                                   w)

BASE = "https://bot.example.test"


@pytest.fixture(autouse=True)
def public_url(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)


def sent(w, user=USER):
    return [p for p in w.meta.payloads if p["to"] == user]


def last(w):
    return sent(w)[-1]


def controls(payload):
    """[(id, título)] de los botones o filas que la persona puede tocar."""
    if payload["type"] != "interactive":
        return []
    action = payload["interactive"]["action"]
    if payload["interactive"]["type"] == "list":
        return [(r["id"], r["title"]) for s in action["sections"] for r in s["rows"]]
    return [(b["reply"]["id"], b["reply"]["title"]) for b in action["buttons"]]


def tap(w, reply_id, mid=None, user=USER):
    """Toca un control que realmente se haya enviado, como lo manda Meta."""
    titles = {i: t for p in sent(w, user) for i, t in controls(p)}
    kind = "list_reply" if any(p["type"] == "interactive" and p["interactive"]["type"] == "list"
                               and reply_id in dict(controls(p)) for p in sent(w, user)) else "button_reply"
    return w.say("", user=user, kind="interactive", mid=mid,
                 interactive={"type": kind, kind: {"id": reply_id, "title": titles[reply_id]}})


def ids_of(payload):
    return [i for i, _ in controls(payload)]


# ── Bienvenida y navegación ──────────────────────────────────────────────────
def test_bienvenida_con_imagen_y_tres_botones_reales(w):
    w.say("hola")
    message = last(w)
    assert message["interactive"]["type"] == "button"
    assert message["interactive"]["header"] == {
        "type": "image", "image": {"link": BASE + "/static/gastroradar-bienvenida.jpg"}}
    assert controls(message) == [("gr_comida", "Busco comida"), ("gr_negocio", "Tengo un negocio"),
                                 ("gr_ayuda", "Necesito ayuda")]
    assert len(sent(w)) == 1


def test_la_imagen_existe_se_sirve_y_cumple_formato(w):
    response = w.client.get("/static/gastroradar-bienvenida.jpg")
    assert response.status_code == 200 and response.mimetype == "image/jpeg"
    assert response.data[:3] == b"\xff\xd8\xff" and len(response.data) < 5 * 1024 * 1024


def test_sin_url_publica_la_bienvenida_va_sin_imagen(w, monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL")
    w.say("hola")
    assert "header" not in last(w)["interactive"] and len(controls(last(w))) == 3


def test_tengo_un_negocio_abre_lista_con_sus_opciones(w):
    w.say("hola")
    tap(w, "gr_negocio")
    message = last(w)
    assert message["interactive"]["type"] == "list" and "header" not in message["interactive"]
    assert message["interactive"]["action"]["button"] == "Ver opciones"
    assert controls(message) == [
        ("gr_registro", "Registrar mi negocio"), ("gr_planes", "Planes"),
        ("gr_banners", "Publicidad y banners"), ("gr_beneficio", "Un año sin costo"),
        ("gr_inicio", "Volver al menú")]


def test_recorrido_de_la_prueba_real_solo_con_toques(w):
    w.say("hola")
    tap(w, "gr_negocio")
    assert "$499" in tap(w, "gr_banners")
    assert ids_of(last(w)) == ["gr_banners_detalle", "gr_asesor", "gr_inicio"]
    assert "Envié el aviso al asesor" in tap(w, "gr_asesor")
    assert ids_of(last(w)) == ["gr_inicio", "gr_cancelar"]
    assert "Publicidad y banners" in w.advisor()[0]
    assert "Soy Vicky" in tap(w, "gr_inicio")                  # el MENU que quedó sin respuesta
    tap(w, "gr_negocio")
    assert "Registra y administra" in tap(w, "gr_registro")
    assert len(w.advisor()) == 1 and w.report()[0]["Estado"] == "Solicita asesor"


@pytest.mark.parametrize("word", ["MENU", "MENÚ", "inicio", "Volver", "volver al menú"])
@pytest.mark.parametrize("pending", [False, True])
def test_volver_funciona_tambien_durante_los_datos_sin_cancelar(w, word, pending):
    w.say("hola")
    if pending:
        w.say("asesor")
    assert "Soy Vicky" in w.say(word)
    assert len(w.advisor()) == int(pending)
    if pending:
        assert w.report()[0]["Estado"] == "Solicita asesor"


@pytest.mark.parametrize("button,expected,topic", [
    ("gr_comida", "Cerca de mí", "gr_comida"), ("gr_ayuda", "/ayuda", "gr_soporte")])
def test_botones_de_bienvenida(w, button, expected, topic):
    w.say("hola")
    assert expected in tap(w, button)
    assert app.user_state.get(USER) == topic and "gr_asesor" in ids_of(last(w))


@pytest.mark.parametrize("button,expected", [
    ("gr_registro", "4. Verifica que lo representas"), ("gr_planes", "Presencia: $89.90"),
    ("gr_banners", "$499 MXN"), ("gr_beneficio", "12 meses del Plan Presencia")])
def test_cada_opcion_del_negocio(w, button, expected):
    w.say("hola")
    tap(w, "gr_negocio")
    assert expected in tap(w, button).replace("*", "")
    assert {"gr_asesor", "gr_inicio"} <= set(ids_of(last(w)))


def test_condiciones_del_banner_en_detalles(w):
    w.say("hola"); tap(w, "gr_negocio")
    short = tap(w, "gr_banners")
    details = tap(w, "gr_banners_detalle")
    assert "por posición y ubicación" in short
    for fact in ("$499", "superior o lateral", "hasta 4", "aparte de los planes", "no publica nada",
                 "Mercado Pago", "después de revisar", "/planes"):
        assert fact in details


def test_registro_en_cuatro_pasos_con_enlace_visible(w):
    w.say("hola")
    text = w.say("quiero registrar mi negocio")
    for step in ("*1. Busca tu negocio*", "*2. Crea tu cuenta*", "*3. Solicita administrarlo*",
                 "*4. Verifica que lo representas*", "https://tugastroradar.com/planes",
                 "Antes de darte acceso, verificamos por teléfono"):
        assert step in text


def test_boton_cancelar_retira_la_solicitud(w):
    w.say("hola"); tap(w, "gr_comida"); tap(w, "gr_asesor")
    tap(w, "gr_cancelar")
    assert "NO CONTACTAR" in w.advisor()[-1] and w.report()[0]["Estado"] == "No contactar"


def test_el_id_decide_y_no_el_titulo(w):
    w.say("hola")
    reply = w.say("", kind="interactive",
                  interactive={"type": "button_reply", "button_reply": {"id": "gr_planes", "title": "asesor"}})
    assert "Planes" in reply and w.advisor() == []


def test_boton_de_un_menu_antiguo_no_rompe(w):
    reply = w.say("", kind="interactive",
                  interactive={"type": "list_reply", "list_reply": {"id": "menu_imss", "title": "Préstamo"}})
    assert reply and "IMSS" not in reply and w.advisor() == []


def all_views(w):
    for text in ("hola", "1", "2", "3", "4", "detalles", "5", "6", "asesor", "Ana, Tacos", "asesor",
                 "cancelar", "cancelar", "tengo un negocio", "¿hacen diseños?", "sí", "gracias",
                 "¿qué hace un asesor?"):
        w.say(text)
    return [p for p in sent(w) if p["type"] == "interactive"]


def test_limites_de_whatsapp_en_todas_las_respuestas(w):
    views = all_views(w)
    assert len(views) >= 15 and all(p["type"] == "interactive" for p in sent(w))
    for payload in views:
        assert len(payload["interactive"]["body"]["text"]) <= 1024
        titles = [t for _, t in controls(payload)]
        is_list = payload["interactive"]["type"] == "list"
        assert 1 <= len(titles) <= (10 if is_list else 3)
        assert all(len(t) <= (24 if is_list else 20) for t in titles)
        assert "No puedo prometer" not in payload["interactive"]["body"]["text"]


# ── Si WhatsApp no acepta el formato ────────────────────────────────────────
def reject(w, monkeypatch, condition):
    real, rejected = w.meta.post, []

    def post(url, headers=None, json=None, timeout=None):
        if json.get("to") == USER and condition(json):
            rejected.append(json)
            return Resp(400, {"error": {"code": 131009}})
        return real(url, headers=headers, json=json, timeout=timeout)
    monkeypatch.setattr(app.requests, "post", post)
    return rejected


def test_imagen_rechazada_se_envia_sin_imagen(w, monkeypatch):
    rejected = reject(w, monkeypatch, lambda p: "header" in p.get("interactive", {}))
    w.say("hola")
    assert len(rejected) == 1 and len(sent(w)) == 1
    assert "header" not in last(w)["interactive"] and len(controls(last(w))) == 3


def test_formato_interactivo_rechazado_cae_a_texto_con_alternativas(w, monkeypatch):
    reject(w, monkeypatch, lambda p: p["type"] == "interactive")
    w.say("hola")
    assert last(w)["type"] == "text" and "1. Encontrar comida" in body_of(last(w))
    assert "Cerca de mí" in w.say("1")


def test_error_de_red_no_reenvia_para_no_duplicar(w, monkeypatch):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        raise TimeoutError()
    monkeypatch.setattr(app.requests, "post", post)
    w.say("hola")
    assert len(calls) == 1


def test_imagen_que_falla_despues_se_reenvia_una_sola_vez_sin_imagen(w):
    w.say("hola")
    wamid = w.meta.wamids[-1]
    status = {"statuses": [{"id": wamid, "status": "failed", "recipient_id": USER,
                            "errors": [{"code": 131053}]}]}
    w.webhook(status)
    restart_process()
    w.webhook(status)
    assert len(sent(w)) == 2 and "header" not in last(w)["interactive"]


# ── Mensajes repetidos ──────────────────────────────────────────────────────
def test_reentrega_del_mismo_mensaje_tras_reinicio_no_repite(w, caplog):
    caplog.set_level("INFO", logger=app.log.name)
    w.say("hola", mid="wamid.A")
    app._seen_ids.clear(); app._seen_dq.clear(); restart_process()
    assert w.say("hola", mid="wamid.A") == ""
    assert len(sent(w)) == 1 and "motivo=ya_atendido" in caplog.text


def test_reentrega_tras_reinicio_no_repite_aviso_al_asesor(w):
    w.say("hola"); w.say("asesor", mid="wamid.B")
    app._seen_ids.clear(); app._seen_dq.clear(); restart_process()
    w.say("asesor", mid="wamid.B")
    assert len(w.advisor()) == 1


def test_proceso_muerto_a_medias_se_vuelve_a_atender(w):
    w.redis.data["vicky:gr_msg:wamid.C"] = "en_proceso:1-proceso-muerto"
    assert "Soy Vicky" in w.say("hola", mid="wamid.C")


def test_el_mismo_proceso_no_atiende_dos_veces_a_la_vez(w):
    w.redis.data["vicky:gr_msg:wamid.D"] = "en_proceso:" + gr._process_id()
    assert w.say("hola", mid="wamid.D") == ""


def test_proceso_nuevo_tiene_otro_identificador(monkeypatch):
    first = gr._process_id()
    monkeypatch.setattr(gr.os, "getpid", lambda: 999999)
    assert gr._process_id() != first


def test_muerte_al_escribir_en_sheets_no_duplica_el_aviso(w, monkeypatch):
    """Lo que puede pasar con un reinicio justo después de avisar."""
    w.say("hola")

    def die(*_a, **_k):
        raise SystemExit("proceso terminado")
    with monkeypatch.context() as m:
        m.setattr(app, "_report_upsert_lead", die)
        with pytest.raises(SystemExit):
            app._handle_dispatch({"from": USER, "id": "wamid.E", "type": "text", "text": {"body": "asesor"}})
    assert len(w.advisor()) == 1
    app._seen_ids.clear(); app._seen_dq.clear()
    gr._process = (None, "")                       # el proceso nuevo tiene otro identificador
    w.say("asesor", mid="wamid.E")                 # Meta reentrega
    assert len(w.advisor()) == 1 and "ya fue enviada" in body_of(last(w))


def test_si_el_almacen_falla_se_atiende_igual(w, monkeypatch):
    def down(*_a, **_k):
        raise TimeoutError()
    monkeypatch.setattr(w.redis, "set", down)
    assert "Soy Vicky" in w.say("hola")


@pytest.mark.parametrize("repeat", ["texto", "boton"])
def test_mismo_toque_seguido_no_contesta_dos_veces(w, monkeypatch, repeat):
    monkeypatch.setattr(gr, "ECHO_SECONDS", 8)
    w.say("hola")
    if repeat == "texto":
        w.say("2"); before = len(sent(w)); assert w.say("2") == ""
    else:
        tap(w, "gr_negocio"); before = len(sent(w)); assert tap(w, "gr_negocio") == ""
    assert len(sent(w)) == before


def test_pasada_la_ventana_o_con_otra_entrada_si_contesta(w, monkeypatch):
    monkeypatch.setattr(gr, "ECHO_SECONDS", 8)
    w.say("hola"); w.say("2")
    assert w.say("3") and w.say("2")                       # alternar no se bloquea
    key = f"vicky:gr_eco:{USER}"
    stamp, signature = w.redis.data[key].split("|", 1)
    w.redis.data[key] = f"{float(stamp) - 9}|{signature}"
    assert w.say("2")


def test_si_la_respuesta_no_salio_se_puede_insistir(w, monkeypatch):
    monkeypatch.setattr(gr, "ECHO_SECONDS", 8)
    with monkeypatch.context() as m:
        reject(w, m, lambda p: True)
        w.say("hola")
    assert "Soy Vicky" in w.say("hola")


def test_tras_aviso_fallido_se_puede_reintentar_de_inmediato(w, monkeypatch):
    monkeypatch.setattr(gr, "ECHO_SECONDS", 8)
    w.say("hola")
    w.meta.reject_all_to.add(ADVISOR)
    assert "No pude enviar" in w.say("asesor")
    w.meta.reject_all_to.clear()
    assert "Envié el aviso" in w.say("asesor")


def test_otra_persona_no_se_ve_afectada(w, monkeypatch):
    monkeypatch.setattr(gr, "ECHO_SECONDS", 8)
    w.say("hola")
    assert "Soy Vicky" in w.say("hola", user="5215550000002")


def test_aux_claim_en_memoria_y_con_fallo(monkeypatch):
    store = app.StateStore()
    store._redis = None
    assert store.aux_claim("k", "1", 60) is True and store.aux_claim("k", "2", 60) is False
    assert store.aux_get("k") == "1"

    class Broken:
        def set(self, *_a, **_k):
            raise TimeoutError()
    store._redis = Broken()
    assert store.aux_claim("k", "1", 60) is None


# ── El destino de los avisos lo decide la configuración ─────────────────────
def test_avisos_van_al_numero_configurado_y_el_codigo_usa_8005_por_omision(w):
    w.say("hola"); w.say("asesor")
    assert {p["to"] for p in w.meta.payloads} == {USER, ADVISOR}
    source = open(app.__file__, encoding="utf-8").read()
    assert 'os.getenv("ADVISOR_NUMBER", "5216682478005")' in source


# ── Memoria: el recurso de Sheets se crea una sola vez ──────────────────────
def test_spreadsheets_se_crea_una_sola_vez():
    created = []

    class Service:
        def spreadsheets(self):
            created.append(object())
            return created[-1]
    service = sheets_client.reuse_spreadsheets(Service())
    assert service.spreadsheets() is service.spreadsheets() is created[0] and len(created) == 1


def test_con_la_libreria_real_values_sigue_funcionando_y_no_crece():
    import httplib2
    from googleapiclient.discovery import build

    class Http:
        redirect_codes = frozenset()

        def request(self, uri, method="GET", body=None, headers=None, **_):
            return httplib2.Response({"status": "200"}), json.dumps({"values": [["ok"]]}).encode()
    service = sheets_client.reuse_spreadsheets(build("sheets", "v4", http=Http(), cache_discovery=False))
    first = service.spreadsheets()
    for _ in range(5):
        got = service.spreadsheets().values().get(spreadsheetId="x", range="A:K").execute()
    assert got == {"values": [["ok"]]} and service.spreadsheets() is first


def test_app_y_radar_usan_el_cliente_con_memoria_acotada():
    for module in ("app.py", "radar_bridge.py"):
        source = open(app.__file__.replace("app.py", module), encoding="utf-8").read()
        builds = source.count('build("sheets", "v4"')
        assert builds == 1 and "reuse_spreadsheets(" in source


# ── Seis opciones y beneficio con TPV Inbursa (26-nov) ──────────────────────
SIX = [("1", "gr_comida", "Encontrar comida", "Encuentra dónde comer"),
       ("2", "gr_registro", "Registro de negocio", "Registra y administra tu negocio"),
       ("3", "gr_planes", "Planes", "Esencial: gratis"),
       ("4", "gr_banners", "Publicidad y banners", "Anuncia tu negocio"),
       ("5", "gr_beneficio", "Un año sin costo con TPV Inbursa", "Un año de publicidad sin costo"),
       ("6", "gr_ayuda", "Soporte", "Ayuda y contacto")]


def reach(w, button):
    """Llega a una opción solo con toques, como en el teléfono."""
    w.say("hola")
    if button not in ("gr_comida", "gr_ayuda"):
        tap(w, "gr_negocio")
    return tap(w, button)


@pytest.mark.parametrize("number,button,label,title", SIX)
def test_seis_opciones_por_toque_siguiente_paso_regreso_y_asesor(w, number, button, label, title):
    reply = reach(w, button)
    assert title in reply and "Siguiente paso" in reply
    assert {"gr_asesor", "gr_inicio"} <= set(ids_of(last(w)))
    assert "Soy Vicky" in tap(w, "gr_inicio")                                  # volver al menú
    reach(w, button)
    assert "Envié el aviso" in tap(w, "gr_asesor")
    [notice] = w.advisor()                                                     # canalizado una vez
    assert f"Tema: {label}." in notice and USER in notice
    assert w.report()[0]["Estado"] == "Solicita asesor"
    assert w.report()[0]["Producto"] == "GastroRadar: " + label
    assert "Soy Vicky" in tap(w, "gr_inicio")


@pytest.mark.parametrize("number,button,label,title", SIX)
def test_las_mismas_seis_opciones_por_numero(w, number, button, label, title):
    w.say("hola")
    assert title in w.say(number)
    assert "Soy Vicky" in w.say("menu")


def test_texto_del_beneficio(w):
    text = reach(w, "gr_beneficio").replace("*", "")
    for fact in ("12 meses del Plan Presencia de GastroRadar sin costo",
                 "mediante la contratación de una terminal punto de venta (TPV) Inbursa",
                 "sujeto a las condiciones aplicables", "La publicidad la presta GastroRadar, no Inbursa",
                 "Los banners son aparte", "Hablar con Christian"):
        assert fact in text, fact
    assert "$" not in text and "requisitos" not in text
    assert controls(last(w)) == [("gr_asesor", "Hablar con Christian"), ("gr_inicio", "Volver al menú")]


def test_el_nombre_corto_de_la_lista_lleva_la_condicion(w):
    w.say("hola"); tap(w, "gr_negocio")
    rows = last(w)["interactive"]["action"]["sections"][0]["rows"]
    [row] = [r for r in rows if r["id"] == "gr_beneficio"]
    assert row == {"id": "gr_beneficio", "title": "Un año sin costo",
                   "description": "Un año de publicidad sin costo al contratar TPV Inbursa"}
    assert all(len(r["title"]) <= 24 and len(r.get("description", "")) <= 72 for r in rows)
    assert not [r for r in rows if "Terminal" in r["title"]]


def test_el_menu_por_texto_lista_las_seis_opciones(w, monkeypatch):
    reject(w, monkeypatch, lambda p: p["type"] == "interactive")
    w.say("hola")
    body = body_of(last(w))
    for option in ("1. Encontrar comida", "2. Registrar mi negocio", "3. Planes",
                   "4. Publicidad y banners", "5. Un año de publicidad sin costo (al contratar TPV Inbursa)",
                   "6. Soporte o atención humana"):
        assert option in body
    assert "Terminal punto de venta" not in body


@pytest.mark.parametrize("phrase", [
    "terminal", "quiero una TPV", "terminal Inbursa", "punto de venta", "un año de publicidad sin costo",
    "publicidad gratis", "quiero el beneficio", "12 meses sin costo"])
def test_frases_que_abren_el_beneficio(w, phrase):
    w.say("hola")
    assert "sujeto a las condiciones aplicables" in w.say(phrase).replace("*", "")
    assert w.advisor() == []


@pytest.mark.parametrize("phrase,expected", [
    ("banner por 12 meses", "$499"), ("el registro es gratis", "El registro es gratis"),
    ("publicidad", "$499"), ("planes", "Esencial")])
def test_frases_que_no_abren_el_beneficio(w, phrase, expected):
    w.say("hola")
    reply = w.say(phrase).replace("*", "")
    assert expected in reply and "TPV" not in reply


@pytest.mark.parametrize("follow", ["¿cómo lo contrato?", "¿qué condiciones tiene?", "requisitos"])
def test_seguimiento_del_beneficio_no_cambia_de_tema(w, follow):
    reach(w, "gr_beneficio")
    assert "sujeto a las condiciones aplicables" in w.say(follow).replace("*", "")


def test_boton_antiguo_de_terminal_abre_el_beneficio(w):
    reply = w.say("", kind="interactive", interactive={
        "type": "list_reply", "list_reply": {"id": "gr_terminal", "title": "Terminal Inbursa"}})
    assert "Un año de publicidad sin costo" in reply


@pytest.mark.parametrize("phrase", ["hablar con Christian", "quiero que me contacte Christian"])
def test_pedir_a_christian_avisa(w, phrase):
    reach(w, "gr_beneficio")
    assert "Envié el aviso a Christian" in w.say(phrase)
    assert "Un año sin costo con TPV Inbursa" in w.advisor()[0]


@pytest.mark.parametrize("name", ["Christian Prueba, Tacos Ficticios", "Mariscos Christian"])
def test_el_nombre_christian_se_toma_como_dato(w, name):
    w.say("hola"); w.say("asesor")
    assert "envié tus datos" in w.say(name).lower() and name in w.advisor()[1]


def test_beneficio_solicitud_canalizada_aun_si_el_texto_libre_falla(w):
    w.meta.reject_text_to.add(ADVISOR)
    reach(w, "gr_beneficio")
    assert "Envié el aviso a Christian" in tap(w, "gr_asesor")
    assert [p["type"] for p in w.meta.payloads if p["to"] == ADVISOR] == ["template"]


def test_beneficio_si_no_sale_el_aviso_no_dice_que_se_envio(w):
    reach(w, "gr_beneficio")
    w.meta.reject_all_to.add(ADVISOR)
    reply = tap(w, "gr_asesor")
    assert "No pude enviar" in reply and "Envié el aviso" not in reply
