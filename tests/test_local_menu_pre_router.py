"""
Pre-router local de UX (menu, opciones 1-6) + fix de _service_to_product_code
para la opcion 6 (Financiamiento Practico / Consigue Tu Credito).

Antes de este fix, handle() mandaba TODO mensaje -- incluido "menu" y "1".."6"
-- directo a _handle_boardroom_authority() cuando BOARDROOM_IS_AUTHORITY es
True. Si Boardroom fallaba (ej. http_401), el usuario recibia el fallback
neutral "Recibi tu mensaje..." incluso para comandos de UX pura sin relacion
con la autoridad comercial de Boardroom.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _text_msg(phone: str, text: str, mid: str) -> dict:
    return {
        "from": phone,
        "id": mid,
        "type": "text",
        "text": {"body": text},
    }


def _base_patches(monkeypatch):
    """Aisla handle() de I/O real: WhatsApp, Sheets, notificaciones, Boardroom HTTP."""
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


# ── Menu local: nunca debe llamar a Boardroom ──────────────────────────────

def test_menu_returns_menu_locally(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-1"))
    assert boardroom_calls == []
    assert len(sent) == 1
    assert "Servicios Financieros Inbursa" in sent[0][1]


def test_inicio_returns_menu_locally(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "inicio", "mid-2"))
    assert boardroom_calls == []
    assert len(sent) == 1
    assert "Servicios Financieros Inbursa" in sent[0][1]


def test_opciones_returns_menu_locally(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "opciones", "mid-3"))
    assert boardroom_calls == []
    assert len(sent) == 1
    assert "Servicios Financieros Inbursa" in sent[0][1]


def test_local_menu_commands_never_call_boardroom(monkeypatch):
    for trigger in ("menu", "memu", "inicio", "start", "servicios", "opciones",
                     "catalogo", "productos", "ver menu", "mostrar menu"):
        sent, boardroom_calls = _base_patches(monkeypatch)
        vicky_app.handle(_text_msg("6681234567", trigger, f"mid-{trigger}"))
        assert boardroom_calls == [], f"trigger {trigger!r} llamo a Boardroom"


# ── Opciones numericas 1-6: nunca deben caer en el fallback neutral ────────

def test_option_1_does_not_fallback(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "1", "mid-opt1"))
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
    assert vicky_app.user_state.get("6681234567", "").startswith("imss_")


def test_option_6_does_not_fallback(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-opt6"))
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
    assert vicky_app.user_state.get("6681234567", "").startswith("fp_")


def test_option_6_does_not_map_to_nomina_empresarial():
    assert vicky_app._service_to_product_code("fp") != "nomina_empresarial"


def test_option_6_maps_to_credito_empresarial_sin_garantia():
    assert vicky_app._service_to_product_code("fp") == "credito_empresarial_sin_garantia"


def test_option_6_starts_correct_fp_flow(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-opt6-flow"))
    assert boardroom_calls == []
    assert len(sent) == 1
    assert "Consigue Tu Crédito" in sent[0][1]
    assert "crédito empresarial sin garantía" in sent[0][1]


# ── Boardroom sigue siendo la autoridad para mensajes libres ───────────────

def test_free_form_non_local_message_still_calls_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "hola quiero saber sobre mis opciones de inversion", "mid-free"))
    assert len(boardroom_calls) == 1


def test_boardroom_http_401_fallback_stays_safe(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)

    def fake_401(payload):
        boardroom_calls.append(payload)
        return None, "http_401"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", fake_401)
    vicky_app.handle(_text_msg("6681234567", "hola quiero saber sobre mis opciones de inversion", "mid-401"))
    assert len(boardroom_calls) == 1
    assert len(sent) == 1
    assert sent[0][1] == vicky_app.NEUTRAL_FALLBACK_MESSAGE


def test_no_duplicate_whatsapp_responses(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-dup"))
    assert len(sent) == 1


# ── Regresion: servicios 1-5 (y 6) siguen funcionando sin caer en Boardroom ─

def test_regression_imss_option_1(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "1", "mid-r1"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("imss_")


def test_regression_auto_option_2(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "2", "mid-r2"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("auto_")


def test_regression_vida_option_3(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "3", "mid-r3"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("vida_")


def test_regression_vrim_option_4(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "4", "mid-r4"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("vrim_")


def test_regression_financiamiento_empresarial_option_5(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "5", "mid-r5"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("emp_")


def test_regression_ctc_financiamiento_practico_option_6(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-r6"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("fp_")
