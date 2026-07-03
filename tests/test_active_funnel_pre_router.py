"""
Pre-router local de estado activo: si el usuario esta a mitad de un funnel
local (imss_/auto_/vida_/vrim_/emp_/fp_), la respuesta debe continuar ESE
funnel, nunca ir a Boardroom.

Antes de este fix: tras iniciar un funnel local (ej. opcion 6 -> fp_start),
la siguiente respuesta del usuario (ej. "si") no coincidia con ningun menu
trigger ni opcion numerica exacta, asi que caia en
_handle_boardroom_authority(). El contrato canonico de Boardroom para Vicky
hoy es Fase 1 (siempre responde con el mensaje neutral generico, ver
audit.decision_reason=phase_1_safe_response_no_commercial_decision en
boardroom-engine), asi que la respuesta de continuacion de funnel terminaba
mostrando el fallback "Recibi tu mensaje..." en vez de avanzar la
conversacion.
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


# ── Escenario completo: opcion 6 -> "si" -> continua el funnel fp ──────────

def test_option_6_starts_fp_funnel(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567") == "fp_q_interes"
    assert "Financiamiento Práctico Empresarial" in sent[0][1]


def test_si_after_option_6_continues_funnel_fp(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6"))
    vicky_app.handle(_text_msg("6681234567", "si", "mid-si"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567") == "fp_q1"


def test_si_does_not_call_boardroom_when_state_is_fp_q_interes(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.user_state["6681234567"] = "fp_q_interes"
    vicky_app.handle(_text_msg("6681234567", "si", "mid-si2"))
    assert boardroom_calls == []


def test_si_after_option_6_sends_giro_question(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6"))
    vicky_app.handle(_text_msg("6681234567", "si", "mid-si"))
    assert len(sent) == 2
    assert "giro de tu empresa" in sent[1][1]


def test_si_after_option_6_no_neutral_fallback(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6"))
    vicky_app.handle(_text_msg("6681234567", "si", "mid-si"))
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


def test_fp_product_code_remains_credito_empresarial_sin_garantia():
    assert vicky_app._service_to_product_code("fp") == "credito_empresarial_sin_garantia"
    assert vicky_app._service_to_product_code("fp") != "nomina_empresarial"


# ── Otros funnels activos: continuan localmente antes de Boardroom ────────

def test_imss_active_state_continues_before_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.user_state["6681234567"] = "imss_q_pension"
    vicky_app.user_data["6681234567"] = {}
    vicky_app.handle(_text_msg("6681234567", "7500", "mid-imss"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681234567", "").startswith("imss_")


def test_auto_active_state_continues_before_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "2", "mid-2"))
    boardroom_calls.clear()
    sent.clear()
    vicky_app.handle(_text_msg("6681234567", "si", "mid-auto-si"))
    assert boardroom_calls == []


def test_vida_active_state_continues_before_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "3", "mid-3"))
    boardroom_calls.clear()
    sent.clear()
    vicky_app.handle(_text_msg("6681234567", "si", "mid-vida-si"))
    assert boardroom_calls == []


def test_vrim_active_state_continues_before_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "4", "mid-4"))
    boardroom_calls.clear()
    sent.clear()
    vicky_app.handle(_text_msg("6681234567", "si", "mid-vrim-si"))
    assert boardroom_calls == []


def test_emp_active_state_continues_before_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "5", "mid-5"))
    boardroom_calls.clear()
    sent.clear()
    vicky_app.handle(_text_msg("6681234567", "si", "mid-emp-si"))
    assert boardroom_calls == []


# ── Regresion: menu, opciones, mensajes libres, no duplicados ─────────────

def test_menu_still_works_without_active_state(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-menu"))
    assert boardroom_calls == []
    assert "Servicios Financieros Inbursa" in sent[0][1]


def test_option_6_still_returns_fp_prompt(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6b"))
    assert boardroom_calls == []
    assert "Financiamiento Práctico Empresarial" in sent[0][1]


def test_free_form_without_active_state_still_calls_boardroom(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "hola quiero saber sobre mis opciones de inversion", "mid-free"))
    assert len(boardroom_calls) == 1


def test_menu_while_active_funnel_feeds_funnel_not_menu(monkeypatch):
    """Comportamiento heredado del handle() original (pre-Boardroom-authority):
    el estado activo tiene prioridad sobre el trigger de menu. No es una
    regresion nueva de este fix, es restaurar el orden que ya existia."""
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6c"))
    vicky_app.handle(_text_msg("6681234567", "menu", "mid-menu-mid-funnel"))
    assert boardroom_calls == []
    assert "Servicios Financieros Inbursa" not in sent[-1][1]


def test_no_duplicate_whatsapp_responses_in_funnel_sequence(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681234567", "6", "mid-6d"))
    vicky_app.handle(_text_msg("6681234567", "si", "mid-si-d"))
    assert len(sent) == 2
