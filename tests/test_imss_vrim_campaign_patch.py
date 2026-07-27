"""
Parche de lanzamiento -- Campaña Préstamos IMSS (Vicky Redes), 27 julio 2026.

Cubre lo que test_imss_visible_loan_proposal.py no cubría antes del parche:
  - Reconocimiento de campaña Meta IMSS por ad_id/hints via entorno (H-03),
    con prioridad absoluta de CTC cuando ambos coinciden.
  - Persistencia de origen/referral_* en la rama activa de _is_campaign() (H-02).
  - Jerarquía de elegibilidad VRIM con monto_solicitado (H-05): una vez
    vrim_preeligible=True nunca se degrada a False.
  - Notificación al asesor ANTES de la pregunta de horario, captura de
    advisor_notify_ok, y actualización breve cuando el horario llega.
  - No regresión de CTC/Auto/Vida/VRIM/Empresarial.
"""

import hashlib
import hmac
import json
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


def _text_msg(phone: str, text: str, mid: str, referral: dict | None = None) -> dict:
    obj = {"from": phone, "id": mid, "type": "text", "text": {"body": text}}
    if referral is not None:
        obj["referral"] = referral
    return obj


def _base_patches(monkeypatch):
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_seen_ids", set())
    monkeypatch.setattr(vicky_app, "_seen_dq", vicky_app.__dict__.get("_seen_dq", []).__class__())
    monkeypatch.setattr(vicky_app, "_ctc_post_close_ctx", {})
    monkeypatch.delenv("IMSS_META_REFERRAL_IDS", raising=False)
    monkeypatch.delenv("IMSS_META_REFERRAL_HINTS", raising=False)
    monkeypatch.delenv("CTC_META_REFERRAL_IDS", raising=False)
    monkeypatch.delenv("CTC_META_REFERRAL_HINTS", raising=False)

    sent = []

    def fake_send_msg(to, text):
        sent.append((to, text))
        return True

    advisor_msgs = []
    logged = []

    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: None)
    def fake_log(phone, nombre, msg, tipo, origen, resultado="", error="", mid=""):
        # Firma fiel a _log() real (incluye kwargs como 'resultado') para
        # poder distinguir tipo/resultado en las aserciones de los tests.
        logged.append((phone, nombre, msg, tipo, origen, resultado, error, mid))

    monkeypatch.setattr(vicky_app, "_log", fake_log)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")

    boardroom_calls = []

    def fake_request_boardroom_instruction(payload):
        boardroom_calls.append(payload)
        return None, "should_not_be_called"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", fake_request_boardroom_instruction)

    return sent, advisor_msgs, boardroom_calls, logged


def _run_to_proposal(monkeypatch, phone="6684440000", pension="12000"):
    """menu -> 1 -> 1 (Ley73) -> pension -> deja en imss_q_revision con
    propuesta ya calculada y VRIM ya ofrecida (>= 40000 por construccion)."""
    sent, advisor_msgs, boardroom_calls, logged = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "1", "m1"))
    vicky_app.handle(_text_msg(phone, "1", "m2"))
    vicky_app.handle(_text_msg(phone, pension, "m3"))
    return sent, advisor_msgs, boardroom_calls, logged


# ── Reconocimiento de campaña IMSS por ad_id/hints (H-03) ─────────────────────

_IMSS_AD_HEADLINE = "Prestamo para pensionados"
_IMSS_AD_BODY = "Conoce si calificas para un prestamo con tu pension."
_GENERIC_TEXT = "Hello! Can I get more info on this?"


def _imss_referral(source_id="9998887776665"):
    return {
        "source_type": "ad",
        "source_id": source_id,
        "headline": "oferta financiera",  # sin keywords IMSS a proposito
        "body": "conoce mas detalles",
        "ctwa_clid": "clid-imss",
    }


