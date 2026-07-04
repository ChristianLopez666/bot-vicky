"""
Upgrade visible de UX para Prestamo IMSS: de un formulario generico de lead
(pide monto deseado, nombre, telefono, si cobra en Inbursa -- 6+ preguntas sin
mostrar nunca un numero) a un flujo que usa la calculadora existente
(cotizador_prestamos_imss.jsx, puerto exacto en calcular_propuesta_imss()) para
dar una propuesta estimada inmediata a partir de la pension mensual.
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

    sent = []

    def fake_send_msg(to, text):
        sent.append((to, text))
        return True

    advisor_msgs = []
    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")

    boardroom_calls = []

    def fake_request_boardroom_instruction(payload):
        boardroom_calls.append(payload)
        return None, "should_not_be_called"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", fake_request_boardroom_instruction)

    return sent, advisor_msgs, boardroom_calls


def _run_full_imss_flow(monkeypatch, phone="6682222222"):
    sent, advisor_msgs, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "1", "m1"))          # menu -> abre calculadora
    vicky_app.handle(_text_msg(phone, "12000", "m2"))      # pension -> propuesta
    vicky_app.handle(_text_msg(phone, "1", "m3"))          # quiere revision
    vicky_app.handle(_text_msg(phone, "Juan Prueba IMSS", "m4"))  # nombre
    vicky_app.handle(_text_msg(phone, "Los Mochis", "m5")) # ciudad -> cierre
    return sent, advisor_msgs, boardroom_calls


# 1 & 2. Menu
def test_menu_contains_imss_pensionados_wording(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "menu", "mid-menu"))
    assert "Préstamo IMSS para pensionados" in sent[0][1]


def test_menu_mentions_proposal_calculation(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "menu", "mid-menu2"))
    assert "propuesta estimada" in sent[0][1]
    assert "pensión" in sent[0][1]


# 3 & 4. Seleccionar opcion 1 pide pension primero, sin prometer aprobacion
def test_option_1_asks_pension_first(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "mid-1"))
    msg = sent[0][1]
    assert "Préstamo IMSS" in msg
    assert "pensión IMSS" in msg
    for forbidden in ("aprobado", "autorizado", "credito seguro", "ya calificaste"):
        assert forbidden not in msg.lower()


# 5 & 6. Intent libre entra al flujo de calculadora
def test_cuanto_me_prestan_enters_imss_flow(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "cuanto me prestan", "mid-free1"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6682222222", "").startswith("imss_")
    assert "pensión IMSS" in sent[0][1]


def test_con_mi_pension_cuanto_alcanzo_enters_imss_flow(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "con mi pension cuanto alcanzo", "mid-free2"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6682222222", "").startswith("imss_")


# 7. Extrae pension de un mensaje libre y calcula directo
def test_free_form_with_pension_extracts_and_calculates(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "mi pension es de 12000 cuanto me prestan", "mid-free3"))
    assert boardroom_calls == []
    msg = sent[0][1]
    assert "12,000" in msg
    assert "Monto aproximado" in msg
    assert vicky_app.user_state.get("6682222222") == "imss_q_revision"


# 8. Monto sin contexto de pension no se confunde con pension
def test_necesito_amount_does_not_treat_as_pension(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "necesito 50000", "mid-necesito"))
    assert boardroom_calls == []
    assert vicky_app.user_data.get("6682222222", {}).get("pension") is None
    assert "pensión IMSS" in sent[0][1]
    assert "50,000" not in sent[0][1]
    assert vicky_app.user_state.get("6682222222") == "imss_q_pension_calc"


# 9. La calculadora existente se usa (mismo puerto de cotizador_prestamos_imss.jsx)
def test_calculator_matches_ported_formula():
    propuesta = vicky_app.calcular_propuesta_imss(12000)
    assert propuesta["plazo"] == 60
    assert propuesta["cuota_max"] == 12000 * 0.30
    assert abs(propuesta["cuota"] - propuesta["cuota_max"]) < 1.0
    assert propuesta["monto"] > 0
    assert propuesta["total"] == propuesta["cuota"] * propuesta["plazo"]


# 10-14. Contenido del mensaje de propuesta
def test_proposal_message_content(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m2"))
    msg = sent[-1][1]
    assert "12,000" in msg
    assert "Monto aproximado" in msg
    assert "Pago aproximado" in msg
    assert "Plazo" in msg
    assert "informativa" in msg
    for forbidden in ("aprobado", "autorizado", "ya calificaste", "garantizado", "credito seguro"):
        assert forbidden not in msg.lower()


# 15-19. Seguimiento y cierre
def test_after_proposal_yes_asks_name(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m2"))
    vicky_app.handle(_text_msg("6682222222", "1", "m3"))
    assert "nombre completo" in sent[-1][1]


def test_captures_name_then_asks_city(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m2"))
    vicky_app.handle(_text_msg("6682222222", "1", "m3"))
    vicky_app.handle(_text_msg("6682222222", "Juan Prueba IMSS", "m4"))
    assert "ciudad" in sent[-1][1].lower()
    assert vicky_app.user_data["6682222222"]["nombre"] == "Juan Prueba Imss"


def test_captures_city_and_closes_with_review_message(monkeypatch):
    sent, advisor_msgs, _ = _run_full_imss_flow(monkeypatch)
    closing = sent[-1][1]
    assert "Christian" in closing
    assert "revisará" in closing or "validar" in closing
    for forbidden in ("aprobado", "autorizado", "ya calificaste"):
        assert forbidden not in closing.lower()


def test_decline_review_closes_politely_without_capturing_more_data(monkeypatch):
    sent, advisor_msgs, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m2"))
    vicky_app.handle(_text_msg("6682222222", "2", "m3"))
    assert advisor_msgs == []
    assert vicky_app.user_state.get("6682222222") is None
    assert "Préstamo IMSS" in sent[-1][1] or "cuánto me prestan" in sent[-1][1]


# 20-23. Notificacion al asesor
def test_advisor_notification_says_nuevo_lead_imss_propuesta(monkeypatch):
    sent, advisor_msgs, _ = _run_full_imss_flow(monkeypatch)
    assert len(advisor_msgs) == 1
    assert "NUEVO LEAD — PRÉSTAMO IMSS CON PROPUESTA" in advisor_msgs[0]


def test_advisor_notification_contains_pension_and_estimate(monkeypatch):
    sent, advisor_msgs, _ = _run_full_imss_flow(monkeypatch)
    msg = advisor_msgs[0]
    assert "12,000" in msg
    assert "Monto estimado" in msg
    assert "Juan Prueba Imss" in msg
    assert "Los Mochis" in msg


def test_advisor_notification_contains_pending_validation(monkeypatch):
    sent, advisor_msgs, _ = _run_full_imss_flow(monkeypatch)
    assert "Pendiente de validación" in advisor_msgs[0]


# 24. Estado activo IMSS no llama a Boardroom
def test_active_imss_calc_state_never_calls_boardroom(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert boardroom_calls == []


# 25. Mensajes libres no-IMSS sin estado activo siguen yendo a Boardroom
def test_unrelated_free_form_still_calls_boardroom(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "hola quiero saber sobre opciones de inversion", "mid-free"))
    assert len(boardroom_calls) == 1


# 26. CTC (opcion 6) sigue funcionando
def test_ctc_option_6_still_works(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "6", "mid-6"))
    assert boardroom_calls == []
    assert "Consigue Tu Crédito" in sent[0][1]


# 27. Sin fallback neutral durante el flujo IMSS valido
def test_no_neutral_fallback_during_imss_flow(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


# 28. Sin respuestas duplicadas
def test_no_duplicate_responses_in_imss_flow(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert len(sent) == 5


# Contrato tecnico: product_code no cambia
def test_product_code_contract_preserved(monkeypatch):
    assert vicky_app._service_to_product_code("imss") == "prestamo_imss_ley73"
