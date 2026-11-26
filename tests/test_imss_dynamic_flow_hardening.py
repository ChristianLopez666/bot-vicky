"""Correcciones derivadas de la auditoria externa del Flow dinamico IMSS
(2026-08-10). CADA test de este archivo falla contra la version anterior del
codigo -- no son tests de confirmacion, son la reproduccion de defectos
reales encontrados en revision:

  1. notify_advisor() se disparaba SIN deduplicar en los pasos PROFILE-3 y
     PENSION, no solo en HANDOFF. Un reintento de Meta duplicaba la alerta.
  2. send_imss_dynamic_flow() ignoraba el bool de aux_set(): un fallo del
     store dejaba el Flow entregado pero SIN correlacion flow_token->telefono,
     es decir un Flow muerto ("tu sesion expiro") en la primera pantalla.
  3. El HANDOFF marcaba "done" ANTES de ejecutar sus efectos: si el mensaje
     de cierre fallaba, el reintento respondia "duplicado" y el prospecto se
     quedaba esperando una pregunta que nunca vio.
  4. Los tests de ruta sustituian _verify_sig por un stub, asi que nunca se
     probo la implementacion HMAC real contra el header que Meta si envia
     (verificado en la documentacion oficial: Meta firma TODAS las
     solicitudes al Flow Endpoint con X-Hub-Signature-256 y la validacion es
     obligatoria).
"""

import hashlib
import hmac
import json
import os
import sys
from base64 import b64encode

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers import algorithms, Cipher, modes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app
import imss_flow

PHONE = "5216681234567"

# Referencia a la implementacion REAL, capturada antes de que el fixture
# autouse la sustituya por un stub. Los tests que verifican el contrato de la
# propia funcion (2xx -> True, resto -> False) la reinstalan.
_BOARDROOM_REAL = vicky_app._notify_boardroom_lead_qualified


class FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _aislar(monkeypatch):
    """Estado limpio por test. _state_store nuevo en cada uno para que las
    claves de dedupe de un test no contaminen al siguiente."""
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_state_store", vicky_app.StateStore())
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")
    monkeypatch.setattr(vicky_app, "_imss_log_lead_backup", lambda *a, **k: None)
    # True = Boardroom confirmo el alta. El stub tiene que declararlo
    # explicitamente: desde la correccion del contrato, un retorno falsy
    # significa "el alta NO se confirmo" y el Flow no marca el dedupe.
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: True)
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_FLOW_ID", "flow-id-de-prueba")
    monkeypatch.setattr(vicky_app, "META_TOKEN", "token-de-prueba")
    monkeypatch.setattr(vicky_app, "WABA_ID", "waba-de-prueba")


@pytest.fixture
def avisos(monkeypatch):
    enviados = []
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: enviados.append(msg) or True)
    return enviados


# ─────────────────────────────────────────────────────────────────────────────
# Defecto 1 -- idempotencia de notify_advisor en PROFILE y PENSION
# ─────────────────────────────────────────────────────────────────────────────

def test_perfil_3_reintentado_no_duplica_la_alerta_al_asesor(avisos):
    """Meta reintenta el data_exchange de la pantalla de perfil. El asesor no
    puede recibir dos veces "INTERES FUTURO" por el mismo prospecto."""
    token = imss_flow.generate_flow_token()
    r1 = vicky_app._imss_flow_handle_profile(PHONE, {"profile": "3"}, token)
    r2 = vicky_app._imss_flow_handle_profile(PHONE, {"profile": "3"}, token)

    assert r1["screen"] == imss_flow.SCREEN_REJECTED
    assert r2["screen"] == imss_flow.SCREEN_REJECTED
    assert len(avisos) == 1
    assert "INTERÉS FUTURO" in avisos[0]


def test_pension_reintentada_con_la_misma_cifra_no_duplica_la_propuesta(avisos):
    token = imss_flow.generate_flow_token()
    paso = {"profile": "1", "pension": "12000"}
    r1 = vicky_app._imss_flow_handle_pension(PHONE, paso, token)
    r2 = vicky_app._imss_flow_handle_pension(PHONE, paso, token)

    assert r1["screen"] == imss_flow.SCREEN_PROPOSAL
    assert r2["screen"] == imss_flow.SCREEN_PROPOSAL
    assert r1["data"]["monto"] == r2["data"]["monto"]
    assert len(avisos) == 1


