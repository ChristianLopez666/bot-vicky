"""
Hallazgo real 2026-08-04: el lead 5214521126115 quedo atrapado 8 veces en el
estado imss_q_ley73 respondiendo cosas que _imss_ley73_choice no interpreta
("no entiendo", "no", texto libre) -- el bot solo repetia "responde 1,2,3,4"
sin escalar ni avisar al asesor. Agrega: (1) escape explicito por palabra
"asesor"/"humano", (2) aviso al asesor una sola vez al segundo intento
invalido, (3) mensaje aclarado con ejemplos a partir del segundo intento.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


def _base_patches(monkeypatch):
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})

    sent = []
    advisor_msgs = []

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)

    return sent, advisor_msgs


def _set_state(phone: str) -> None:
    vicky_app.user_state[phone] = "imss_q_ley73"
    vicky_app.user_data[phone] = {}


def test_first_invalid_reply_does_not_notify_advisor(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6681112222"
    _set_state(phone)

    vicky_app.funnel_imss(phone, "no entiendo")

    assert advisor_msgs == []
    assert vicky_app.user_data[phone]["ley73_intentos_invalidos"] == 1
    assert "responde" in sent[-1][1].lower()


def test_second_invalid_reply_notifies_advisor_once_with_clarified_message(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6681112222"
    _set_state(phone)

    vicky_app.funnel_imss(phone, "no entiendo")
    vicky_app.funnel_imss(phone, "no se que me quiere decir")

    assert len(advisor_msgs) == 1
    assert phone in advisor_msgs[0]
    assert "ATASCADO" in advisor_msgs[0]
    last_reply = sent[-1][1]
    assert "Escribe *1*" in last_reply
    assert "asesor" in last_reply.lower()


def test_third_invalid_reply_does_not_spam_advisor_again(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6681112222"
    _set_state(phone)

    for msg in ("no entiendo", "no se", "que"):
        vicky_app.funnel_imss(phone, msg)

    assert len(advisor_msgs) == 1
    assert vicky_app.user_data[phone]["ley73_intentos_invalidos"] == 3


def test_asesor_keyword_escalates_and_closes_funnel(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6681112222"
    _set_state(phone)

    vicky_app.funnel_imss(phone, "no entiendo")
    vicky_app.funnel_imss(phone, "quiero hablar con un asesor")

    assert len(advisor_msgs) == 1
    assert "SOLICITA ASESOR" in advisor_msgs[0]
    assert vicky_app.user_state[phone] == "imss_post_cierre"


def test_valid_reply_after_invalid_attempts_proceeds_and_resets_counter(monkeypatch):
    sent, advisor_msgs = _base_patches(monkeypatch)
    phone = "6681112222"
    _set_state(phone)

    vicky_app.funnel_imss(phone, "no entiendo")
    vicky_app.funnel_imss(phone, "1")

    assert vicky_app.user_state[phone] == "imss_q_pension_calc"
    assert "ley73_intentos_invalidos" not in vicky_app.user_data[phone]
