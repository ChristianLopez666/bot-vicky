"""WA-1: Interactive Messages foundation (reply buttons / list message).

Cubre:
  - whatsapp_interactive.py aislado (parser + builders + puente id->svc),
    sin Flask ni HTTP -- pruebas puras.
  - Integracion real en app.handle(): payload webhook simulado -> parser real
    -> normalizacion -> pre-router real -> funnel/route real, sin llamar a
    Meta ni mockear el router (Fase 15: al menos una prueba debe recorrer el
    camino completo, no solo builders aislados).
  - Regresion: texto legacy (incluye TPV/IMSS) y el menu por defecto no
    cambian de comportamiento con el feature flag apagado.

Cero I/O real: `_wa_post`/`_log`/`notify_advisor` mockeados donde aplica; no
se envia ningun mensaje de WhatsApp.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app
import whatsapp_interactive as wai


# ─────────────────────────────────────────────────────────────────────────────
# 1-7: parser puro (whatsapp_interactive.normalize_incoming_message)
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_text_legacy():
    msg = {"from": "6681234567", "id": "mid-1", "type": "text",
           "text": {"body": "hola"}}
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_TEXT
    assert event["text"] == "hola"
    assert event["interactive_id"] is None


def test_parse_button_reply_valido():
    msg = {
        "from": "6681234567", "id": "mid-2", "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "menu_imss", "title": "Préstamo IMSS"},
        },
    }
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_BUTTON_REPLY
    assert event["interactive_id"] == "menu_imss"
    assert event["interactive_title"] == "Préstamo IMSS"
    assert event["text"] == "Préstamo IMSS"
    assert event["raw_type"] == "interactive:button_reply"


def test_parse_list_reply_valido():
    msg = {
        "from": "6681234567", "id": "mid-3", "type": "interactive",
        "interactive": {
            "type": "list_reply",
            "list_reply": {
                "id": "menu_auto",
                "title": "Seguro de Auto",
                "description": "Cobertura amplia",
            },
        },
    }
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_LIST_REPLY
    assert event["interactive_id"] == "menu_auto"
    assert event["interactive_title"] == "Seguro de Auto"
    assert event["interactive_description"] == "Cobertura amplia"
    assert event["text"] == "Seguro de Auto"


def test_parse_nfm_reply_reconocido_no_procesado():
    msg = {
        "from": "6681234567", "id": "mid-4", "type": "interactive",
        "interactive": {
            "type": "nfm_reply",
            "nfm_reply": {"response_json": "{\"algo\":\"algo\"}", "name": "flow"},
        },
    }
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_NFM_REPLY
    # No se toca response_json: el contrato normalizado no lo expone.
    assert "response_json" not in event
    assert event["interactive_id"] is None


def test_parse_interactive_type_desconocido():
    msg = {
        "from": "6681234567", "id": "mid-5", "type": "interactive",
        "interactive": {"type": "algo_futuro_no_soportado"},
    }
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_INTERACTIVE_UNKNOWN


def test_parse_button_reply_sin_id():
    msg = {
        "from": "6681234567", "id": "mid-6", "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"title": "Sin id"},
        },
    }
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_INTERACTIVE_UNKNOWN


def test_parse_list_reply_sin_id():
    msg = {
        "from": "6681234567", "id": "mid-7", "type": "interactive",
        "interactive": {
            "type": "list_reply",
            "list_reply": {"title": "Sin id"},
        },
    }
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_INTERACTIVE_UNKNOWN


def test_parse_legacy_template_button_no_se_confunde_con_interactive():
    """type:"button" (quick-reply de plantilla) es un contrato distinto de
    interactive.button_reply -- no deben mezclarse (Fase 3 del prompt)."""
    msg = {"from": "6681234567", "id": "mid-8", "type": "button",
           "button": {"text": "Sí", "payload": "SI"}}
    event = wai.normalize_incoming_message(msg)
    assert event["kind"] == wai.KIND_LEGACY_TEMPLATE_BUTTON
    assert event["kind"] != wai.KIND_BUTTON_REPLY


# ─────────────────────────────────────────────────────────────────────────────
# 8-10: build reply buttons
# ─────────────────────────────────────────────────────────────────────────────

def test_build_1_reply_button():
    payload = wai.build_reply_buttons_payload(
        "6681234567", "¿Continuamos?", [("si", "Sí")]
    )
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "button"
    buttons = payload["interactive"]["action"]["buttons"]
    assert len(buttons) == 1
    assert buttons[0] == {"type": "reply", "reply": {"id": "si", "title": "Sí"}}


def test_build_3_reply_buttons():
    payload = wai.build_reply_buttons_payload(
        "6681234567", "Elige uno", [("a", "Opción A"), ("b", "Opción B"), ("c", "Opción C")]
    )
    assert len(payload["interactive"]["action"]["buttons"]) == 3


def test_rechaza_mas_de_3_reply_buttons():
    with pytest.raises(ValueError):
        wai.build_reply_buttons_payload(
            "6681234567", "Elige uno",
            [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")],
        )


def test_build_reply_buttons_id_duplicado_rechazado():
    with pytest.raises(ValueError):
        wai.build_reply_buttons_payload(
            "6681234567", "Elige uno", [("a", "Uno"), ("a", "Otro")]
        )


def test_build_reply_buttons_title_excede_limite_rechazado():
    with pytest.raises(ValueError):
        wai.build_reply_buttons_payload(
            "6681234567", "Elige uno", [("a", "T" * 21)]
        )


# ─────────────────────────────────────────────────────────────────────────────
# 11-12: build list message
# ─────────────────────────────────────────────────────────────────────────────

def test_build_list_con_una_seccion():
    payload = wai.build_list_message_payload(
        "6681234567", "Elige un servicio", "Ver opciones",
        [{"title": "Servicios", "rows": [{"id": "menu_imss", "title": "IMSS"}]}],
    )
    assert payload["interactive"]["type"] == "list"
    action = payload["interactive"]["action"]
    assert action["button"] == "Ver opciones"
    assert len(action["sections"]) == 1
    assert action["sections"][0]["rows"] == [{"id": "menu_imss", "title": "IMSS"}]


def test_build_list_con_varias_rows_y_secciones():
    payload = wai.build_list_message_payload(
        "6681234567", "Elige", "Ver todo",
        [
            {"title": "Pensiones", "rows": [
                {"id": "menu_imss", "title": "IMSS", "description": "Ley 73"},
            ]},
            {"title": "Seguros", "rows": [
                {"id": "menu_auto", "title": "Auto"},
                {"id": "menu_vida", "title": "Vida", "description": "GMM"},
            ]},
        ],
    )
    sections = payload["interactive"]["action"]["sections"]
    assert len(sections) == 2
    assert sum(len(s["rows"]) for s in sections) == 3
    assert sections[0]["rows"][0]["description"] == "Ley 73"


def test_build_list_rechaza_mas_de_10_rows_totales():
    rows = [{"id": f"r{i}", "title": f"Row {i}"} for i in range(11)]
    with pytest.raises(ValueError):
        wai.build_list_message_payload(
            "6681234567", "Elige", "Ver",
            [{"title": "Muchas", "rows": rows}],
        )


def test_build_list_row_id_duplicado_rechazado():
    with pytest.raises(ValueError):
        wai.build_list_message_payload(
            "6681234567", "Elige", "Ver",
            [{"title": "S", "rows": [
                {"id": "x", "title": "Uno"}, {"id": "x", "title": "Otro"},
            ]}],
        )


# ─────────────────────────────────────────────────────────────────────────────
# 13-14: puente interactive_id -> servicio canonico
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("interactive_id,expected_svc", [
    ("menu_imss", "imss"),
    ("menu_auto", "auto"),
    ("menu_vida", "vida"),
    ("menu_vrim", "vrim"),
    ("menu_emp", "emp"),
    ("menu_fp", "fp"),
])
def test_interactive_id_resuelve_a_intent_existente(interactive_id, expected_svc):
    assert wai.resolve_service_from_interactive_id(interactive_id) == expected_svc


def test_interactive_id_desconocido_devuelve_none_fallback_seguro():
    assert wai.resolve_service_from_interactive_id("menu_no_existe") is None
    assert wai.resolve_service_from_interactive_id(None) is None
    assert wai.resolve_service_from_interactive_id("") is None


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag: parse_bool_flag (15-17) — puro, sin reload de modulo
# ─────────────────────────────────────────────────────────────────────────────

def test_flag_ausente_es_legacy():
    value, invalid = wai.parse_bool_flag(None)
    assert value is False
    assert invalid is False


def test_flag_false_es_legacy():
    value, invalid = wai.parse_bool_flag("false")
    assert value is False
    assert invalid is False


def test_flag_true_activa_interactive_menu():
    value, invalid = wai.parse_bool_flag("true")
    assert value is True
    assert invalid is False


def test_flag_valor_invalido_cae_a_false_con_aviso():
    value, invalid = wai.parse_bool_flag("tal-vez")
    assert value is False
    assert invalid is True


def test_flag_por_defecto_en_app_es_false():
    """El flag ya cargado en app.py (sin WHATSAPP_INTERACTIVE_MENU_ENABLED en
    el entorno de test) debe ser false -- LEGACY_TEXT_MENU por default."""
    assert vicky_app.WHATSAPP_INTERACTIVE_MENU_ENABLED is False


# ─────────────────────────────────────────────────────────────────────────────
# Integracion end-to-end real: webhook -> handle() -> parser -> router -> funnel
# (sin mockear el router; solo se aisla I/O externo real)
# ─────────────────────────────────────────────────────────────────────────────

class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _interactive_msg(phone, mid, kind, interactive_id, title, description=None):
    interactive = {"type": kind, kind: {"id": interactive_id, "title": title}}
    if description is not None:
        interactive[kind]["description"] = description
    return {"from": phone, "id": mid, "type": "interactive", "interactive": interactive}


def _text_msg(phone, text, mid):
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


def _base_patches(monkeypatch):
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

    def fake_request_boardroom_instruction(payload):
        boardroom_calls.append(payload)
        return None, "should_not_be_called"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", fake_request_boardroom_instruction)

    return sent, boardroom_calls


def test_button_reply_menu_imss_llega_al_funnel_real_sin_llamar_boardroom(monkeypatch):
    """Recorrido completo (Fase 15): payload interactive real -> handle() real
    -> normalize_incoming_message real -> resolve_service_from_interactive_id
    real -> route() real -> funnel_imss real. Nada mockeado salvo I/O externo."""
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_interactive_msg(
        "6681234567", "mid-int-1", "button_reply", "menu_imss", "Préstamo IMSS",
    ))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("imss_")
    assert len(sent) == 1


def test_list_reply_menu_auto_llega_al_funnel_real(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_interactive_msg(
        "6681234567", "mid-int-2", "list_reply", "menu_auto", "Seguro de Auto",
        description="Cobertura amplia",
    ))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("auto_")


def test_interactive_id_desconocido_cae_a_fallback_por_titulo(monkeypatch):
    """ID no mapeado: no debe crashear ni ir a Boardroom directo -- usa el
    titulo visible como si fuera texto libre, igual que hoy con texto
    normal, y termina en el mismo camino que "hola" (menu local)."""
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_interactive_msg(
        "6681234567", "mid-int-3", "button_reply", "id_no_mapeado", "hola",
    ))
    assert boardroom_calls == []
    assert "Servicios Financieros Inbursa" in sent[0][1]


def test_nfm_reply_no_crashea_no_responde_y_se_registra(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    msg = {
        "from": "6681234567", "id": "mid-int-4", "type": "interactive",
        "interactive": {"type": "nfm_reply", "nfm_reply": {"response_json": "{}"}},
    }
    vicky_app.handle(msg)  # no debe lanzar excepcion
    assert boardroom_calls == []
    assert sent == []  # no se envia nada al usuario todavia (fuera de alcance WA-1)
    assert vicky_app.user_state.get("6681234567", "") == ""


def test_interactive_type_desconocido_no_crashea(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    msg = {
        "from": "6681234567", "id": "mid-int-5", "type": "interactive",
        "interactive": {"type": "algo_nuevo_no_soportado"},
    }
    vicky_app.handle(msg)  # no debe lanzar excepcion


def test_flag_true_usa_menu_interactivo_en_lugar_del_legacy(monkeypatch):
    _base_patches(monkeypatch)
    sent_interactive = []
    monkeypatch.setattr(vicky_app, "WHATSAPP_INTERACTIVE_MENU_ENABLED", True)
    monkeypatch.setattr(
        vicky_app, "show_menu_interactive",
        lambda phone: sent_interactive.append(phone) or True,
    )
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-flag-true"))
    assert sent_interactive == ["6681234567"]


def test_flag_false_sigue_usando_menu_legacy_por_defecto(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    assert vicky_app.WHATSAPP_INTERACTIVE_MENU_ENABLED is False
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-flag-false"))
    assert "Servicios Financieros Inbursa" in sent[0][1]


# ─────────────────────────────────────────────────────────────────────────────
# 18-20: regresion explicita de texto legacy (no debe cambiar con WA-1)
# ─────────────────────────────────────────────────────────────────────────────

def test_texto_tpv_sigue_funcionando(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-tpv"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("fp_")


def test_texto_imss_sigue_funcionando(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "1", "mid-imss"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("imss_")


def test_menu_legacy_sigue_exactamente_operativo(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-menu-legacy"))
    assert boardroom_calls == []
    assert len(sent) == 1
    assert sent[0][1] == vicky_app._MENU


# ─────────────────────────────────────────────────────────────────────────────
# Builders del menu interactivo real (Fase 10) -- sin HTTP
# ─────────────────────────────────────────────────────────────────────────────

def test_build_main_menu_interactive_payload_tiene_las_6_opciones_reales():
    payload = vicky_app.build_main_menu_interactive_payload("6681234567")
    rows = payload["interactive"]["action"]["sections"][0]["rows"]
    ids = {r["id"] for r in rows}
    assert ids == {"menu_imss", "menu_auto", "menu_vida", "menu_vrim", "menu_emp", "menu_fp"}
    assert len(rows) == 6


def test_show_menu_interactive_llama_al_sender_interactivo(monkeypatch):
    calls = []
    monkeypatch.setattr(
        vicky_app, "send_interactive_list",
        lambda to, body, btn, sections: calls.append((to, body, btn, sections)) or True,
    )
    ok = vicky_app.show_menu_interactive("6681234567")
    assert ok is True
    assert len(calls) == 1
    assert calls[0][0] == "6681234567"


# ─────────────────────────────────────────────────────────────────────────────
# Senders (send_interactive_buttons / send_interactive_list) -- _wa_post mockeado
# ─────────────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_send_interactive_buttons_ok(monkeypatch):
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "META_TOKEN", "fake-token")
    monkeypatch.setattr(vicky_app, "WABA_ID", "fake-waba")
    calls = []
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: calls.append(payload) or FakeResp(200))
    ok = vicky_app.send_interactive_buttons("6681234567", "¿Continuamos?", [("si", "Sí"), ("no", "No")])
    assert ok is True
    assert calls[0]["interactive"]["type"] == "button"


def test_send_interactive_buttons_rechaza_mas_de_3_sin_llamar_http(monkeypatch):
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "META_TOKEN", "fake-token")
    monkeypatch.setattr(vicky_app, "WABA_ID", "fake-waba")
    calls = []
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: calls.append(payload) or FakeResp(200))
    ok = vicky_app.send_interactive_buttons(
        "6681234567", "Elige", [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
    )
    assert ok is False
    assert calls == []  # nunca debe llamar a Meta con un payload invalido


def test_send_interactive_list_ok(monkeypatch):
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "META_TOKEN", "fake-token")
    monkeypatch.setattr(vicky_app, "WABA_ID", "fake-waba")
    calls = []
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: calls.append(payload) or FakeResp(200))
    ok = vicky_app.send_interactive_list(
        "6681234567", "Elige", "Ver opciones",
        [{"title": "S", "rows": [{"id": "menu_imss", "title": "IMSS"}]}],
    )
    assert ok is True
    assert calls[0]["interactive"]["type"] == "list"


def test_send_interactive_list_meta_error_no_crashea(monkeypatch):
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "META_TOKEN", "fake-token")
    monkeypatch.setattr(vicky_app, "WABA_ID", "fake-waba")
    monkeypatch.setattr(vicky_app, "_wa_post", lambda payload: FakeResp(500, "server error"))
    ok = vicky_app.send_interactive_list(
        "6681234567", "Elige", "Ver",
        [{"title": "S", "rows": [{"id": "menu_imss", "title": "IMSS"}]}],
    )
    assert ok is False