def test_pension_corregida_por_el_prospecto_si_vuelve_a_notificar(avisos):
    """routing_model permite IMSS_HANDOFF -> IMSS_PENSION ("cambiar monto o
    plazo"). Una cifra DISTINTA es una propuesta distinta: el asesor tiene
    que enterarse, no puede quedar deduplicada con la anterior."""
    token = imss_flow.generate_flow_token()
    vicky_app._imss_flow_handle_pension(PHONE, {"profile": "1", "pension": "12000"}, token)
    vicky_app._imss_flow_handle_pension(PHONE, {"profile": "1", "pension": "20000"}, token)

    assert len(avisos) == 2
    assert "$12,000" in avisos[0]
    assert "$20,000" in avisos[1]


def test_tokens_distintos_no_comparten_dedupe(avisos):
    """Dos prospectos (o dos sesiones del mismo) no pueden silenciarse entre
    si por compartir la clave de deduplicacion."""
    vicky_app._imss_flow_handle_profile(PHONE, {"profile": "3"}, imss_flow.generate_flow_token())
    vicky_app._imss_flow_handle_profile(PHONE, {"profile": "3"}, imss_flow.generate_flow_token())
    assert len(avisos) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Defecto 2 -- un Flow sin correlacion persistida no puede considerarse enviado
# ─────────────────────────────────────────────────────────────────────────────

def _romper_aux_set(monkeypatch, prefijo: str, solo_valor=None):
    """Hace fallar aux_set() SOLO para las claves de un prefijo, como haria
    un Redis intermitente. Con `solo_valor` se restringe aun mas el fallo a
    las escrituras de ese valor concreto -- necesario para reproducir un
    borrado fallido (valor "") sin impedir la escritura previa (valor "1").
    Devuelve la lista de claves cuyo write se rechazo."""
    rechazadas = []
    real_aux_set = vicky_app._state_store.aux_set

    def aux_set_selectivo(key, value, ttl):
        if key.startswith(prefijo) and (solo_valor is None or value == solo_valor):
            rechazadas.append(key)
            return False
        return real_aux_set(key, value, ttl)

    monkeypatch.setattr(vicky_app._state_store, "aux_set", aux_set_selectivo)
    return rechazadas


def test_si_no_se_puede_persistir_la_correlacion_no_se_envia_el_flow(monkeypatch):
    """El endpoint resuelve el telefono desde flow_token. Sin esa correlacion
    el Flow llega vivo pero inservible. Preferimos no enviarlo y caer al
    funnel legacy, que si funciona."""
    enviados = []
    monkeypatch.setattr(vicky_app, "_wa_post",
                        lambda payload: enviados.append(payload) or FakeResp(200))
    _romper_aux_set(monkeypatch, "imss_flow_token:")

    assert vicky_app.send_imss_dynamic_flow(PHONE) == vicky_app.IMSS_FLOW_FALLIDO
    assert enviados == [], "no debe salir NADA hacia Meta si la correlacion no quedo asegurada"


def test_correlacion_fallida_es_FALLIDO_no_AMBIGUO(monkeypatch):
    """Distincion critica: nada salio hacia Meta, asi que route() SI debe
    caer al funnel legacy. Clasificarlo como ambiguo dejaria al prospecto sin
    ninguna respuesta."""
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: FakeResp(200))
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_DYNAMIC_FLOW_ENABLED", True)
    _romper_aux_set(monkeypatch, "imss_flow_token:")

    llamadas_legacy = []
    monkeypatch.setattr(vicky_app, "funnel_imss",
                        lambda phone, msg: llamadas_legacy.append(phone))

    vicky_app.route(PHONE, "imss")
    assert llamadas_legacy == [PHONE]


def test_envio_exitoso_deja_la_correlacion_consultable(monkeypatch):
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: FakeResp(200))
    assert vicky_app.send_imss_dynamic_flow(PHONE) == vicky_app.IMSS_FLOW_ENVIADO

    tokens = [k for k in _claves_aux() if k.startswith("imss_flow_token:")]
    assert len(tokens) == 1
    assert vicky_app._state_store.aux_get(tokens[0]) == PHONE


