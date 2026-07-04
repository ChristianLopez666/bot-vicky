"""
Upgrade visible de UX para Prestamo IMSS: de un formulario generico de lead
(pide monto deseado, nombre, telefono, si cobra en Inbursa -- 6+ preguntas sin
mostrar nunca un numero) a un flujo que usa la calculadora existente
(cotizador_prestamos_imss.jsx, puerto exacto en calcular_propuesta_imss()) para
dar una propuesta estimada inmediata a partir de la pension mensual.

V2 (acceptance fix): agrega bienvenida + filtro Ley 73 antes de pedir la
pension, continuidad de "propuesta activa" para preguntas de seguimiento
sobre monto/plazo/pago, y cortesia despues del cierre para evitar el
fallback neutral de Boardroom tras un "gracias"/"ok"/etc.
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
    """menu -> 1 (abre calculadora) -> 1 (Ley 73) -> pension -> revision -> nombre -> ciudad"""
    sent, advisor_msgs, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "1", "m1"))                      # menu -> bienvenida + filtro Ley73
    vicky_app.handle(_text_msg(phone, "1", "m2"))                      # Ley73 = si -> pide pension
    vicky_app.handle(_text_msg(phone, "12000", "m3"))                  # pension -> propuesta
    vicky_app.handle(_text_msg(phone, "1", "m4"))                      # quiere revision
    vicky_app.handle(_text_msg(phone, "Juan Prueba IMSS", "m5"))       # nombre
    vicky_app.handle(_text_msg(phone, "Los Mochis", "m6"))             # ciudad -> cierre
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


# Correccion 1 -- bienvenida + filtro Ley 73
def test_option_1_shows_welcome(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "mid-1"))
    msg = sent[0][1]
    assert "Préstamo IMSS" in msg
    assert "soy Vicky" in msg


def test_option_1_asks_ley73_before_pension(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "mid-1b"))
    msg = sent[0][1]
    assert "Ley 73" in msg
    assert "pensión IMSS" not in msg  # todavia no pide el monto de pension
    for forbidden in ("aprobado", "autorizado", "credito seguro", "ya calificaste"):
        assert forbidden not in msg.lower()


def test_ley73_response_1_continues_to_pension_question(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    assert boardroom_calls == []
    assert "pensión" in sent[-1][1].lower()
    assert vicky_app.user_state.get("6682222222") == "imss_q_pension_calc"


def test_ley73_response_2_also_continues_to_pension(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "2", "m2"))
    assert vicky_app.user_state.get("6682222222") == "imss_q_pension_calc"


def test_ley73_response_3_explains_and_does_not_ask_pension(monkeypatch):
    sent, advisor_msgs, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "3", "m2"))
    assert "Christian" in sent[-1][1]
    # queda en post-cierre (no None) para poder responder cortesia sin fallback
    assert vicky_app.user_state.get("6682222222") == "imss_post_cierre"
    assert len(advisor_msgs) == 1


def test_ley73_response_4_asks_familiar_pension(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "4", "m2"))
    assert "familiar" in sent[-1][1].lower()
    assert vicky_app.user_state.get("6682222222") == "imss_q_pension_calc"


# 5 & 6. Intent libre entra al flujo (siempre inicia con bienvenida+filtro)
def test_cuanto_me_prestan_enters_imss_flow(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "cuanto me prestan", "mid-free1"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6682222222") == "imss_q_ley73"
    assert "Ley 73" in sent[0][1]


def test_con_mi_pension_cuanto_alcanzo_enters_imss_flow(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "con mi pension cuanto alcanzo", "mid-free2"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6682222222") == "imss_q_ley73"


def test_free_form_with_pension_still_asks_ley73_first(monkeypatch):
    """Correccion 1: incluso si el mensaje ya trae una pension, primero se
    confirma Ley 73 -- no se salta directo al calculo."""
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "mi pension es de 12000 cuanto me prestan", "mid-free3"))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6682222222") == "imss_q_ley73"
    assert "Ley 73" in sent[0][1]


# 8. Monto sin contexto de pension no se confunde con pension
def test_necesito_amount_does_not_treat_as_pension(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "necesito 50000", "mid-necesito"))
    assert boardroom_calls == []
    assert vicky_app.user_data.get("6682222222", {}).get("pension") is None
    assert "50,000" not in sent[0][1]
    assert vicky_app.user_state.get("6682222222") == "imss_q_ley73"


# 9. La calculadora existente se usa (mismo puerto de cotizador_prestamos_imss.jsx)
def test_calculator_matches_ported_formula():
    propuesta = vicky_app.calcular_propuesta_imss(12000)
    assert propuesta["plazo"] == 60
    assert propuesta["cuota_max"] == 12000 * 0.30
    assert abs(propuesta["cuota"] - propuesta["cuota_max"]) < 1.0
    assert propuesta["monto"] > 0
    assert propuesta["total"] == propuesta["cuota"] * propuesta["plazo"]


# 4 & 10-14. Contenido del mensaje de propuesta
def test_pension_10000_calculates_proposal(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "10000", "m3"))
    msg = sent[-1][1]
    assert "10,000" in msg
    assert "Monto aproximado" in msg
    assert "Pago aproximado" in msg
    assert "Plazo" in msg
    assert "informativa" in msg
    for forbidden in ("aprobado", "autorizado", "ya calificaste", "garantizado", "credito seguro"):
        assert forbidden not in msg.lower()
    assert vicky_app.user_state.get("6682222222") == "imss_q_revision"


# Correccion 2 -- continuidad ante pregunta de seguimiento
def test_followup_question_recalculates_with_requested_amount_and_plazo(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "10000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "cuánto pagaría por 100,000 pesos a 60 meses", "m4"))
    assert boardroom_calls == []
    msg = sent[-1][1]
    assert "100,000" in msg
    assert "60 meses" in msg
    assert "pago" in msg.lower()
    # sigue en propuesta activa, no se rompe ni cae al "responde 1 o 2"
    assert "Responde" not in msg
    assert vicky_app.user_state.get("6682222222") == "imss_q_revision"


def test_followup_question_with_only_amount(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "10000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "y si quiero 50000", "m4"))
    msg = sent[-1][1]
    assert "50,000" in msg


def test_followup_question_with_only_plazo(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "10000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "a 48 meses cuánto pago", "m4"))
    msg = sent[-1][1]
    assert "48 meses" in msg


def test_followup_question_without_numbers_restates_proposal(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "10000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "cuánto me descuentan", "m4"))
    msg = sent[-1][1]
    assert "$" in msg
    assert "Responde" not in msg


def test_no_generic_respond_1_or_2_on_valid_followup(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "10000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "me prestan más", "m4"))
    assert "Responde *1* si quieres" not in sent[-1][1]


# 15-19. Seguimiento y cierre
def test_after_proposal_yes_asks_name(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "1", "m4"))
    assert "nombre completo" in sent[-1][1]


def test_captures_name_then_asks_city(monkeypatch):
    sent, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "1", "m4"))
    vicky_app.handle(_text_msg("6682222222", "Juan Prueba IMSS", "m5"))
    assert "ciudad" in sent[-1][1].lower()
    assert vicky_app.user_data["6682222222"]["nombre"] == "Juan Prueba Imss"


def test_captures_city_and_closes_with_review_message(monkeypatch):
    sent, advisor_msgs, _ = _run_full_imss_flow(monkeypatch)
    closing = sent[-1][1]
    assert "Christian" in closing
    assert "revisará" in closing or "validar" in closing
    for forbidden in ("aprobado", "autorizado", "ya calificaste"):
        assert forbidden not in closing.lower()


# 7. "2" cierra correctamente
def test_decline_review_closes_politely(monkeypatch):
    sent, advisor_msgs, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "2", "m4"))
    assert advisor_msgs == []
    assert "Préstamo IMSS" in sent[-1][1] or "cuánto me prestan" in sent[-1][1]
    assert vicky_app.user_state.get("6682222222") == "imss_post_cierre"


# Correccion 3 -- cortesia tras cierre, sin fallback neutral
def test_gracias_after_close_does_not_trigger_fallback(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "2", "m4"))
    vicky_app.handle(_text_msg("6682222222", "gracias", "m5"))
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
    assert "Con gusto" in sent[-1][1]
    assert vicky_app.user_state.get("6682222222") is None


def test_other_courtesy_words_after_close_do_not_trigger_fallback(monkeypatch):
    for word in ("ok", "perfecto", "sale", "de acuerdo"):
        sent, _, boardroom_calls = _base_patches(monkeypatch)
        vicky_app.handle(_text_msg("6682222222", "1", "m1"))
        vicky_app.handle(_text_msg("6682222222", "1", "m2"))
        vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
        vicky_app.handle(_text_msg("6682222222", "2", "m4"))
        vicky_app.handle(_text_msg("6682222222", word, "m5"))
        assert boardroom_calls == []
        assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


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


# 9. Estado activo IMSS no llama a Boardroom (incluye imss_post_cierre)
def test_active_imss_calc_state_never_calls_boardroom(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert boardroom_calls == []


def test_post_cierre_courtesy_message_never_calls_boardroom(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "2", "m4"))
    vicky_app.handle(_text_msg("6682222222", "gracias", "m5"))
    assert boardroom_calls == []


# Correccion (production verification failed): cortesia tras el CIERRE EXITOSO
# (acepta revision -> nombre -> ciudad -> notificacion), no solo tras declinar.
def test_gracias_after_successful_close_gets_courtesy_not_fallback(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert len(advisor_msgs) == 1  # notificacion ya enviada por el cierre exitoso
    vicky_app.handle(_text_msg("6682222222", "gracias", "m7"))
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)
    assert "Christian" in sent[-1][1]
    # no se manda una segunda notificacion al asesor
    assert len(advisor_msgs) == 1
    assert vicky_app.user_state.get("6682222222") is None


def test_courtesy_after_successful_close_does_not_spam_on_second_message(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "gracias", "m7"))
    sent.clear()
    vicky_app.handle(_text_msg("6682222222", "hola", "m8"))
    # el estado ya se limpio tras la primera cortesia; "hola" es un menu trigger
    assert "Servicios Financieros Inbursa" in sent[0][1]


# Mensaje que combina cortesia con una intencion nueva: no debe tragarse
def test_courtesy_combined_with_new_intent_is_not_swallowed(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "1", "m1"))
    vicky_app.handle(_text_msg("6682222222", "1", "m2"))
    vicky_app.handle(_text_msg("6682222222", "12000", "m3"))
    vicky_app.handle(_text_msg("6682222222", "2", "m4"))
    vicky_app.handle(_text_msg("6682222222", "gracias, también quiero cotizar auto", "m5"))
    # no debe responder el acuse de cortesia especifico de cierre IMSS
    assert "Si después quieres revisar una propuesta" not in sent[-1][1]
    assert vicky_app.user_state.get("6682222222") != "imss_post_cierre"
    # se libero el estado y se enruto como mensaje nuevo (aqui: a Boardroom,
    # que es la autoridad comercial para texto libre sin producto local claro)
    assert len(boardroom_calls) == 1


# 10. Mensajes libres no-IMSS sin estado activo siguen yendo a Boardroom
def test_unrelated_free_form_still_calls_boardroom(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "hola quiero saber sobre opciones de inversion", "mid-free"))
    assert len(boardroom_calls) == 1


# 11. CTC (opcion 6) sigue funcionando
def test_ctc_option_6_still_works(monkeypatch):
    sent, _, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6682222222", "6", "mid-6"))
    assert boardroom_calls == []
    assert "Consigue Tu Crédito" in sent[0][1]


# Sin fallback neutral durante todo el flujo IMSS valido
def test_no_neutral_fallback_during_imss_flow(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


# Sin respuestas duplicadas
def test_no_duplicate_responses_in_imss_flow(monkeypatch):
    sent, advisor_msgs, boardroom_calls = _run_full_imss_flow(monkeypatch)
    assert len(sent) == 6


# Contrato tecnico: product_code no cambia
def test_product_code_contract_preserved(monkeypatch):
    assert vicky_app._service_to_product_code("imss") == "prestamo_imss_ley73"
