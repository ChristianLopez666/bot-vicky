"""
P2 — notify_advisor fuera de ventana 24h.

Meta rechaza parámetros de template con saltos de línea, tabs o >4 espacios
consecutivos (error 132000/132012). Todos los mensajes al asesor son
multilínea, así que el nivel 2 (template) fallaba SIEMPRE fuera de ventana
24h: el lead llegaba, Vicky respondía, y Don Chiwy no se enteraba. Estos
tests protegen el sanitizador del parámetro y la escalera texto-libre →
template de notify_advisor.

Cero I/O real: _wa_post está mockeado en todos los tests; no se envía ningún
mensaje de WhatsApp.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


MULTILINE_MSG = (
    "NUEVO LEAD — CONSIGUE TU CRÉDITO\n\n"
    "Producto: Crédito empresarial sin garantía\n"
    "Campaña: CTC julio 2026\n"
    "Estado: Pendiente de calificación\n\n"
    "Nombre: Juan Prueba\n"
    "WhatsApp: 5216681234567\n"
    "Monto solicitado: 500000\n\n"
    "Resumen: Lead interesado.\tRequiere revisión manual."
)


class FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _patch_notify_env(monkeypatch, wa_responses, template_name="asesor_lead_v1"):
    """Mockea _wa_post con una cola de respuestas y aisla _log/env del asesor."""
    calls = []

    def fake_wa_post(payload):
        calls.append(payload)
        return wa_responses[min(len(calls) - 1, len(wa_responses) - 1)]

    monkeypatch.setattr(vicky_app, "_wa_post", fake_wa_post)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "ADVISOR_NUM", "5216682478005")
    monkeypatch.setattr(vicky_app, "ADV_TPL", template_name)
    monkeypatch.setattr(vicky_app, "ADV_TPL_LANG", "es_MX")
    return calls


# ── _sanitize_template_param (helper puro) ────────────────────────────────────

def test_sanitize_collapses_newlines_and_tabs_to_single_line():
    out = vicky_app._sanitize_template_param(MULTILINE_MSG)
    assert "\n" not in out
    assert "\t" not in out
    assert "  " not in out
    assert "NUEVO LEAD — CONSIGUE TU CRÉDITO Producto:" in out


def test_sanitize_preserves_accents_and_emojis():
    out = vicky_app._sanitize_template_param("🔔 Préstamo IMSS\nPensión: $12,000 😊")
    assert out == "🔔 Préstamo IMSS Pensión: $12,000 😊"


def test_sanitize_truncates_to_1024():
    out = vicky_app._sanitize_template_param("x" * 5000)
    assert len(out) == 1024


def test_sanitize_respects_custom_limit():
    out = vicky_app._sanitize_template_param("palabra " * 100, limit=50)
    assert len(out) <= 50


def test_sanitize_empty_and_whitespace_use_safe_fallback():
    assert vicky_app._sanitize_template_param("") == vicky_app._TPL_PARAM_FALLBACK
    assert vicky_app._sanitize_template_param("\n\n\t  \n") == vicky_app._TPL_PARAM_FALLBACK
    assert vicky_app._sanitize_template_param(None) == vicky_app._TPL_PARAM_FALLBACK


def test_sanitize_strips_control_characters():
    out = vicky_app._sanitize_template_param("Lead\x00nuevo\x1b[0m listo")
    assert "\x00" not in out and "\x1b" not in out


# ── notify_advisor: escalera texto libre → template ───────────────────────────

def test_free_text_ok_returns_true_and_never_tries_template(monkeypatch):
    calls = _patch_notify_env(monkeypatch, [FakeResp(200)])
    assert vicky_app.notify_advisor(MULTILINE_MSG) is True
    assert len(calls) == 1
    assert calls[0]["type"] == "text"
    # El texto libre conserva el mensaje crudo multilínea (comportamiento intacto).
    assert calls[0]["text"]["body"] == MULTILINE_MSG


def test_free_text_fails_template_gets_sanitized_param(monkeypatch):
    calls = _patch_notify_env(
        monkeypatch,
        [FakeResp(400, '{"error":{"code":131047}}'), FakeResp(200)],
    )
    assert vicky_app.notify_advisor(MULTILINE_MSG) is True
    assert len(calls) == 2
    assert calls[1]["type"] == "template"
    param = calls[1]["template"]["components"][0]["parameters"][0]["text"]
    assert "\n" not in param and "\t" not in param
    assert len(param) <= 1024
    assert param == vicky_app._sanitize_template_param(MULTILINE_MSG)


def test_free_text_fails_template_201_returns_true(monkeypatch):
    calls = _patch_notify_env(monkeypatch, [FakeResp(500), FakeResp(201)])
    assert vicky_app.notify_advisor("Lead\nnuevo") is True
    assert len(calls) == 2


def test_free_text_fails_no_template_returns_false_single_call(monkeypatch):
    calls = _patch_notify_env(monkeypatch, [FakeResp(400)], template_name="")
    assert vicky_app.notify_advisor(MULTILINE_MSG) is False
    assert len(calls) == 1


def test_free_text_fails_and_template_fails_returns_false(monkeypatch):
    calls = _patch_notify_env(
        monkeypatch,
        [FakeResp(400), FakeResp(400, '{"error":{"code":132012}}')],
    )
    assert vicky_app.notify_advisor(MULTILINE_MSG) is False
    assert len(calls) == 2


def test_no_advisor_number_returns_false_without_calls(monkeypatch):
    calls = _patch_notify_env(monkeypatch, [FakeResp(200)])
    monkeypatch.setattr(vicky_app, "ADVISOR_NUM", "")
    assert vicky_app.notify_advisor("hola") is False
    assert calls == []