def _claves_aux():
    """Las claves vivas del StateStore en modo memoria (el de los tests)."""
    return list(vicky_app._state_store._aux_mem.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Defecto 2b / hallazgo original -- Flow + legacy duplicados
# ─────────────────────────────────────────────────────────────────────────────

def test_entrega_ambigua_bloquea_el_fallback_a_legacy(monkeypatch):
    """La peticion salio hacia Meta y nunca hubo respuesta. Meta pudo haberla
    entregado: apilar el menu de texto legacy encima del Flow es la mala
    experiencia que este Flow viene a corregir."""
    def post_que_revienta(payload):
        raise requests.exceptions.ConnectionError("se cayo la red")

    monkeypatch.setattr(vicky_app, "_wa_post", post_que_revienta)
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_DYNAMIC_FLOW_ENABLED", True)

    llamadas_legacy = []
    monkeypatch.setattr(vicky_app, "funnel_imss",
                        lambda phone, msg: llamadas_legacy.append(phone))

    vicky_app.route(PHONE, "imss")

    assert llamadas_legacy == [], "no debe caer a legacy cuando la entrega fue ambigua"


def test_la_ambiguedad_no_depende_del_state_store(monkeypatch):
    """El estado de entrega viaja en el valor de retorno, dentro de la misma
    pila de llamadas. Aunque el store rechace TODAS las escrituras despues de
    la correlacion, la ambiguedad no se puede perder -- una version anterior
    la pasaba por un marcador auxiliar y ese marcador si podia perderse,
    reabriendo el defecto de Flow + legacy duplicados."""
    def post_que_revienta(payload):
        raise requests.exceptions.ConnectionError("se cayo la red")

    monkeypatch.setattr(vicky_app, "_wa_post", post_que_revienta)
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_DYNAMIC_FLOW_ENABLED", True)

    llamadas_legacy = []
    monkeypatch.setattr(vicky_app, "funnel_imss",
                        lambda phone, msg: llamadas_legacy.append(phone))

    # El store acepta la correlacion y rechaza todo lo demas.
    _romper_aux_set(monkeypatch, "imss_flow_ambiguo")

    vicky_app.route(PHONE, "imss")
    assert llamadas_legacy == []


def test_rechazo_explicito_de_meta_si_cae_a_legacy(monkeypatch):
    """Meta respondio 400: la entrega NO ocurrio, no hay ambiguedad. Aqui el
    fallback legacy es obligatorio -- el prospecto tiene que ser atendido."""
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: FakeResp(400, "rechazado"))
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_DYNAMIC_FLOW_ENABLED", True)

    llamadas_legacy = []
    monkeypatch.setattr(vicky_app, "funnel_imss",
                        lambda phone, msg: llamadas_legacy.append(phone))

    vicky_app.route(PHONE, "imss")

    assert llamadas_legacy == [PHONE]
    assert vicky_app.user_state[PHONE] == "imss_open"


def test_rechazo_de_meta_cae_a_legacy_aunque_no_se_pueda_borrar_nada(monkeypatch):
    """Variante INVERSA del defecto. El diseno anterior marcaba la ambiguedad
    antes de enviar y la borraba al recibir respuesta; si ese BORRADO fallaba
    (pero la escritura previa si habia funcionado), un rechazo EXPLICITO de
    Meta se leia como ambiguo y suprimia el fallback, dejando al prospecto
    sin ninguna respuesta.

    Se reproduce con precision: se permiten las escrituras de valor "1" (la
    marca) y se rechazan solo las de valor "" (el borrado)."""
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: FakeResp(400, "rechazado"))
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_DYNAMIC_FLOW_ENABLED", True)
    _romper_aux_set(monkeypatch, "imss_flow_ambiguo", solo_valor="")

    llamadas_legacy = []
    monkeypatch.setattr(vicky_app, "funnel_imss",
                        lambda phone, msg: llamadas_legacy.append(phone))

    vicky_app.route(PHONE, "imss")
    assert llamadas_legacy == [PHONE], "un rechazo explicito SIEMPRE debe caer a legacy"


# ─────────────────────────────────────────────────────────────────────────────
# Defecto 3 -- semantica de "completado" en el HANDOFF
# ─────────────────────────────────────────────────────────────────────────────

def _preparar_prospecto_con_propuesta(token, avisos):
    vicky_app._imss_flow_handle_profile(PHONE, {"profile": "1"}, token)
    vicky_app._imss_flow_handle_pension(PHONE, {"profile": "1", "pension": "12000"}, token)
    avisos.clear()