def test_imss_referral_recognized_by_keyword_headline(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    ref = {"source_type": "ad", "source_id": "111", "headline": _IMSS_AD_HEADLINE, "body": _IMSS_AD_BODY}
    vicky_app.handle(_text_msg("6684440001", _GENERIC_TEXT, "mid-1", referral=ref))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6684440001") == "imss_q_ley73"
    data = vicky_app.user_data.get("6684440001", {})
    assert data.get("origen", "").startswith("campana_IMSS")
    assert data.get("referral_headline") == _IMSS_AD_HEADLINE


def test_imss_referral_not_recognized_without_ad_id_or_keywords_asks_menu(monkeypatch):
    """Referral generico (sin keywords IMSS ni ad_id configurado): no se
    reconoce como IMSS -- cae a la pregunta aclaratoria del pre-router (no al
    fallback neutral, mientras exista objeto referral)."""
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440002", _GENERIC_TEXT, "mid-2", referral=_imss_referral()))
    assert boardroom_calls == []
    assert "1️⃣" in sent[0][1]


def test_imss_referral_recognized_by_ad_id_env_override(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    monkeypatch.setenv("IMSS_META_REFERRAL_IDS", "9998887776665")
    vicky_app.handle(_text_msg("6684440003", _GENERIC_TEXT, "mid-3", referral=_imss_referral("9998887776665")))
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6684440003") == "imss_q_ley73"
    data = vicky_app.user_data.get("6684440003", {})
    assert data.get("origen", "").startswith("campana_IMSS")
    assert data.get("referral_source_id") == "9998887776665"


def test_imss_referral_recognized_by_hint_env_override(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    monkeypatch.setenv("IMSS_META_REFERRAL_HINTS", "imss_ley73_julio2026")
    ref = _imss_referral("000")
    ref["campaign_name"] = "IMSS_Ley73_Julio2026"
    vicky_app.handle(_text_msg("6684440004", _GENERIC_TEXT, "mid-4", referral=ref))
    assert vicky_app.user_state.get("6684440004") == "imss_q_ley73"


def test_direct_text_prestamo_imss_without_referral(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440005", "quiero prestamo imss", "mid-5"))
    assert boardroom_calls == []
    assert vicky_app.user_data.get("6684440005", {}).get("origen") == "interes_directo_IMSS"


def test_ctc_wins_over_imss_even_if_imss_ad_id_env_matches(monkeypatch):
    """Si el mismo ID quedara configurado por error en ambas variables, CTC
    debe ganar siempre (prioridad absoluta)."""
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    ctc_id = "6951847773049"  # id real ya hardcoded en _CTC_META_REFERRAL_IDS
    monkeypatch.setenv("IMSS_META_REFERRAL_IDS", ctc_id)
    ref = {
        "source_type": "ad", "source_id": ctc_id,
        "headline": "financiamiento para empresas",
        "body": "credito empresarial y factoraje",
    }
    vicky_app.handle(_text_msg("6684440006", _GENERIC_TEXT, "mid-6", referral=ref))
    assert vicky_app.user_state.get("6684440006") == "fp_tipo"
    assert "Consigue Tu Crédito" in sent[0][1]


def test_ctc_referral_unaffected_by_imss_patch(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    ref = {"source_type": "ad", "source_id": "6951847773049", "headline": "financiamiento para empresas",
           "body": "credito empresarial y factoraje"}
    vicky_app.handle(_text_msg("6684440007", _GENERIC_TEXT, "mid-7", referral=ref))
    assert vicky_app.user_state.get("6684440007") == "fp_tipo"


# ── Jerarquía de elegibilidad VRIM (sin degradación) ───────────────────────────

def test_vrim_preeligible_true_at_exact_40000(monkeypatch):
    # Buscar una pension cuya propuesta.monto caiga lo mas cerca posible de
    # 40000 sin quedar debajo (el gate exacto ya lo prueba
    # test_imss_visible_loan_proposal). Se usa 5000 (monto ~50,125) para
    # confirmar el flag en el primer escalon arriba del minimo.
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch, pension="5000")
    data = vicky_app.user_data.get("6684440000", {})
    assert data["propuesta_monto"] >= vicky_app.IMSS_MONTO_MINIMO
    assert data["vrim_preeligible"] is True
    assert data["vrim_eligibility_basis"] == "propuesta_monto"
    assert data["vrim_offered"] is True


def test_monto_solicitado_viable_updates_basis_without_degrading(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    data = vicky_app.user_data.get("6684440000", {})
    propuesta_monto = data["propuesta_monto"]
    assert propuesta_monto > 45000  # pension 12000 -> propuesta ~120k
    vicky_app.handle(_text_msg("6684440000", "y si quiero 45000", "m4"))
    data = vicky_app.user_data.get("6684440000", {})
    assert data["monto_solicitado"] == 45000.0
    assert data["vrim_eligibility_basis"] == "monto_solicitado"
    assert data["vrim_preeligible"] is True


def test_monto_solicitado_below_minimum_does_not_degrade_vrim(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vrim_msg_count_before = sum(1 for s in sent if "VRIM Plus" in s[1])
    vicky_app.handle(_text_msg("6684440000", "y si quiero 30000", "m4"))
    data = vicky_app.user_data.get("6684440000", {})
    assert data["monto_solicitado"] == 30000.0
    # No se degrada ni se cambia la base ya establecida.
    assert data["vrim_preeligible"] is True
    assert data["vrim_eligibility_basis"] == "propuesta_monto"
    msg = sent[-1][1]
    assert "$40,000" in msg
    assert "mínimo" in msg.lower()
    # No se reenvia la oferta VRIM.
    vrim_msg_count_after = sum(1 for s in sent if "VRIM Plus" in s[1])
    assert vrim_msg_count_after == vrim_msg_count_before


# ── Turno posterior a "¿Te gustaría que revisemos tu caso a partir de esa
# cifra?" (mensaje de monto bajo el minimo) ────────────────────────────────────

def test_below_minimum_followup_yes_advances_to_nombre(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "y si quiero 30000", "m4"))
    assert "mínimo" in sent[-1][1].lower()
    vrim_count_before = sum(1 for s in sent if "VRIM Plus" in s[1])
    vicky_app.handle(_text_msg("6684440000", "si", "m5"))
    assert vicky_app.user_state.get("6684440000") == "imss_q_nombre_calc"
    assert boardroom_calls == []
    vrim_count_after = sum(1 for s in sent if "VRIM Plus" in s[1])
    assert vrim_count_after == vrim_count_before  # no se repite VRIM


def test_below_minimum_followup_no_closes_cleanly(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "y si quiero 30000", "m4"))
    vicky_app.handle(_text_msg("6684440000", "no", "m5"))
    assert vicky_app.user_state.get("6684440000") == "imss_post_cierre"
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


def test_below_minimum_followup_ambiguous_reprompts_without_boardroom(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "y si quiero 30000", "m4"))
    vicky_app.handle(_text_msg("6684440000", "tal vez", "m5"))
    # No queda en un estado ambiguo/perdido: sigue en imss_q_revision,
    # reprompteando localmente, nunca a Boardroom.
    assert vicky_app.user_state.get("6684440000") == "imss_q_revision"
    assert boardroom_calls == []
    assert "Responde" in sent[-1][1]


def test_monto_solicitado_above_propuesta_uses_propuesta_as_reference(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    data = vicky_app.user_data.get("6684440000", {})
    propuesta_monto = data["propuesta_monto"]
    vicky_app.handle(_text_msg("6684440000", "y si quiero 900000", "m4"))
    data = vicky_app.user_data.get("6684440000", {})
    assert data["monto_solicitado"] == 900000.0
    assert data["vrim_eligibility_basis"] == "propuesta_monto"
    assert data["vrim_preeligible"] is True
    msg = sent[-1][1]
    assert f"{propuesta_monto:,.0f}" in msg


def test_vrim_offer_not_duplicated_on_followup_question(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vrim_count_before = sum(1 for s in sent if "VRIM Plus" in s[1])
    assert vrim_count_before == 1
    vicky_app.handle(_text_msg("6684440000", "cuánto pagaría por 60000", "m4"))
    vrim_count_after = sum(1 for s in sent if "VRIM Plus" in s[1])
    assert vrim_count_after == 1


def test_vrim_offered_false_when_send_fails(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)
    calls = {"n": 0}

    def flaky_send(to, text):
        calls["n"] += 1
        if "VRIM Plus" in text:
            return False
        sent.append((to, text))
        return True

    monkeypatch.setattr(vicky_app, "send_msg", flaky_send)
    vicky_app.handle(_text_msg("6684440008", "1", "m1"))
    vicky_app.handle(_text_msg("6684440008", "1", "m2"))
    vicky_app.handle(_text_msg("6684440008", "12000", "m3"))
    data = vicky_app.user_data.get("6684440008", {})
    assert data["vrim_preeligible"] is True
    assert data.get("vrim_offered") is not True
    assert "vrim_offer_timestamp" not in data


def test_vrim_bubble_send_failure_sends_fallback_cta(monkeypatch):
    """Bloqueante 2: si la burbuja VRIM completa falla, el prospecto no debe
    quedarse sin CTA -- se manda un CTA de respaldo breve y el estado sigue
    siendo respondible con 1/2."""
    sent, advisor_msgs, boardroom_calls, _ = _base_patches(monkeypatch)

    def flaky_send(to, text):
        if "VRIM Plus" in text:
            return False
        sent.append((to, text))
        return True

    monkeypatch.setattr(vicky_app, "send_msg", flaky_send)
    vicky_app.handle(_text_msg("6684440020", "1", "m1"))
    vicky_app.handle(_text_msg("6684440020", "1", "m2"))
    vicky_app.handle(_text_msg("6684440020", "12000", "m3"))

    fallback_msg = sent[-1][1]
    assert fallback_msg.strip().endswith("2. No por ahora")
    assert "1. Sí, quiero que me contacte" in fallback_msg
    # No es la burbuja VRIM completa (esa fallo y no se agrego a sent[]).
    assert "VRIM Plus" not in fallback_msg

    data = vicky_app.user_data.get("6684440020", {})
    assert data.get("vrim_offered") is not True

    # El CTA de respaldo si permite avanzar el funnel con 1/2.
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    vicky_app.handle(_text_msg("6684440020", "1", "m4"))
    assert vicky_app.user_state.get("6684440020") == "imss_q_nombre_calc"


# ── Notificación al asesor antes del horario, advisor_notify_ok, abandono ─────

def test_advisor_notified_before_horario_question_and_lead_not_lost_on_abandon(monkeypatch):
    sent, advisor_msgs, boardroom_calls, logged = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "1", "m4"))
    vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
    vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))
    # Ya se notifico al asesor ANTES de que exista respuesta de horario.
    assert len(advisor_msgs) == 1
    assert "📣 PROSPECTO IMSS CALIFICADO — LLAMAR" in advisor_msgs[0]
    data = vicky_app.user_data.get("6684440000", {})
    assert data.get("advisor_notify_ok") is True
    assert data.get("nombre") == "Juan Perez"
    assert data.get("ciudad") == "Los Mochis"
    # El prospecto "abandona" (no responde el horario): el lead ya quedo
    # notificado y sus datos no se perdieron.
    assert vicky_app.user_state.get("6684440000") == "imss_q_horario_calc"


def test_horario_captured_sends_brief_advisor_update_and_closes(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "1", "m4"))
    vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
    vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))
    assert len(advisor_msgs) == 1
    vicky_app.handle(_text_msg("6684440000", "10am", "m7"))
    assert len(advisor_msgs) == 2
    assert "⏰ HORARIO DE CONTACTO" in advisor_msgs[1]
    assert "Juan Perez" in advisor_msgs[1]
    assert "10am" in advisor_msgs[1]
    # La actualizacion es breve: no repite toda la notificacion principal.
    assert "📣 PROSPECTO IMSS CALIFICADO" not in advisor_msgs[1]
    assert vicky_app.user_state.get("6684440000") == "imss_post_cierre"
    # Correccion bloqueante 1: _imss_close() ya NO descarta los datos
    # comerciales -- deben seguir disponibles despues de cerrar.
    data = vicky_app.user_data.get("6684440000", {})
    assert data.get("nombre") == "Juan Perez"
    assert data.get("ciudad") == "Los Mochis"
    assert data.get("pension") == 12000
    assert data.get("propuesta_monto")
    assert data.get("propuesta_cuota")
    assert data.get("propuesta_plazo")
    assert data.get("vrim_preeligible") is True
    assert data.get("vrim_offered") is True
    assert data.get("advisor_notify_ok") is True
    assert data.get("horario_contacto") == "10am"
    assert data.get("cierre_tipo") == "revision_aceptada"


