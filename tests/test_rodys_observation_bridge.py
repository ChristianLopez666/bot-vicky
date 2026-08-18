"""
Puente de Observaciones hacia Boardroom/Rodys (RFC-003 Fase 3).

Antes de este cambio, Vicky Redes solo hablaba con Boardroom cuando el mensaje
llegaba hasta _handle_boardroom_authority(), el ultimo `else` del dispatcher.
Todo lo que resolvia localmente -- los seis funnels activos, el menu, los
botones/listas, el arranque de campana IMSS, los referrals de Meta, la
deteccion directa de producto -- retornaba antes, asi que Rodys nunca veia esos
turnos. Son ~18 retornos locales dentro del bloque de autoridad, y ahi ocurre
practicamente toda la conversacion comercial real.

La solucion no instrumenta esos ~18 retornos uno por uno (cualquier `return`
agregado despues quedaria sin observar en silencio, y ninguna prueba lo
detectaria). En vez de eso, handle() envuelve al dispatcher y emite en un
`finally`, con un latch para que el camino de autoridad -- que ya postea de
forma sincrona -- no genere un segundo envio.

Por que importa no duplicar: Boardroom no deduplica eventos (no hay indice por
event_id ni por message_id en boardroom/rodys/), y _build_boardroom_event()
genera un uuid4 nuevo en cada llamada, asi que un dedupe futuro por event_id
tampoco atraparia el doble envio del mismo mensaje. Como FLUJO_BLOQUEADO
dispara al contar tres Observaciones consecutivas en la misma etapa, emitir dos
veces por turno haria saltar la regla en el segundo turno real: falsos
positivos fabricados por nosotros.
"""

import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


class ImmediateThread:
    """Corre el hilo daemon de la emision de forma sincrona, para poder
    afirmar sobre el POST dentro de la misma prueba."""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _text_msg(phone: str, text: str, mid: str) -> dict:
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


def _nfm_reply_msg(phone: str, mid: str) -> dict:
    return {
        "from": phone,
        "id": mid,
        "type": "interactive",
        "interactive": {
            "type": "nfm_reply",
            "nfm_reply": {"name": "flow", "response_json": '{"ok": true}'},
        },
    }


def _referral_msg(phone: str, text: str, mid: str) -> dict:
    msg = _text_msg(phone, text, mid)
    msg["referral"] = {
        "source_type": "ad",
        "headline": "Credito empresarial para tu negocio",
        "source_id": "9876",
        "ad_id": "5432",
        "campaign_id": "1111",
    }
    return msg


class _FakeResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {}


def _base_patches(monkeypatch, *, post_side_effect=None):
    """Aisla handle() de I/O real y captura cada POST a /bus/event.

    Se parchea requests.post (no _emit_boardroom_observation) a proposito: asi
    la prueba ejercita el helper real, sus headers y su manejo de errores.
    """
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_seen_ids", set())
    monkeypatch.setattr(vicky_app, "_seen_dq", vicky_app.__dict__.get("_seen_dq", []).__class__())

    # El helper corta temprano si el bus no esta configurado; en pruebas no hay
    # entorno de Render, asi que se fija explicitamente.
    monkeypatch.setattr(vicky_app, "_BUS_ACTIVE", True)
    monkeypatch.setattr(vicky_app, "BUS_URL", "https://boardroom-engine.test")
    monkeypatch.setattr(vicky_app, "BUS_INTERNAL_TOKEN", "token-de-prueba")

    sent = []
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: True)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")

    posts = []

    def fake_post(url, **kwargs):
        posts.append({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        if post_side_effect is not None:
            raise post_side_effect
        return _FakeResponse()

    monkeypatch.setattr(vicky_app.requests, "post", fake_post)
    return sent, posts


def _bus_posts(posts):
    return [p for p in posts if p["url"].endswith("/bus/event")]


# ── Una emision por mensaje, en los caminos locales ───────────────────────


def test_menu_emite_una_observacion(monkeypatch):
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-menu"))
    assert len(_bus_posts(posts)) == 1


def test_inicio_de_funnel_por_campana_emite_una_observacion(monkeypatch):
    """El evento que ABRE la conversacion.

    Un lead de campana entra por _is_campaign(), se le fija user_state y se
    llama funnel_imss(phone, "") -- retorno local, primer mensaje de la
    conversacion. Sin este evento el lead ni siquiera existiria en el store de
    Rodys, y no habria contra que comparar si algun dia retoma.
    """
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(
        "6681234567", "Hola, quiero cotizar un préstamo para pensionados del IMSS.", "mid-camp",
    ))
    assert len(_bus_posts(posts)) == 1


def test_referral_meta_emite_una_observacion(monkeypatch):
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_referral_msg("6681234567", "Hello! Can I get more info on this?", "mid-ref"))
    assert len(_bus_posts(posts)) == 1