def test_handoff_captura_el_horario_dentro_del_flow(monkeypatch, avisos):
    """El horario se pregunta DENTRO del Flow, junto al nombre. Al salir, el
    prospecto ya quedo agendado: no se le deja un estado abierto esperando
    que conteste 1/2/3 por texto."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    enviados = []
    monkeypatch.setattr(vicky_app, "send_msg",
                        lambda to, text: enviados.append(text) or True)

    etiquetas = vicky_app._imss_horarios_ofrecidos(vicky_app.user_data[PHONE])
    r = vicky_app._imss_flow_handle_handoff(
        PHONE, {"nombre": "Juan Pérez", "horario": "1"}, token)

    assert r["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert vicky_app.user_data[PHONE]["horario_contacto"] == etiquetas["1"]
    # Ya no queda esperando la respuesta 1/2/3 por texto.
    assert vicky_app.user_state.get(PHONE) != "imss_q_horario_calc"
    # Y el asesor recibe el horario en la MISMA ficha, no en un aviso aparte.
    assert len(avisos) == 1
    assert etiquetas["1"] in avisos[0]


def test_handoff_con_otro_horario_sigue_esperando_el_texto(monkeypatch, avisos):
    """Unico caso que queda abierto: si elige "otro dia y horario", falta el
    dato y hay que pedirlo por texto en el estado de siempre."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    enviados = []
    monkeypatch.setattr(vicky_app, "send_msg",
                        lambda to, text: enviados.append(text) or True)

    etiquetas = vicky_app._imss_horarios_ofrecidos(vicky_app.user_data[PHONE])
    otro = next(k for k, v in etiquetas.items()
                if v == vicky_app._IMSS_OTRO_HORARIO_LABEL)

    vicky_app._imss_flow_handle_handoff(
        PHONE, {"nombre": "Juan Pérez", "horario": otro}, token)

    assert not vicky_app.user_data[PHONE].get("horario_contacto")
    assert vicky_app.user_state.get(PHONE) == "imss_q_horario_calc"
    assert any("horario" in m.lower() for m in enviados)


def test_handoff_con_cierre_fallido_no_se_marca_completado(monkeypatch, avisos):
    """Si el mensaje de cierre no se entrego, el prospecto queda esperando
    una pregunta de horario que nunca vio. Un reintento de Meta DEBE volver a
    intentar el cierre -- no responder "duplicado" y abandonarlo."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: False)
    paso = {"nombre": "Juan Pérez", "ciudad": "Culiacán"}

    r1 = vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    r2 = vicky_app._imss_flow_handle_handoff(PHONE, paso, token)

    params1 = r1["data"]["extension_message_response"]["params"]
    params2 = r2["data"]["extension_message_response"]["params"]
    assert params1["resultado"] == "calificado"
    assert params2["resultado"] == "calificado", "el reintento NO debe darse por duplicado"
    # ...pero el asesor recibe la ficha una sola vez.
    assert len(avisos) == 1


def test_handoff_solo_requiere_nombre_sin_ciudad(monkeypatch, avisos):
    """La pantalla de handoff dejo de pedir ciudad: teclear es lo mas dificil
    para el publico objetivo y era el ultimo paso, donde mas caro sale perder
    al prospecto. El paso debe calificar con el nombre solo, y la ficha del
    asesor simplemente no trae el renglon de Ciudad."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: True)

    r = vicky_app._imss_flow_handle_handoff(PHONE, {"nombre": "Juan Pérez"}, token)

    assert r["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert vicky_app.user_data[PHONE]["nombre"] == "Juan Pérez"
    assert not vicky_app.user_data[PHONE].get("ciudad")
    assert len(avisos) == 1
    assert "Ciudad:" not in avisos[0]


def test_handoff_sigue_exigiendo_nombre(monkeypatch, avisos):
    """Sin nombre no hay ficha que mandar: debe regresar a la misma pantalla
    con error, no calificar al prospecto."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: True)

    r = vicky_app._imss_flow_handle_handoff(PHONE, {"nombre": "   "}, token)

    assert r["screen"] == imss_flow.SCREEN_HANDOFF
    assert avisos == []


def test_handoff_exitoso_si_deduplica_el_reintento(monkeypatch, avisos):
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)

    enviados = []
    monkeypatch.setattr(vicky_app, "send_msg",
                        lambda to, text: enviados.append(text) or True)
    paso = {"nombre": "Juan Pérez", "ciudad": "Culiacán"}

    r1 = vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    mensajes_tras_primer_intento = len(enviados)
    r2 = vicky_app._imss_flow_handle_handoff(PHONE, paso, token)

    assert r1["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert r2["data"]["extension_message_response"]["params"]["resultado"] == "duplicado"
    assert len(enviados) == mensajes_tras_primer_intento, "no se reenvia nada al prospecto"
    assert len(avisos) == 1


def test_handoff_recuperado_deja_de_reintentar_al_entregar_el_cierre(monkeypatch, avisos):
    """Ciclo completo del caso real: primer intento falla el cierre, el
    reintento lo entrega, y a partir de ahi si queda cerrado."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    paso = {"nombre": "Juan Pérez", "ciudad": "Culiacán"}

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: False)
    vicky_app._imss_flow_handle_handoff(PHONE, paso, token)

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: True)
    r2 = vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    r3 = vicky_app._imss_flow_handle_handoff(PHONE, paso, token)

    assert r2["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert r3["data"]["extension_message_response"]["params"]["resultado"] == "duplicado"
    assert len(avisos) == 1
    assert vicky_app.user_state[PHONE] == "imss_q_horario_calc"


def test_boardroom_se_notifica_una_sola_vez_aunque_se_reintente(monkeypatch, avisos):
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)

    altas = []
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified",
                        lambda phone, code, data: altas.append((phone, code)) or True)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: False)
    paso = {"nombre": "Juan Pérez", "ciudad": "Culiacán"}

    vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    vicky_app._imss_flow_handle_handoff(PHONE, paso, token)

    assert len(altas) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Degradacion del StateStore en los guardrails de idempotencia