def test_courtesy_reply_to_horario_question_does_not_save_as_horario(monkeypatch):
    sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "1", "m4"))
    vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
    vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))
    assert len(advisor_msgs) == 1
    vicky_app.handle(_text_msg("6684440000", "gracias", "m7"))
    # No se manda actualizacion de horario (falsa) al asesor.
    assert len(advisor_msgs) == 1
    data = vicky_app.user_data.get("6684440000", {})
    assert "horario_contacto" not in data
    # Se cierra de forma segura, sin quedar atrapado, y sin llamar a Boardroom.
    assert boardroom_calls == []
    assert vicky_app.user_state.get("6684440000") == "imss_post_cierre"
    # Los datos comerciales sobreviven igual al cierre por cortesia.
    assert data.get("nombre") == "Juan Perez"
    assert data.get("propuesta_monto")


def test_valid_time_expressions_are_saved_as_horario(monkeypatch):
    for horario_text in ("10:00 am", "después de las 4"):
        sent, advisor_msgs, boardroom_calls, _ = _run_to_proposal(monkeypatch)
        vicky_app.handle(_text_msg("6684440000", "1", "m4"))
        vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
        vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))
        vicky_app.handle(_text_msg("6684440000", horario_text, "m7"))
        assert len(advisor_msgs) == 2
        assert "⏰ HORARIO DE CONTACTO" in advisor_msgs[1]
        data = vicky_app.user_data.get("6684440000", {})
        assert data.get("horario_contacto") == horario_text


