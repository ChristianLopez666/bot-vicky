"""
Fix del cierre post-funnel de CTC (Consigue Tu Credito / opcion 6): el cierre
exitoso llamaba reset() directo, asi que un "gracias" (o "ok"/"perfecto"/
"sale"/etc) despues del cierre caia en _handle_boardroom_authority() y
mostraba el fallback neutral ("Recibi tu mensaje. En un momento te
atiendo."). Mismo patron ya aplicado a IMSS (imss_post_cierre), aqui
replicado de forma independiente para CTC (fp_post_cierre) sin tocar nada
de IMSS.
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
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


def _base_patches(monkeypatch):
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_seen_ids", set())
    monkeypatch.setattr(vicky_app, "_seen_dq", vicky_app.__dict__.get("_seen_dq", []).__class__())
    monkeypatch.setattr(vicky_app, "_ctc_post_close_ctx", {})

    sent = []

    def fake_send_msg(to, text):
        sent.append((to, text))
        return True

    advisor_msgs = []
    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")

    boardroom_calls = []

    def fake_request_boardroom_instruction(payload):
        boardroom_calls.append(payload)
        return None, "should_not_be_called"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", fake_request_boardroom_instruction)

    return sent, advisor_msgs, boardroom_calls


def _run_full_ctc_flow(monkeypatch, phone="6683333333"):
    sent, advisor_msgs, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "6", "m1"))
    vicky_app.handle(_text_msg(phone, "1", "m2"))
    vicky_app.handle(_text_msg(phone, "500000", "m3"))
    vicky_app.handle(_text_msg(phone, "inventario", "m4"))
    vicky_app.handle(_text_msg(phone, "ferretero", "m5"))
    vicky_app.handle(_text_msg(phone, "1", "m6"))
    vicky_app.handle(_text_msg(phone, "Juan Prueba CTC", "m7"))
    return sent, advisor_msgs, boardroom_calls


# 1. El cierre exitoso registra contexto post-cierre (independiente de
# user_state, que vuelve a None tras un reset() completo)
def test_ctc_successful_close_stores_post_close_state(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    assert len(advisor_msgs) == 1
    assert vicky_app.user_state.get("6683333333") is None
    assert vicky_app._ctc_post_close_active("6683333333") is True


# 2, 3, 4. "gracias" tras el cierre: cortesia, sin Boardroom, sin fallback
def test_gracias_after_ctc_close_gets_courtesy_response(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias", "m8"))
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
    assert "Christian" in sent[-1][1]
    assert vicky_app.user_state.get("6683333333") is None


# 5. "ok"/"perfecto"/"sale" tambien se manejan
def test_ok_perfecto_sale_after_close_are_handled(monkeypatch):
    for word in ("ok", "perfecto", "sale", "va", "entendido", "listo"):
        sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
        vicky_app.handle(_text_msg("6683333333", word, "m8"))
        assert boardroom_calls == []
        assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
        assert "Christian" in sent[-1][1]


# 6. Segunda cortesia no hace spam (el estado ya se limpio tras la primera)
def test_second_courtesy_does_not_spam(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias", "m8"))
    sent.clear()
    vicky_app.handle(_text_msg("6683333333", "ok", "m9"))
    # el estado ya se limpio; "ok" sin estado activo no es un trigger local
    # de menu, asi que -- segun la arquitectura actual -- se enruta a Boardroom,
    # no vuelve a mostrar la cortesia de cierre ni el fallback neutral.
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


# 7. Sin notificacion duplicada al asesor
def test_no_duplicate_advisor_notification_after_courtesy(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias", "m8"))
    assert len(advisor_msgs) == 1


# 8 & 9. Cortesia combinada con nueva intencion no se traga
def test_courtesy_combined_with_new_intent_not_swallowed_auto(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias, también quiero cotizar auto", "m8"))
    assert "Christian revisará tu caso" not in sent[-1][1]
    assert vicky_app.user_state.get("6683333333") != "fp_post_cierre"
    assert len(boardroom_calls) == 1


def test_courtesy_combined_with_new_intent_not_swallowed_imss(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias, cuánto me prestan", "m8"))
    # "cuanto me prestan" es intent IMSS valido: debe entrar al flujo IMSS,
    # no tragarse como cortesia de cierre CTC.
    assert vicky_app.user_state.get("6683333333", "").startswith("imss_")
    assert boardroom_calls == []


# 10 & 11 & 12. CTC sigue funcionando, contrato tecnico preservado
def test_ctc_option_6_still_works(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "6", "mid-6"))
    assert boardroom_calls == []
    assert "Consigue Tu Crédito" in sent[0][1]


def test_ctc_product_code_preserved():
    assert vicky_app._service_to_product_code("fp") == "credito_empresarial_sin_garantia"


def test_ctc_campaign_label_preserved():
    assert vicky_app.CTC_CAMPAIGN_LABEL == "CTC julio 2026"


# 13. IMSS calculator flow sigue pasando (regresion rapida)
def test_imss_calculator_flow_still_works(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "1", "m1"))
    vicky_app.handle(_text_msg("6683333333", "1", "m2"))
    vicky_app.handle(_text_msg("6683333333", "10000", "m3"))
    assert boardroom_calls == []
    assert "Monto aproximado" in sent[-1][1]


# 14. Mensaje libre no relacionado sin estado post-cierre sigue yendo a Boardroom
def test_unrelated_free_form_without_post_close_state_still_calls_boardroom(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "hola quiero saber sobre opciones de inversion", "mid-free"))
    assert len(boardroom_calls) == 1


# 15. Sin fallback neutral durante toda la secuencia cierre + cortesia
def test_no_neutral_fallback_during_close_and_courtesy(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias", "m8"))
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


# 16. Sin respuestas duplicadas
def test_no_duplicate_responses(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    vicky_app.handle(_text_msg("6683333333", "gracias", "m8"))
    assert len(sent) == 8
