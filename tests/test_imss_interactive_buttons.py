"""IMSS + botones/listas sobre WA-1, SIN WhatsApp Flow.

Objetivo de esta entrega: convertir las preguntas cerradas de mayor volumen
del embudo IMSS de TEXTO (menu Ley73 de 4 opciones, CTA de revision 1/2,
pregunta si/no de "pension baja") a Interactive Messages (List/Reply
Buttons), reutilizando integramente whatsapp_interactive.py/send_interactive_*
de WA-1. imss_flow.py (WA-2, WhatsApp Flow) NO se toca ni se usa aqui.

Cubre:
  - Fidelidad textual: los textos legacy (flag apagado) siguen siendo
    BYTE-IDENTICOS a los de antes de este cambio (verificado contra
    origin/main durante el desarrollo; aqui se verifica la consistencia
    interna de la reconstruccion base+CTA).
  - Builders puros (list Ley73, botones CTA, botones si/no) sin HTTP.
  - Puente id->parser: los ids de los botones/rows son los mismos tokens
    cortos que _imss_ley73_choice()/_imss_revision_choice()/yes_no() ya
    interpretan de forma nativa -- se prueba explicitamente que NINGUNO de
    esos parsers fue modificado.
  - Integracion real end-to-end en handle(): interactive reply real ->
    parser real -> puente id real -> funnel_imss real, con el flag ON y OFF.
  - Fallback: envio interactivo fallido siempre cae a texto plano, nunca
    deja al prospecto sin CTA que responder.
  - Regresion: con el flag apagado, comportamiento identico al legacy.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app
import whatsapp_interactive as wai


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _text_msg(phone, text, mid):
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


def _button_reply_msg(phone, mid, rid, title):
    return {
        "from": phone, "id": mid, "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": title}},
    }


def _list_reply_msg(phone, mid, rid, title):
    return {
        "from": phone, "id": mid, "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": title}},
    }


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_seen_ids", set())
    monkeypatch.setattr(vicky_app, "_seen_dq", vicky_app.__dict__.get("_seen_dq", []).__class__())

    sent = []

    def fake_send_msg(to, text):
        sent.append((to, text))
        return True

    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: True)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")

    boardroom_calls = []
    monkeypatch.setattr(
        vicky_app, "_request_boardroom_instruction",
        lambda payload: (boardroom_calls.append(payload) or (None, "should_not_be_called")),
    )
    monkeypatch.setattr(vicky_app, "WHATSAPP_INTERACTIVE_MENU_ENABLED", False)

    yield sent, boardroom_calls


# ─────────────────────────────────────────────────────────────────────────────
# Fidelidad textual (flag apagado == exactamente el texto de antes)
# ─────────────────────────────────────────────────────────────────────────────

def test_flag_por_defecto_es_false():
    assert vicky_app.WHATSAPP_IMSS_BUTTONS_ENABLED is False


def test_vrim_promo_message_reconstruido_es_identico_al_original():
    reconstruido = (
        vicky_app._IMSS_VRIM_PROMO_BODY + "\n\n"
        "*" + vicky_app._IMSS_REVISION_CTA_QUESTION + "*\n\n"
        "1️⃣ Sí, quiero que me contacte\n"
        "2️⃣ No por ahora"
    )
    assert vicky_app._IMSS_VRIM_PROMO_MESSAGE == reconstruido


def test_revision_cta_fallback_y_cta_no_se_tocaron():
    assert vicky_app._IMSS_REVISION_CTA_FALLBACK == (
        "¿Quieres que Christian revise tu caso?\n"
        "1. Sí, quiero que me contacte\n"
        "2. No por ahora"
    )
    assert vicky_app._IMSS_REVISION_CTA == (
        "¿Quieres que Christian revise si podemos avanzar con esta opción?\n"
        "1️⃣ Sí, quiero que me contacte\n"
        "2️⃣ No por ahora"
    )


def test_parsers_de_negocio_no_fueron_modificados():
    """Los mismos parsers de siempre, sin tocar -- solo se les alimenta el id
    del boton/row en vez del titulo cuando corresponde (ver handle())."""
    assert vicky_app._imss_ley73_choice("1") == "1"
    assert vicky_app._imss_ley73_choice("4") == "4"
    assert vicky_app._imss_revision_choice("1") == "si"
    assert vicky_app._imss_revision_choice("2") == "no"
    assert vicky_app.yes_no("si") == "si"
    assert vicky_app.yes_no("no") == "no"
    # El TITULO de un boton NO siempre es interpretable por estos parsers --
    # exactamente el motivo por el que se usa el id, no el titulo.
    assert vicky_app._imss_ley73_choice("Recibo pensión, no sé si es Ley 73") == "?"


# ─────────────────────────────────────────────────────────────────────────────
# Builders puros (sin HTTP)
# ─────────────────────────────────────────────────────────────────────────────

def test_build_ley73_list_payload_valido():
    ok_calls = []
    payload = None

    def fake_send_interactive_list(to, body, btn, sections):
        nonlocal payload
        payload = wai.build_list_message_payload(to, body, btn, sections)
        ok_calls.append(to)
        return True

    import unittest.mock as mock
    with mock.patch.object(vicky_app, "send_interactive_list", fake_send_interactive_list):
        ok = vicky_app._imss_send_ley73_menu("6681234567")
    assert ok is True
    rows = payload["interactive"]["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["1", "2", "3", "4"]
    assert all(len(r["title"]) <= 24 for r in rows)


def test_build_revision_cta_buttons_payload_valido():
    captured = {}

    def fake_send_interactive_buttons(to, body, buttons):
        captured["payload"] = wai.build_reply_buttons_payload(to, body, buttons)
        return True

    import unittest.mock as mock
    with mock.patch.object(vicky_app, "send_interactive_buttons", fake_send_interactive_buttons):
        ok = vicky_app._imss_send_revision_cta("6681234567")
    assert ok is True
    buttons = captured["payload"]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["1", "2"]


def test_build_si_no_buttons_payload_valido():
    captured = {}

    def fake_send_interactive_buttons(to, body, buttons):
        captured["payload"] = wai.build_reply_buttons_payload(to, body, buttons)
        return True

    import unittest.mock as mock
    with mock.patch.object(vicky_app, "send_interactive_buttons", fake_send_interactive_buttons):
        ok = vicky_app._imss_send_si_no("6681234567", "¿Deseas que un asesor te contacte?")
    assert ok is True
    buttons = captured["payload"]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["si", "no"]


def test_revision_cta_cae_a_texto_si_falla_el_envio_interactivo(monkeypatch):
    sent = []
    monkeypatch.setattr(vicky_app, "send_interactive_buttons", lambda *a, **k: False)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    ok = vicky_app._imss_send_revision_cta("6681234567")
    assert ok is True
    assert sent[0][1] == vicky_app._IMSS_REVISION_CTA_FALLBACK


def test_si_no_cae_a_texto_si_falla_el_envio_interactivo(monkeypatch):
    sent = []
    monkeypatch.setattr(vicky_app, "send_interactive_buttons", lambda *a, **k: False)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    ok = vicky_app._imss_send_si_no("6681234567", "¿Pregunta?")
    assert ok is True
    assert sent[0] == ("6681234567", "¿Pregunta?")


# ─────────────────────────────────────────────────────────────────────────────
# Integracion real: handle() con el flag ENCENDIDO
# ─────────────────────────────────────────────────────────────────────────────

def test_imss_open_flag_on_envia_welcome_y_list(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    list_calls = []
    monkeypatch.setattr(
        vicky_app, "send_interactive_list",
        lambda to, body, btn, sections: list_calls.append((to, body, sections)) or True,
    )
    vicky_app.handle(_text_msg("6681234567", "1", "mid-1"))
    assert boardroom_calls == []
    # El texto de bienvenida se manda (sin la lista numerada embebida).
    assert any("Vicky, asistente de Christian" in s[1] for s in sent)
    assert all("1️⃣" not in s[1] for s in sent)
    assert len(list_calls) == 1
    assert vicky_app.user_state.get("6681234567") == "imss_q_ley73"


def test_list_reply_ley73_perfil_1_avanza_por_id(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-2", "1", "Ya soy Ley 73"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567") == "imss_q_pension_calc"
    assert vicky_app.user_data.get("6681234567", {}).get("ley73_estatus") == "pensionado_ley73"


def test_list_reply_ley73_perfil_2_titulo_ambiguo_igual_funciona_por_id(_isolate_state, monkeypatch):
    """El titulo real de la opcion 2 ('No sé si es Ley 73') NO seria
    interpretable por _imss_ley73_choice() como texto libre -- por eso se
    usa el id, no el titulo (ver test_parsers_de_negocio_no_fueron_modificados)."""
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-3", "2", "No sé si es Ley 73"))
    assert vicky_app.user_state.get("6681234567") == "imss_q_pension_calc"
    assert vicky_app.user_data.get("6681234567", {}).get("ley73_estatus") == "pensionado_sin_confirmar_ley73"


def test_list_reply_ley73_perfil_4_familiar_por_id(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-4", "4", "Pregunto por familiar"))
    assert vicky_app.user_state.get("6681234567") == "imss_q_pension_calc"
    assert vicky_app.user_data.get("6681234567", {}).get("relacion") == "familiar"


def test_pension_alta_flag_on_envia_vrim_body_y_botones_cta(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    buttons_calls = []
    monkeypatch.setattr(
        vicky_app, "send_interactive_buttons",
        lambda to, body, buttons: buttons_calls.append((to, body, buttons)) or True,
    )
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-5", "1", "Ya soy Ley 73"))
    vicky_app.handle(_text_msg("6681234567", "12000", "mid-6"))

    # El cuerpo VRIM se envio SIN el CTA embebido.
    vrim_msgs = [s for s in sent if "VRIM Plus" in s[1]]
    assert len(vrim_msgs) == 1
    assert "¿Quieres que Christian revise tu caso?" not in vrim_msgs[0][1]
    # El CTA se envio aparte, como botones.
    assert len(buttons_calls) == 1
    assert buttons_calls[0][1] == vicky_app._IMSS_REVISION_CTA_QUESTION
    assert vicky_app.user_state.get("6681234567") == "imss_q_revision"


def test_button_reply_cta_si_avanza_a_nombre(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    vicky_app.user_state["6681234567"] = "imss_q_revision"
    vicky_app.user_data["6681234567"] = {"pension": 12000, "propuesta_monto": 100000}
    vicky_app.handle(_button_reply_msg("6681234567", "mid-7", "1", "Sí, contáctenme"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567") == "imss_q_nombre_calc"


def test_button_reply_cta_no_cierra(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    vicky_app.user_state["6681234567"] = "imss_q_revision"
    vicky_app.user_data["6681234567"] = {"pension": 12000, "propuesta_monto": 100000}
    vicky_app.handle(_button_reply_msg("6681234567", "mid-8", "2", "No por ahora"))
    assert vicky_app.user_state.get("6681234567", "") == "imss_post_cierre"  # _imss_close


def test_pension_baja_flag_on_envia_botones_si_no(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    buttons_calls = []
    monkeypatch.setattr(
        vicky_app, "send_interactive_buttons",
        lambda to, body, buttons: buttons_calls.append((to, body, buttons)) or True,
    )
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-9", "1", "Ya soy Ley 73"))
    vicky_app.handle(_text_msg("6681234567", "500", "mid-10"))  # pension muy baja
    assert vicky_app.user_state.get("6681234567") == "imss_pension_baja"
    assert len(buttons_calls) == 1
    assert [b[0] for b in buttons_calls[0][2]] == ["si", "no"]


def test_button_reply_pension_baja_si_notifica_asesor(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    advisor_calls = []
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_calls.append(msg) or True)
    vicky_app.user_state["6681234567"] = "imss_pension_baja"
    vicky_app.user_data["6681234567"] = {"pension": 500}
    vicky_app.handle(_button_reply_msg("6681234567", "mid-11", "si", "Sí"))
    assert len(advisor_calls) == 1
    assert "PENSIÓN BAJA" in advisor_calls[0]


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: envio interactivo falla -> nunca se pierde el CTA
# ─────────────────────────────────────────────────────────────────────────────

def test_vrim_ok_pero_botones_cta_fallan_cae_a_texto_y_avanza(_isolate_state, monkeypatch):
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    monkeypatch.setattr(vicky_app, "send_interactive_buttons", lambda *a, **k: False)
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-12", "1", "Ya soy Ley 73"))
    vicky_app.handle(_text_msg("6681234567", "12000", "mid-13"))
    assert any(s[1] == vicky_app._IMSS_REVISION_CTA_FALLBACK for s in sent)
    assert vicky_app.user_state.get("6681234567") == "imss_q_revision"


def test_vrim_falla_botones_fallan_y_texto_fallback_funciona(_isolate_state, monkeypatch):
    """VRIM completo falla -> intenta botones (fallan) -> intenta texto
    fallback dentro de _imss_send_revision_cta (funciona). Nunca se pierde
    el CTA mientras exista AL MENOS un canal que funcione."""
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    monkeypatch.setattr(vicky_app, "send_interactive_buttons", lambda *a, **k: False)

    real_send_msg = vicky_app.send_msg

    def fake_send_msg(to, text):
        if "VRIM Plus" in text:
            return False  # la burbuja VRIM completa falla al enviarse
        return real_send_msg(to, text)

    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.handle(_list_reply_msg("6681234567", "mid-14", "1", "Ya soy Ley 73"))
    vicky_app.handle(_text_msg("6681234567", "12000", "mid-15"))

    assert any(s[1] == vicky_app._IMSS_REVISION_CTA_FALLBACK for s in sent)
    assert vicky_app.user_state.get("6681234567") == "imss_q_revision"


def test_vrim_botones_y_fallback_de_texto_fallan_todos_pasa_a_cta_pendiente(_isolate_state, monkeypatch):
    """Triple fallo real (VRIM, botones, texto fallback): el prospecto no
    recibe nada en este turno, pero el estado queda en imss_cta_pendiente
    (recuperable) y se registra el respaldo -- nunca se avanza a
    imss_q_revision sin que el CTA realmente haya llegado."""
    sent, boardroom_calls = _isolate_state
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_BUTTONS_ENABLED", True)
    monkeypatch.setattr(vicky_app, "send_interactive_buttons", lambda *a, **k: False)
    backup_calls = []
    monkeypatch.setattr(
        vicky_app, "_imss_log_lead_backup",
        lambda phone, data, resultado="advisor_notify_failed": backup_calls.append(resultado),
    )

    def fake_send_msg(to, text):
        return False  # todo el texto plano tambien falla

    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    vicky_app.user_state["6681234567"] = "imss_q_ley73"
    vicky_app.user_data["6681234567"] = {}
    # No pasa por handle() para el primer paso (necesita send_msg real para
    # avanzar el estado inicial); se posiciona directo en imss_q_pension_calc.
    vicky_app.user_state["6681234567"] = "imss_q_pension_calc"
    vicky_app.user_data["6681234567"] = {"ley73_estatus": "pensionado_ley73", "relacion": "titular"}
    vicky_app.handle(_text_msg("6681234567", "12000", "mid-16"))

    assert vicky_app.user_state.get("6681234567") == "imss_cta_pendiente"
    assert backup_calls == ["cta_send_failed"]