def test_advisor_notify_failure_keeps_conversation_going_and_logs_lead(monkeypatch):
    sent, advisor_msgs, boardroom_calls, logged = _run_to_proposal(monkeypatch)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: False)
    vicky_app.handle(_text_msg("6684440000", "1", "m4"))
    vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
    vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))
    data = vicky_app.user_data.get("6684440000", {})
    assert data.get("advisor_notify_ok") is False
    # La conversacion continua: se sigue preguntando el horario.
    assert "horario" in sent[-1][1].lower()
    # _log ya registro el lead en cada mensaje entrante, independientemente
    # del resultado de notify_advisor (respaldo Sheets existente).
    assert len(logged) >= 6


def test_advisor_notify_failure_writes_structured_backup_row(monkeypatch):
    """Bloqueante 3: cuando notify_advisor() falla, debe quedar una fila en
    el mecanismo existente de _log()/Sheets con los datos comerciales
    minimos para recuperar el lead manualmente."""
    sent, advisor_msgs, boardroom_calls, logged = _run_to_proposal(monkeypatch)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: False)
    vicky_app.handle(_text_msg("6684440000", "1", "m4"))
    vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
    vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))

    backup_rows = [call for call in logged if len(call) >= 4 and call[3] == "respaldo_lead"]
    assert len(backup_rows) == 1
    phone, nombre, resumen, tipo, origen, resultado = backup_rows[0][:6]
    assert phone == "6684440000"
    assert nombre == "Juan Perez"
    assert tipo == "respaldo_lead"
    assert resultado == "advisor_notify_failed"
    for campo in ("nombre=Juan Perez", "whatsapp=6684440000", "ciudad=Los Mochis",
                  "pension=12,000", "propuesta_monto=", "propuesta_cuota=",
                  "propuesta_plazo=", "origen=", "vrim_preeligible=True",
                  "vrim_offered=True", "advisor_notify_ok=False"):
        assert campo in resumen
    # Orden obligatorio: los campos criticos van antes que ciudad/origen.
    assert resumen.index("advisor_notify_ok=") < resumen.index("whatsapp=")
    assert resumen.index("whatsapp=") < resumen.index("nombre=")
    assert resumen.index("vrim_offered=") < resumen.index("ciudad=")
    assert resumen.index("vrim_offered=") < resumen.index("origen=")