# ─────────────────────────────────────────────────────────────────────────────
# aux_set() puede devolver False sin lanzar. Ningun guardrail nuevo puede
# ignorar ese contrato en silencio: donde el efecto ya ocurrio y no se puede
# deshacer, el minimo exigible es que quede REGISTRADO con un motivo propio,
# para que un duplicado en produccion sea diagnosticable en vez de inexplicable.

def test_marca_no_persistida_queda_registrada_con_motivo(monkeypatch, caplog):
    _romper_aux_set(monkeypatch, "imss_flow_")
    with caplog.at_level("ERROR"):
        assert vicky_app._imss_flow_marcar("imss_flow_notif:tok:handoff") is False
    assert "imss_flow_marca_no_persistida" in caplog.text


def test_marca_persistida_no_registra_error(monkeypatch, caplog):
    with caplog.at_level("ERROR"):
        assert vicky_app._imss_flow_marcar("imss_flow_notif:tok:handoff") is True
    assert "imss_flow_marca_no_persistida" not in caplog.text


def test_dedupe_del_asesor_caido_no_rompe_el_paso(monkeypatch, avisos, caplog):
    """Con el store rechazando writes, el asesor sigue recibiendo su alerta
    (el efecto importante NO se pierde) y la degradacion queda registrada."""
    _romper_aux_set(monkeypatch, "imss_flow_notif:")
    token = imss_flow.generate_flow_token()

    with caplog.at_level("ERROR"):
        r = vicky_app._imss_flow_handle_profile(PHONE, {"profile": "3"}, token)

    assert r["screen"] == imss_flow.SCREEN_REJECTED
    assert len(avisos) == 1
    assert "imss_flow_marca_no_persistida" in caplog.text


