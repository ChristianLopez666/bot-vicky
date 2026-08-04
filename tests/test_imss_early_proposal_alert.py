"""
Punto 5 (alerta temprana) 2026-08-04: el asesor solo se enteraba de un lead
IMSS calificado hasta que aceptaba la revision y daba nombre+ciudad
(imss_q_ciudad_calc). Un lead con propuesta real (ej. $90,226, caso real
5216681693152) que nunca contesta esa pregunta quedaba invisible. Agrega una
alerta adicional en cuanto se calcula la propuesta, antes de saber si el
prospecto acepta revision.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


def _text_msg(phone: str, text: str, mid: str) -> dict:
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _base_patches(monkeypatch):
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_seen_ids", set())
    monkeypatch.setattr(vicky_app, "_seen_dq", vicky_app.__dict__.get("_seen_dq", []).__class__())

    sent = []
    advisor_msgs = []

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")
    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", lambda payload: (None, "n/a"))

    return sent, advisor_msgs


def test_early_alert_fires_right_after_proposal_is_calculated(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6683334444"

    vicky_app.handle(_text_msg(phone, "1", "m1"))       # menu -> filtro Ley73
    vicky_app.handle(_text_msg(phone, "1", "m2"))       # Ley73 = si -> pide pension
    assert advisor_msgs == []

    vicky_app.handle(_text_msg(phone, "9000", "m3"))    # pension -> propuesta calculada

    assert len(advisor_msgs) == 1
    alert = advisor_msgs[0]
    assert "PROPUESTA CALCULADA" in alert
    assert phone in alert
    assert "pendiente de confirmar" in alert.lower()
    # Todavia no llego a nombre/ciudad -- confirma que es una alerta previa,
    # no la de calificacion completa.
    assert vicky_app.user_state.get(phone) in ("imss_q_revision", "imss_cta_pendiente")


def test_early_alert_does_not_fire_below_minimum_amount(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6683334444"

    vicky_app.handle(_text_msg(phone, "1", "m1"))
    vicky_app.handle(_text_msg(phone, "1", "m2"))
    vicky_app.handle(_text_msg(phone, "500", "m3"))     # pension muy baja -> no califica

    assert advisor_msgs == []
    assert vicky_app.user_state.get(phone) == "imss_pension_baja"


def test_early_alert_plus_full_qualification_alert_are_both_sent(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6683334444"

    vicky_app.handle(_text_msg(phone, "1", "m1"))
    vicky_app.handle(_text_msg(phone, "1", "m2"))
    vicky_app.handle(_text_msg(phone, "12000", "m3"))   # propuesta -> alerta temprana (1)
    vicky_app.handle(_text_msg(phone, "1", "m4"))       # acepta revision
    vicky_app.handle(_text_msg(phone, "Juan Prueba", "m5"))  # nombre
    vicky_app.handle(_text_msg(phone, "Los Mochis", "m6"))   # ciudad -> alerta de calificacion (2)

    assert len(advisor_msgs) == 2
    assert "PROPUESTA CALCULADA" in advisor_msgs[0]
    assert "CALIFICADO" in advisor_msgs[1]