def test_advisor_notify_success_does_not_write_backup_row(monkeypatch):
    sent, advisor_msgs, boardroom_calls, logged = _run_to_proposal(monkeypatch)
    vicky_app.handle(_text_msg("6684440000", "1", "m4"))
    vicky_app.handle(_text_msg("6684440000", "Juan Perez", "m5"))
    vicky_app.handle(_text_msg("6684440000", "Los Mochis", "m6"))
    backup_rows = [call for call in logged if len(call) >= 4 and call[3] == "respaldo_lead"]
    assert backup_rows == []


def test_backup_survives_real_500_char_truncation_with_long_fields(monkeypatch):
    """Bloqueante 1 (ronda 2): con nombre/ciudad/origen deliberadamente
    largos, los campos criticos deben sobrevivir incluso aplicando el
    truncamiento real de _log() (str(msg)[:500])."""
    sent, advisor_msgs, boardroom_calls, logged = _base_patches(monkeypatch)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: False)

    nombre_largo = "Juan " + "Perez " * 40  # muy por encima del cap individual
    ciudad_larga = "Los Mochis " * 20
    referral_largo = {
        "source_type": "ad", "source_id": "1",
        "headline": "Prestamo IMSS " + ("Ley 73 pensionados " * 20),
        "body": "conoce mas",
    }

    vicky_app.handle(_text_msg("6684440099", "Hello! Can I get more info?", "m1", referral=referral_largo))
    assert vicky_app.user_state.get("6684440099") == "imss_q_ley73"
    vicky_app.handle(_text_msg("6684440099", "1", "m2"))
    vicky_app.handle(_text_msg("6684440099", "12000", "m3"))
    vicky_app.handle(_text_msg("6684440099", "1", "m4"))
    vicky_app.handle(_text_msg("6684440099", nombre_largo, "m5"))
    vicky_app.handle(_text_msg("6684440099", ciudad_larga, "m6"))

    backup_rows = [call for call in logged if len(call) >= 4 and call[3] == "respaldo_lead"]
    assert len(backup_rows) == 1
    resumen_construido = backup_rows[0][2]

    # Aplica exactamente el truncamiento real de _log(): str(msg)[:500].
    resumen_truncado = str(resumen_construido)[:500]
    assert len(resumen_truncado) <= 500

    for campo in ("advisor_notify_ok=False", "whatsapp=", "nombre=",
                  "pension=", "propuesta_monto=", "propuesta_cuota=",
                  "propuesta_plazo=", "vrim_preeligible=", "vrim_offered="):
        assert campo in resumen_truncado, f"'{campo}' no sobrevivio al truncamiento real de 500"