def test_dedupe_de_boardroom_caido_no_rompe_el_handoff(monkeypatch, avisos, caplog):
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)

    altas = []
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified",
                        lambda phone, code, data: altas.append(phone) or True)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: True)
    _romper_aux_set(monkeypatch, "imss_flow_boardroom:")

    with caplog.at_level("ERROR"):
        r = vicky_app._imss_flow_handle_handoff(
            PHONE, {"nombre": "Juan Pérez", "ciudad": "Culiacán"}, token)

    assert r["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert len(altas) == 1
    assert "imss_flow_marca_no_persistida" in caplog.text


def test_boardroom_fallido_se_reintenta_y_solo_despues_deja_de_intentarse(monkeypatch, avisos):
    """Ciclo completo del ultimo defecto encontrado en auditoria.

    _notify_boardroom_lead_qualified() absorbe sus propios errores (timeout,
    HTTP no exitoso, servicio sin configurar), asi que marcar el dedupe por
    el simple hecho de HABERLO INTENTADO perdia el lead para siempre: el
    reintento de Meta veia la clave puesta y ya no volvia a intentarlo.

    Secuencia: falla -> no queda dedupe -> reintento funciona -> queda
    dedupe -> tercer reintento no duplica."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    paso = {"nombre": "Juan Pérez", "ciudad": "Culiacán"}

    intentos = []
    boardroom_disponible = {"ok": False}

    def boardroom(phone, code, data):
        intentos.append(phone)
        return boardroom_disponible["ok"]

    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", boardroom)
    # El cierre al prospecto falla, para que el HANDOFF no quede completado y
    # Meta pueda reintentarlo -- que es justo cuando debe recuperarse el alta.
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: False)

    vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    assert len(intentos) == 1, "primer intento"
    assert not vicky_app._state_store.aux_get(f"imss_flow_boardroom:{token}"), \
        "un alta NO confirmada no puede quedar marcada como hecha"

    boardroom_disponible["ok"] = True
    vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    assert len(intentos) == 2, "el reintento SI debe volver a intentar el alta"
    assert vicky_app._state_store.aux_get(f"imss_flow_boardroom:{token}")

    vicky_app._imss_flow_handle_handoff(PHONE, paso, token)
    assert len(intentos) == 2, "ya confirmada, no se vuelve a dar de alta"


def test_boardroom_no_configurado_no_marca_dedupe(monkeypatch, avisos):
    """Sin BOARDROOM_URL/TOKEN la funcion real devuelve False sin enviar
    nada. Ese caso tampoco puede consumir el dedupe."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", _BOARDROOM_REAL)
    monkeypatch.setattr(vicky_app, "BOARDROOM_URL", "")
    monkeypatch.setattr(vicky_app, "BOARDROOM_API_TOKEN", "")
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: True)

    vicky_app._imss_flow_handle_handoff(
        PHONE, {"nombre": "Juan Pérez", "ciudad": "Culiacán"}, token)

    assert not vicky_app._state_store.aux_get(f"imss_flow_boardroom:{token}")


def test_boardroom_http_no_exitoso_cuenta_como_fallo(monkeypatch):
    """Contrato de la funcion real: 2xx -> True; cualquier otro codigo o
    excepcion -> False. Se ejercita la implementacion REAL, no un stub."""
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", _BOARDROOM_REAL)
    monkeypatch.setattr(vicky_app, "BOARDROOM_URL", "https://boardroom.example")
    monkeypatch.setattr(vicky_app, "BOARDROOM_API_TOKEN", "token")

    monkeypatch.setattr(vicky_app.requests, "post", lambda *a, **k: FakeResp(200))
    assert vicky_app._notify_boardroom_lead_qualified(PHONE, "prestamo_imss_ley73", {}) is True

    monkeypatch.setattr(vicky_app.requests, "post", lambda *a, **k: FakeResp(500, "boom"))
    assert vicky_app._notify_boardroom_lead_qualified(PHONE, "prestamo_imss_ley73", {}) is False

    def revienta(*a, **k):
        raise requests.exceptions.ConnectionError("sin red")

    monkeypatch.setattr(vicky_app.requests, "post", revienta)
    assert vicky_app._notify_boardroom_lead_qualified(PHONE, "prestamo_imss_ley73", {}) is False


