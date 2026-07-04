"""
Upgrade visible de UX para la opcion 6 (Financiamiento Practico -> Consigue Tu
Credito / CTC). Antes de este cambio, la opcion 6 usaba copy identico al viejo
"Financiamiento Practico Empresarial" (menu, apertura, funnel de 11 preguntas
burocraticas, cierre, notificacion al asesor) y no se distinguia como campana
propia. Este archivo prueba que el cliente y el asesor ven una experiencia
visiblemente distinta, preservando el contrato tecnico (product_code=
credito_empresarial_sin_garantia).
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


def _run_full_ctc_flow(monkeypatch, phone="6681111111"):
    sent, advisor_msgs, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "6", "m1"))
    vicky_app.handle(_text_msg(phone, "1", "m2"))               # tipo de actividad
    vicky_app.handle(_text_msg(phone, "500000", "m3"))          # monto
    vicky_app.handle(_text_msg(phone, "inventario", "m4"))      # uso
    vicky_app.handle(_text_msg(phone, "abarrotes", "m5"))       # giro
    vicky_app.handle(_text_msg(phone, "1", "m6"))               # factura
    vicky_app.handle(_text_msg(phone, "Juan Prueba CTC", "m7")) # nombre
    return sent, advisor_msgs, boardroom_calls


# 1. Menu contiene "Consigue Tu Crédito"
def test_menu_contains_consigue_tu_credito(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "menu", "mid-menu"))
    assert "Consigue Tu Crédito" in sent[0][1]


# 2. Opcion 6 del menu ya no dice solo el generico viejo
def test_menu_option_6_is_not_only_old_generic_label(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "menu", "mid-menu2"))
    assert "Financiamiento Práctico Empresarial" not in sent[0][1]


# 3. Seleccionar "6" envia apertura CTC sin promesa de aprobacion
def test_option_6_opening_message_is_ctc_branded(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "mid-6"))
    msg = sent[0][1]
    assert "Consigue Tu Crédito" in msg
    assert "crédito empresarial sin garantía" in msg
    for forbidden in ("aprobado", "preaprobado", "autorizado", "Tu solicitud fue aprobada", "Crédito autorizado"):
        assert forbidden not in msg


# 4. Primera pregunta CTC: negocio / independiente / empezando
def test_ctc_first_question_asks_business_type(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "mid-6b"))
    msg = sent[0][1]
    assert "negocio" in msg.lower()
    assert "independiente" in msg.lower()
    assert "empezando" in msg.lower()


# 5. Funnel pregunta monto
def test_ctc_funnel_asks_amount(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "m1"))
    vicky_app.handle(_text_msg("6681111111", "1", "m2"))
    assert "crédito necesitas aproximadamente" in sent[-1][1]


# 6. Funnel pregunta uso del credito
def test_ctc_funnel_asks_credit_use(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "m1"))
    vicky_app.handle(_text_msg("6681111111", "1", "m2"))
    vicky_app.handle(_text_msg("6681111111", "500000", "m3"))
    assert "¿Para qué lo usarías?" in sent[-1][1]


# 7. Funnel pregunta giro/actividad
def test_ctc_funnel_asks_giro(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "m1"))
    vicky_app.handle(_text_msg("6681111111", "1", "m2"))
    vicky_app.handle(_text_msg("6681111111", "500000", "m3"))
    vicky_app.handle(_text_msg("6681111111", "inventario", "m4"))
    assert "giro o actividad" in sent[-1][1]


# 8. Funnel pregunta si factura
def test_ctc_funnel_asks_if_invoices(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "m1"))
    vicky_app.handle(_text_msg("6681111111", "1", "m2"))
    vicky_app.handle(_text_msg("6681111111", "500000", "m3"))
    vicky_app.handle(_text_msg("6681111111", "inventario", "m4"))
    vicky_app.handle(_text_msg("6681111111", "abarrotes", "m5"))
    assert "¿Actualmente facturas?" in sent[-1][1]


# 9. Funnel pregunta nombre completo
def test_ctc_funnel_asks_full_name(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "6", "m1"))
    vicky_app.handle(_text_msg("6681111111", "1", "m2"))
    vicky_app.handle(_text_msg("6681111111", "500000", "m3"))
    vicky_app.handle(_text_msg("6681111111", "inventario", "m4"))
    vicky_app.handle(_text_msg("6681111111", "abarrotes", "m5"))
    vicky_app.handle(_text_msg("6681111111", "1", "m6"))
    assert "nombre completo" in sent[-1][1]


# 10 & 11. Cierre: Christian revisa, sin promesa de aprobacion
def test_ctc_closing_message_promises_review_not_approval(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    closing = sent[-1][1]
    assert "Christian" in closing
    assert "revisará" in closing
    for forbidden in ("aprobado", "autorizado", "ya calificaste", "Tu solicitud fue aprobada"):
        assert forbidden not in closing


# 12, 13, 14. Notificacion al asesor
def test_advisor_notification_says_nuevo_lead_ctc(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    assert len(advisor_msgs) == 1
    assert "NUEVO LEAD — CONSIGUE TU CRÉDITO" in advisor_msgs[0]


def test_advisor_notification_contains_product_label(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    assert "Crédito empresarial sin garantía" in advisor_msgs[0]


def test_advisor_notification_contains_campaign(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    assert "CTC julio 2026" in advisor_msgs[0]


def test_advisor_notification_contains_captured_fields(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    msg = advisor_msgs[0]
    assert "Juan Prueba CTC" in msg
    assert "6681111111" in msg
    assert "500000" in msg
    assert "inventario" in msg
    assert "abarrotes" in msg


# 15. Sin fallback neutral durante el flujo CTC valido
def test_no_neutral_fallback_during_ctc_flow(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
    assert boardroom_calls == []


# 16. Sin respuestas duplicadas (una respuesta por mensaje entrante)
def test_no_duplicate_responses_in_ctc_flow(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_ctc_flow(monkeypatch)
    assert len(sent) == 7


# 17. Mensajes libres sin estado activo siguen yendo a Boardroom
def test_free_form_without_active_state_still_goes_to_boardroom(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "hola quiero saber sobre opciones de inversion", "mid-free"))
    assert len(boardroom_calls) == 1


# 18. Otras opciones del menu no se rompen
def test_other_menu_options_still_work(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6681111111", "1", "mid-opt1"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6681111111", "").startswith("imss_")


# Contrato tecnico: product_code y campana no cambian
def test_product_code_and_campaign_contract_preserved(monkeypatch):
    assert vicky_app._service_to_product_code("fp") == "credito_empresarial_sin_garantia"
    assert vicky_app._service_to_product_code("fp") != "nomina_empresarial"
    assert vicky_app.CTC_CAMPAIGN_LABEL == "CTC julio 2026"