def test_cta_scenario_a_vrim_sent_ok(monkeypatch):
    """Escenario A: la burbuja VRIM se envia correctamente."""
    sent, advisor_msgs, boardroom_calls, logged = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440021", "1", "m1"))
    vicky_app.handle(_text_msg("6684440021", "1", "m2"))
    vicky_app.handle(_text_msg("6684440021", "12000", "m3"))

    vrim_msgs = [s for s in sent if "VRIM Plus" in s[1]]
    assert len(vrim_msgs) == 1
    assert vrim_msgs[0][1].strip().endswith("2. No por ahora")
    fallback_msgs = [s for s in sent if s[1] == vicky_app._IMSS_REVISION_CTA_FALLBACK]
    assert fallback_msgs == []

    data = vicky_app.user_data.get("6684440021", {})
    assert data.get("vrim_offered") is True
    assert vicky_app.user_state.get("6684440021") == "imss_q_revision"
    backup_rows = [call for call in logged if len(call) >= 4 and call[3] == "respaldo_lead"]
    assert backup_rows == []


def test_cta_scenario_b_vrim_fails_fallback_succeeds(monkeypatch):
    """Escenario B: la burbuja VRIM falla, el CTA de respaldo se entrega."""
    sent, advisor_msgs, boardroom_calls, logged = _base_patches(monkeypatch)

    def flaky_send(to, text):
        if "VRIM Plus" in text:
            return False
        sent.append((to, text))
        return True

    monkeypatch.setattr(vicky_app, "send_msg", flaky_send)
    vicky_app.handle(_text_msg("6684440022", "1", "m1"))
    vicky_app.handle(_text_msg("6684440022", "1", "m2"))
    vicky_app.handle(_text_msg("6684440022", "12000", "m3"))

    fallback_msgs = [s for s in sent if s[1] == vicky_app._IMSS_REVISION_CTA_FALLBACK]
    assert len(fallback_msgs) == 1  # el fallback se invoca exactamente una vez, sin duplicar

    data = vicky_app.user_data.get("6684440022", {})
    assert data.get("vrim_offered") is not True
    assert vicky_app.user_state.get("6684440022") == "imss_q_revision"
    # No hay respaldo de doble fallo (el fallback si funciono).
    backup_rows = [call for call in logged if len(call) >= 4 and call[3] == "respaldo_lead"]
    assert backup_rows == []