def test_camino_de_autoridad_emite_una_sola_vez(monkeypatch):
    """El POST sincrono de _handle_boardroom_authority() ya es la emision: el
    finally no debe agregar otra."""
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "zzz texto que no rutea a nada", "mid-auth"))
    assert len(_bus_posts(posts)) == 1


# ── Conteo por turnos: la aritmetica de la que depende FLUJO_BLOQUEADO ────


def test_dos_turnos_generan_exactamente_dos_observaciones(monkeypatch):
    """Dos turnos reales nunca deben verse como tres.

    FLUJO_BLOQUEADO dispara con tres Observaciones consecutivas en la misma
    etapa. Si cada turno emitiera dos veces, la regla saltaria en el segundo
    turno real. El umbral vive en boardroom-engine; lo que se afirma aqui es la
    entrada que ese umbral consume.
    """
    _, posts = _base_patches(monkeypatch)
    vicky_app.user_state["6681234567"] = "imss_q_pension_calc"
    vicky_app.handle(_text_msg("6681234567", "Seis mil pesos", "mid-t1"))
    vicky_app.handle(_text_msg("6681234567", "6000 mil pesos", "mid-t2"))
    assert len(_bus_posts(posts)) == 2


def test_tres_turnos_reales_generan_tres_observaciones(monkeypatch):
    """Los tres textos son los de un lead real del 17-ago que tuvo que decir el
    monto tres veces (logs de bot-vicky-redes): la firma de FLUJO_BLOQUEADO que
    hoy Rodys no ve."""
    _, posts = _base_patches(monkeypatch)
    vicky_app.user_state["6681234567"] = "imss_q_pension_calc"
    for i, texto in enumerate(("Seis mil pesos", "6000 mil pesos", "6000")):
        vicky_app.handle(_text_msg("6681234567", texto, f"mid-t{i}"))
    assert len(_bus_posts(posts)) == 3


# ── Exclusiones y deduplicacion ───────────────────────────────────────────


def test_nfm_reply_no_emite_nada(monkeypatch):
    """nfm_reply esta fuera de alcance de Boardroom y retorna antes de que el
    evento se construya: queda excluido por construccion, no por un check."""
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_nfm_reply_msg("6681234567", "mid-nfm"))
    assert _bus_posts(posts) == []


def test_mensaje_duplicado_de_whatsapp_emite_una_sola_vez(monkeypatch):
    """El reintento de Meta trae el mismo message_id y retorna en el dedupe de
    _seen_ids, antes de construir el evento."""
    _, posts = _base_patches(monkeypatch)
    msg = _text_msg("6681234567", "menu", "mid-repetido")
    vicky_app.handle(msg)
    vicky_app.handle(msg)
    assert len(_bus_posts(posts)) == 1


def test_thread_local_sucio_no_reemite_el_evento_anterior(monkeypatch):
    """El reset de _tl al entrar a handle() es la segunda linea de defensa.

    En el camino normal, _flush_boardroom_observation() ya limpia el
    thread-local, asi que el reset parece redundante. Deja de serlo si el hilo
    quedo sucio por cualquier otra causa: una llamada previa que murio antes de
    llegar al flush, o un cambio futuro que agregue un retorno temprano al
    flush. Se simula ensuciando _tl a mano y procesando un mensaje que no
    construye evento propio (nfm_reply): sin el reset, se emitiria el evento
    ajeno.
    """
    _, posts = _base_patches(monkeypatch)
    vicky_app._tl.boardroom_event = {
        "event_id": "evento-de-otro-mensaje",
        "phone": "6689999999",
        "text": "mensaje anterior",
    }
    vicky_app._tl.boardroom_emitted = False

    vicky_app.handle(_nfm_reply_msg("6681234567", "mid-sucio"))
    assert _bus_posts(posts) == []