def test_marca_de_completado_caida_queda_registrada(monkeypatch, avisos, caplog):
    """Si no se puede persistir "completado", el prospecto SI recibio su
    cierre (lo importante) pero un reintento de Meta podria reenviarlo. Ese
    riesgo tiene que quedar en el log, no desaparecer."""
    token = imss_flow.generate_flow_token()
    _preparar_prospecto_con_propuesta(token, avisos)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: True)
    _romper_aux_set(monkeypatch, "imss_flow_handoff_completado:")

    with caplog.at_level("ERROR"):
        r = vicky_app._imss_flow_handle_handoff(
            PHONE, {"nombre": "Juan Pérez", "ciudad": "Culiacán"}, token)

    assert r["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert vicky_app.user_state[PHONE] == "imss_q_horario_calc"
    assert "imss_flow_marca_no_persistida" in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# Defecto 4 -- firma HMAC REAL, sin stub de _verify_sig
# ─────────────────────────────────────────────────────────────────────────────
# Meta firma TODAS las solicitudes al Flow Endpoint con X-Hub-Signature-256 y
# la validacion es obligatoria (documentacion oficial, verificada 2026-08-10).
# Estos tests ejercitan la implementacion HMAC real de _verify_sig().

SECRETO = "secreto_de_app_de_prueba"


@pytest.fixture
def cliente_con_firma_real(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    monkeypatch.setattr(vicky_app, "APP_SECRET", SECRETO)
    monkeypatch.setattr(vicky_app, "IMSS_FLOW_PRIVATE_KEY", private_pem)
    vicky_app.app.config["TESTING"] = True
    return vicky_app.app.test_client(), private_key.public_key()


def _cuerpo_cifrado(public_key, payload):
    aes_key = os.urandom(16)
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(aes_key), modes.GCM(iv)).encryptor()
    ct = enc.update(json.dumps(payload).encode("utf-8")) + enc.finalize()
    enc_key = public_key.encrypt(
        aes_key, OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return json.dumps({
        "encrypted_flow_data": b64encode(ct + enc.tag).decode("utf-8"),
        "encrypted_aes_key": b64encode(enc_key).decode("utf-8"),
        "initial_vector": b64encode(iv).decode("utf-8"),
    }).encode("utf-8")


def _firmar(cuerpo: bytes, secreto: str) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def test_firma_hmac_real_valida_es_aceptada(cliente_con_firma_real):
    """Prueba la implementacion REAL de _verify_sig (sin stub) con un HMAC
    calculado igual que lo hace Meta."""
    client, public_key = cliente_con_firma_real
    cuerpo = _cuerpo_cifrado(public_key, {"action": "ping"})

    resp = client.post("/ext/flow/imss", data=cuerpo, content_type="application/json",
                       headers={"X-Hub-Signature-256": _firmar(cuerpo, SECRETO)})

    assert resp.status_code == 200


def test_firma_hmac_real_con_secreto_equivocado_es_rechazada(cliente_con_firma_real):
    client, public_key = cliente_con_firma_real
    cuerpo = _cuerpo_cifrado(public_key, {"action": "ping"})

    resp = client.post("/ext/flow/imss", data=cuerpo, content_type="application/json",
                       headers={"X-Hub-Signature-256": _firmar(cuerpo, "otro_secreto")})

    assert resp.status_code == 403


def test_cuerpo_alterado_invalida_la_firma(cliente_con_firma_real):
    """La firma cubre el cuerpo: si alguien lo modifica en transito, el HMAC
    deja de coincidir."""
    client, public_key = cliente_con_firma_real
    cuerpo = _cuerpo_cifrado(public_key, {"action": "ping"})
    firma = _firmar(cuerpo, SECRETO)
    # Alteracion real de los bytes (mismo largo, contenido distinto): la firma
    # se calculo sobre el cuerpo original y ya no puede coincidir.
    cuerpo_alterado = cuerpo.replace(b'"encrypted_flow_data"', b'"encrypted_flow_dat0"')
    assert cuerpo_alterado != cuerpo, "la alteracion debe modificar el cuerpo de verdad"

    resp = client.post("/ext/flow/imss", data=cuerpo_alterado, content_type="application/json",
                       headers={"X-Hub-Signature-256": firma})

    assert resp.status_code == 403


def test_sin_header_de_firma_es_rechazado(cliente_con_firma_real):
    client, public_key = cliente_con_firma_real
    cuerpo = _cuerpo_cifrado(public_key, {"action": "ping"})

    resp = client.post("/ext/flow/imss", data=cuerpo, content_type="application/json")

    assert resp.status_code == 403


def test_sin_app_secret_configurado_el_endpoint_queda_cerrado(monkeypatch, cliente_con_firma_real):
    """Sin META_APP_SECRET no se puede validar nada: el endpoint no debe
    aceptar trafico "por si acaso"."""
    client, public_key = cliente_con_firma_real
    monkeypatch.setattr(vicky_app, "APP_SECRET", "")
    cuerpo = _cuerpo_cifrado(public_key, {"action": "ping"})

    resp = client.post("/ext/flow/imss", data=cuerpo, content_type="application/json",
                       headers={"X-Hub-Signature-256": _firmar(cuerpo, SECRETO)})

    assert resp.status_code == 403