def test_cta_scenario_c_vrim_and_fallback_both_fail(monkeypatch):
    """Escenario C: fallan tanto la burbuja VRIM como el CTA de respaldo."""
    sent, advisor_msgs, boardroom_calls, logged = _base_patches(monkeypatch)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: False)

    vicky_app.handle(_text_msg("6684440023", "1", "m1"))
    vicky_app.handle(_text_msg("6684440023", "1", "m2"))
    vicky_app.handle(_text_msg("6684440023", "12000", "m3"))

    data = vicky_app.user_data.get("6684440023", {})
    assert data.get("vrim_offered") is not True
    # No queda en imss_q_revision (nunca vio el CTA) ni se pierde el estado.
    assert vicky_app.user_state.get("6684440023") == "imss_cta_pendiente"
    # user_data se conserva (pension/propuesta ya calculados).
    assert data.get("pension") == 12000
    assert data.get("propuesta_monto")
    # Respaldo de doble fallo registrado, distinguible del fallo de notify_advisor.
    backup_rows = [call for call in logged if len(call) >= 4 and call[3] == "respaldo_lead"]
    assert len(backup_rows) == 1
    assert backup_rows[0][5] == "cta_send_failed"
    assert boardroom_calls == []

    # Reintento ante el siguiente mensaje del prospecto: si ahora el envio
    # funciona, avanza a imss_q_revision sin ir a Boardroom ni reiniciar.
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    vicky_app.handle(_text_msg("6684440023", "hola", "m4"))
    assert vicky_app.user_state.get("6684440023") == "imss_q_revision"
    assert boardroom_calls == []
    data = vicky_app.user_data.get("6684440023", {})
    assert data.get("pension") == 12000  # user_data sigue intacto tras el reintento


# ── No regresión de otros productos ────────────────────────────────────────────

def test_auto_funnel_unaffected(monkeypatch):
    sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440009", "2", "mid-auto"))
    assert "Seguro de Auto" in sent[0][1]
    assert vicky_app.user_state.get("6684440009") == "auto_q_tipo"


def test_vida_funnel_unaffected(monkeypatch):
    sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440010", "3", "mid-vida"))
    assert "Seguro de Vida" in sent[0][1]


def test_vrim_independent_funnel_unaffected(monkeypatch):
    sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440011", "4", "mid-vrim"))
    assert "Tarjeta Médica VRIM" in sent[0][1]
    assert vicky_app.user_state.get("6684440011") == "vrim_q_personas"


def test_empresarial_funnel_unaffected(monkeypatch):
    sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg("6684440012", "5", "mid-emp"))
    assert "Crédito Empresarial" in sent[0][1]


def test_sys_prompt_has_no_50000_references():
    assert "$50,000" not in vicky_app._SYS
    assert "$50k" not in vicky_app._SYS
    assert "$40,000" in vicky_app._SYS


# ── Webhook end-to-end (firma Meta valida) ─────────────────────────────────────

def test_webhook_returns_200_with_valid_signature(monkeypatch):
    monkeypatch.setattr(vicky_app, "APP_SECRET", "test-secret")
    monkeypatch.setattr(vicky_app, "handle", lambda msg_obj: None)
    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [
                        {"from": "6684440099", "id": "wamid.test1", "type": "text",
                         "text": {"body": "hola"}}
                    ]
                }
            }]
        }]
    }
    raw = json.dumps(body).encode("utf-8")
    sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    client = vicky_app.app.test_client()
    resp = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_webhook_rejects_without_app_secret(monkeypatch):
    monkeypatch.setattr(vicky_app, "APP_SECRET", "")
    client = vicky_app.app.test_client()
    resp = client.post("/webhook", data=b"{}", content_type="application/json")
    assert resp.status_code == 403


def test_no_motivo_prestamo_field_anywhere_in_module():
    import inspect
    src = inspect.getsource(vicky_app)
    for forbidden in ('"motivo_prestamo"', "'motivo_prestamo'",
                       '"destino_prestamo"', "'destino_prestamo'"):
        assert forbidden not in src