def test_dos_mensajes_en_el_mismo_hilo_sin_contaminacion(monkeypatch):
    """gunicorn reusa hilos: el segundo mensaje no debe reemitir el evento del
    primero. Aqui el segundo es nfm_reply, que no construye evento propio --
    sin el reset del thread-local, el finally reemitiria el del primero."""
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-a"))
    vicky_app.handle(_nfm_reply_msg("6681234567", "mid-b"))
    bus = _bus_posts(posts)
    assert len(bus) == 1
    assert bus[0]["json"]["text"] == "menu"


# ── Fallas de Boardroom: nunca deben tocar la conversacion ────────────────


def test_timeout_sincrono_no_dispara_segundo_post(monkeypatch):
    """El latch sube ANTES del POST sincrono. Si ese POST expira, el finally no
    debe mandar un segundo intento del mismo mensaje."""
    _, posts = _base_patches(monkeypatch, post_side_effect=requests.exceptions.Timeout())
    vicky_app.handle(_text_msg("6681234567", "zzz texto que no rutea a nada", "mid-timeout"))
    assert len(_bus_posts(posts)) == 1


def test_falla_de_boardroom_no_rompe_el_funnel(monkeypatch):
    """La emision es fire-and-forget: si el POST truena, el funnel local ya
    avanzo y el cliente recibio su respuesta igual."""
    sent, posts = _base_patches(monkeypatch, post_side_effect=requests.exceptions.ConnectionError())
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-falla"))
    assert len(_bus_posts(posts)) == 1
    assert sent, "el menu debe haberse enviado pese a la falla de Boardroom"


def test_excepcion_en_funnel_emite_una_sola_observacion(monkeypatch):
    """Si el turno truena a mitad, el evento recibido se observa igual (el
    finally corre), y exactamente una vez."""
    _, posts = _base_patches(monkeypatch)

    def explota(*a, **k):
        raise RuntimeError("fallo simulado dentro del funnel")

    monkeypatch.setattr(vicky_app, "funnel_imss", explota)
    vicky_app.user_state["6681234567"] = "imss_monto"
    with pytest.raises(RuntimeError):
        vicky_app.handle(_text_msg("6681234567", "10000", "mid-boom"))
    assert len(_bus_posts(posts)) == 1


# ── Forma del payload ─────────────────────────────────────────────────────


def test_payload_conserva_la_etapa_anterior_al_procesamiento(monkeypatch):
    """last_known_stage debe ser la etapa a la que LLEGO el mensaje, no la que
    quedo despues. FLUJO_BLOQUEADO cuenta repeticiones de la misma etapa: si se
    capturara la etapa posterior, un funnel que avanza normal se veria como
    etapas distintas y uno atascado podria verse movido."""
    _, posts = _base_patches(monkeypatch)
    # Transicion real y verificada del funnel fp: fp_tipo + "1" -> fp_monto.
    vicky_app.user_state["6681234567"] = "fp_tipo"
    vicky_app.handle(_text_msg("6681234567", "1", "mid-etapa"))

    bus = _bus_posts(posts)
    assert len(bus) == 1
    assert bus[0]["json"]["conversation"]["last_known_stage"] == "fp_tipo"
    # El funnel si avanzo: lo capturado es la etapa previa, no la resultante.
    assert vicky_app.user_state["6681234567"] == "fp_monto"


def test_observacion_usa_el_contrato_canonico_que_boardroom_acepta(monkeypatch):
    """Los headers y el canal tienen que pasar _handle_canonical_vicky_event()
    de boardroom-engine, que responde 400 si no coinciden."""
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-contrato"))

    post = _bus_posts(posts)[0]
    assert post["headers"]["X-Source-System"] == "vicky"
    assert post["headers"]["X-Event-Type"] == "inbound_message"
    assert post["json"]["channel"] == "vicky_campanas"
    # _CANONICAL_MESSAGE_TYPES en boardroom-engine.
    assert post["json"]["message_type"] in {"text", "audio", "image", "document", "button", "unknown"}
    assert post["json"]["conversation"]["conversation_id"] == "vicky_campanas:6681234567"


def test_un_solo_event_id_por_mensaje_en_el_camino_de_autoridad(monkeypatch):
    """El camino de autoridad reusa el evento construido en handle(), no crea
    uno nuevo: un mensaje, un event_id."""
    _, posts = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "zzz texto que no rutea a nada", "mid-uno"))

    bus = _bus_posts(posts)
    assert len(bus) == 1
    assert bus[0]["json"]["event_id"]
    assert bus[0]["json"]["message_id"] == "mid-uno"
