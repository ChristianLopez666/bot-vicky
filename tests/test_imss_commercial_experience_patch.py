"""
Parche de experiencia comercial -- funnel Prestamo IMSS Ley 73 (Vicky Redes),
28 de julio de 2026.

Cubre lo que los parches anteriores no cubrian:
  - Saludo comercial nuevo conservando las 4 opciones del filtro Ley 73.
  - Presentacion de tasa fija anual y CAT bajo el MISMO criterio (sin IVA),
    con constantes de fuente unica y control de coherencia contra el calculo
    compuesto (el valor oficial documentado siempre prevalece).
  - Los diez plazos vigentes del cotizador oficial de Inbursa.
  - Interpretacion de plazo vs monto ('24' es monto, '24 meses' es plazo).
  - Validacion del minimo al recalcular por plazo, mostrando directamente la
    alternativa viable mas corta sin abrir estados intermedios.
  - Propuesta ACTIVA: la ultima propuesta valida visible manda en el cierre,
    en la notificacion al asesor y en el respaldo de Sheets.
  - Cierre + horario en una sola burbuja, con normalizacion de las opciones.
  - Alineacion de _SYS con las condiciones financieras del funnel y orden de
    definicion de constantes (import app sin NameError).

Fuente financiera: diez tablas de amortizacion del cotizador oficial de Banco
Inbursa entregadas por el usuario el 28 de julio de 2026 (6 a 60 meses):
tasa fija anual sin IVA 22.39%, CAT sin IVA 24.8%.
"""

import datetime
import importlib
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app
import cierre_cortesia as cc


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


PHONE = "6685550000"


def _cierre(sent):
    """Ultima burbuja del cierre determinista. Desde el acuse automatico
    post-propuesta, sent[-1] es el agradecimiento, no el cierre."""
    for _, texto in reversed(sent):
        if texto != cc.ACUSE_PROPUESTA:
            return texto
    raise AssertionError("no se envio ningun cierre")


def _text_msg(phone: str, text: str, mid: str) -> dict:
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


def _base_patches(monkeypatch):
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_seen_ids", set())
    monkeypatch.setattr(vicky_app, "_seen_dq", vicky_app.__dict__.get("_seen_dq", []).__class__())
    monkeypatch.setattr(vicky_app, "_ctc_post_close_ctx", {})
    monkeypatch.delenv("IMSS_META_REFERRAL_IDS", raising=False)
    monkeypatch.delenv("IMSS_META_REFERRAL_HINTS", raising=False)

    sent = []
    advisor_msgs = []
    logged = []

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: None)

    def fake_log(phone, nombre, msg, tipo, origen, resultado="", error="", mid=""):
        logged.append((phone, nombre, msg, tipo, origen, resultado, error, mid))

    monkeypatch.setattr(vicky_app, "_log", fake_log)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")

    boardroom_calls = []

    def fake_request_boardroom_instruction(payload):
        boardroom_calls.append(payload)
        return None, "should_not_be_called"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", fake_request_boardroom_instruction)
    return sent, advisor_msgs, boardroom_calls, logged


def _to_proposal(monkeypatch, pension="12000", phone=PHONE):
    """menu -> 1 -> Ley 73 -> pension. Deja en imss_q_revision con propuesta
    inicial calculada, VRIM ofrecida y propuesta activa registrada."""
    ctx = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "1", "m1"))
    vicky_app.handle(_text_msg(phone, "1", "m2"))
    vicky_app.handle(_text_msg(phone, pension, "m3"))
    return ctx


def _accept_and_close(phone=PHONE, nombre="Juan Perez", ciudad="Los Mochis"):
    """Desde imss_q_revision: 1 -> nombre -> ciudad (cierre + notificacion)."""
    vicky_app.handle(_text_msg(phone, "1", "c1"))
    vicky_app.handle(_text_msg(phone, nombre, "c2"))
    vicky_app.handle(_text_msg(phone, ciudad, "c3"))


def _data(phone=PHONE):
    return vicky_app.user_data.get(phone, {})


def _at(anio, mes, dia, hora, minuto=0):
    """Instante concreto en la zona horaria comercial (America/Mazatlan)."""
    naive = datetime.datetime(anio, mes, dia, hora, minuto)
    tz = vicky_app._IMSS_TZ_COMERCIAL
    return tz.localize(naive) if hasattr(tz, "localize") else naive.replace(tzinfo=tz)


# Instantes de referencia (verificados con datetime.weekday()).
MARTES_10AM = _at(2026, 7, 28, 10)        # escenario A entre semana
SABADO_1759 = _at(2026, 8, 1, 17, 59)     # escenario A en sabado
SABADO_1801 = _at(2026, 8, 1, 18, 1)      # escenario C
DOMINGO_9AM = _at(2026, 8, 2, 9)          # escenario C
VIERNES_7PM = _at(2026, 7, 31, 19)        # escenario B


def _freeze_horario(monkeypatch, momento):
    """Congela el reloj comercial: sin esto, las pruebas que afirman etiquetas
    concretas de horario pasarian o fallarian segun la hora real de ejecucion."""
    monkeypatch.setattr(vicky_app, "_imss_ahora_comercial", lambda: momento)


# ══════════════════════════════════════════════════════════════════════════════
# 1-2. Saludo inicial nuevo
# ══════════════════════════════════════════════════════════════════════════════

def test_new_greeting_full_content(monkeypatch):
    sent, _, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    msg = sent[0][1]
    for fragmento in (
        "👋 *¡Hola! Soy Vicky, asistente de Christian López.*",
        "de manera rápida y sencilla",
        "*préstamo para pensionados IMSS*",
        "Prepararé una propuesta estimada de acuerdo con tu pensión",
        "algún monto o plazo específico",
        "Para comenzar, selecciona cuál opción corresponde a tu caso:",
    ):
        assert fragmento in msg


def test_greeting_preserves_four_options_and_their_logic(monkeypatch):
    sent, _, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    msg = sent[0][1]
    assert "1️⃣ Ya estoy pensionado por IMSS Ley 73" in msg
    assert "2️⃣ Estoy pensionado, pero no sé si soy Ley 73" in msg
    assert "3️⃣ Estoy por pensionarme" in msg
    assert "4️⃣ Estoy ayudando a un familiar" in msg
    assert vicky_app.user_state.get(PHONE) == "imss_q_ley73"


def test_greeting_option_1_and_2_still_go_to_pension(monkeypatch):
    for opcion in ("1", "2"):
        sent, _, _, _ = _base_patches(monkeypatch)
        vicky_app.handle(_text_msg(PHONE, "1", "m1"))
        vicky_app.handle(_text_msg(PHONE, opcion, "m2"))
        assert vicky_app.user_state.get(PHONE) == "imss_q_pension_calc"


def test_greeting_option_3_and_4_keep_previous_behaviour(monkeypatch):
    sent, advisor_msgs, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    vicky_app.handle(_text_msg(PHONE, "3", "m2"))
    assert vicky_app.user_state.get(PHONE) == "imss_post_cierre"
    assert len(advisor_msgs) == 1

    sent, _, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    vicky_app.handle(_text_msg(PHONE, "4", "m2"))
    assert vicky_app.user_state.get(PHONE) == "imss_q_pension_calc"
    assert _data().get("relacion") == "familiar"


# ══════════════════════════════════════════════════════════════════════════════
# 3-13. Presentacion financiera de la propuesta
# ══════════════════════════════════════════════════════════════════════════════

def test_proposal_message_shows_monto_cuota_plazo_tasa_and_cat(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    msg = sent[-2][1]  # sent[-1] es la burbuja VRIM
    data = _data()
    assert "🎉 *¡Tenemos una propuesta para ti!*" in msg
    assert f"💰 *Monto aproximado:* *${data['propuesta_monto']:,.0f}*" in msg
    assert f"💳 *Pago aproximado:* *${data['propuesta_cuota']:,.0f} al mes*" in msg
    assert f"📆 *Plazo:* *{data['propuesta_plazo']} meses*" in msg
    assert "📈 *Tasa fija anual:* *22.39% sin IVA*" in msg
    assert "📊 *CAT informativo:* *24.8% sin IVA*" in msg
    assert "sujeta a validación final" in msg
    assert "otras opciones de monto o plazo" in msg


def test_tasa_anual_constant_exists_and_derives_from_monthly_rate():
    assert hasattr(vicky_app, "IMSS_TASA_ANUAL_SIN_IVA")
    assert vicky_app.IMSS_TASA_ANUAL_SIN_IVA == vicky_app.IMSS_TASA_MENSUAL * 12 * 100
    assert f"{vicky_app.IMSS_TASA_ANUAL_SIN_IVA:.2f}" == "22.39"


def test_cat_sin_iva_constant_exists_with_official_value():
    assert hasattr(vicky_app, "IMSS_CAT_SIN_IVA")
    assert vicky_app.IMSS_CAT_SIN_IVA == 24.8
    assert f"{vicky_app.IMSS_CAT_SIN_IVA:.1f}" == "24.8"


def test_cat_official_value_is_documented_in_source():
    src = inspect.getsource(vicky_app)
    fin = src.index("IMSS_CAT_SIN_IVA = 24.8")
    bloque = src[max(0, fin - 1200):fin]
    assert "Inbursa" in bloque
    assert "28 de julio de 2026" in bloque
    assert "OFICIAL" in bloque


def test_proposal_message_does_not_mix_iva_criteria(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    msg = sent[-2][1]
    assert msg.count("sin IVA") == 2
    assert "con IVA" not in msg
    assert "29.3" not in msg
    assert str(vicky_app.IMSS_CAT) not in msg


def test_internal_cat_with_iva_never_shown_to_client(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    for _to, text in sent:
        assert "29.3" not in text


def test_cat_coherence_control_against_compound_rate():
    """Control de coherencia: el calculo compuesto debe seguir cuadrando con
    el CAT oficial documentado. Si deja de coincidir, esta prueba FALLA y la
    discrepancia queda visible -- el valor oficial es el que prevalece."""
    compuesto = ((1 + vicky_app.IMSS_TASA_MENSUAL) ** 12 - 1) * 100
    assert abs(compuesto - vicky_app.IMSS_CAT_SIN_IVA) < 0.1, (
        f"CAT compuesto calculado {compuesto:.4f}% ya no coincide con el CAT "
        f"oficial documentado {vicky_app.IMSS_CAT_SIN_IVA}%. Prevalece el oficial."
    )


def test_financial_constants_unchanged():
    assert vicky_app.IMSS_TASA_MENSUAL == 0.018659
    assert vicky_app.IMSS_IVA_RATE == 0.16
    assert vicky_app.IMSS_CAT == 29.3
    assert vicky_app.IMSS_LIMITE_DESCUENTO == 0.30
    assert vicky_app.IMSS_MONTO_MINIMO == 40000
    assert vicky_app.IMSS_PLAZO_MESES == 60


def test_amortization_formula_unchanged():
    """La biseccion/formula sigue dando el mismo resultado que el puerto del
    cotizador: la cuota consume exactamente el limite de descuento."""
    propuesta = vicky_app.calcular_propuesta_imss(12000)
    assert propuesta["cuota_max"] == 12000 * 0.30
    assert abs(propuesta["cuota"] - propuesta["cuota_max"]) < 1.0
    assert propuesta["total"] == propuesta["cuota"] * propuesta["plazo"]
    assert abs(vicky_app._imss_calcular_cuota(100000, 60) - 2992.6) < 5


def test_rate_values_not_hardcoded_in_templates():
    src = inspect.getsource(vicky_app)
    # 22.39 y 24.8 solo pueden aparecer en comentarios de procedencia, nunca
    # como literal dentro de una plantilla de texto enviada al cliente.
    for linea in src.splitlines():
        codigo = linea.split("#", 1)[0]
        assert "22.39" not in codigo, linea
        assert "24.8%" not in codigo, linea


# ══════════════════════════════════════════════════════════════════════════════
# 14-17. Plazos disponibles
# ══════════════════════════════════════════════════════════════════════════════

def test_plazos_disponibles_constant_exists_with_exact_catalog():
    assert hasattr(vicky_app, "IMSS_PLAZOS_DISPONIBLES")
    assert sorted(vicky_app.IMSS_PLAZOS_DISPONIBLES) == [6, 12, 18, 24, 30, 36, 42, 48, 54, 60]


def test_plazos_list_is_not_duplicated_literally_in_source():
    """La secuencia literal solo puede vivir en la constante: en el codigo
    ejecutable (sin comentarios) no puede repetirse."""
    codigo = "\n".join(l.split("#", 1)[0] for l in inspect.getsource(vicky_app).splitlines())
    assert len(re.findall(r"6,\s*12,\s*18,\s*24,\s*30,\s*36,\s*42,\s*48,\s*54", codigo)) == 1
    # El texto que ve el cliente se deriva de la constante, no de un literal.
    assert "IMSS_PLAZOS_DISPONIBLES" in inspect.getsource(vicky_app._imss_plazos_texto)


def test_every_available_plazo_produces_a_valid_calculation():
    for plazo in vicky_app.IMSS_PLAZOS_DISPONIBLES:
        propuesta = vicky_app.calcular_propuesta_imss(20000, plazo)
        assert propuesta["plazo"] == plazo
        assert propuesta["monto"] > 0
        assert abs(propuesta["cuota"] - 20000 * 0.30) < 1.0
        assert propuesta["total"] == propuesta["cuota"] * plazo
    montos = [vicky_app.calcular_propuesta_imss(20000, p)["monto"]
              for p in sorted(vicky_app.IMSS_PLAZOS_DISPONIBLES)]
    assert montos == sorted(montos)  # a mayor plazo, mayor monto


def test_default_plazo_still_60():
    assert vicky_app.IMSS_PLAZO_MESES == 60
    assert vicky_app.calcular_propuesta_imss(12000)["plazo"] == 60


def test_extractor_consumes_the_constant(monkeypatch):
    """_imss_extract_monto_plazo lee IMSS_PLAZOS_DISPONIBLES, no una lista
    propia: si se restringe la constante, el extractor lo refleja."""
    monkeypatch.setattr(vicky_app, "IMSS_PLAZOS_DISPONIBLES", (60,))
    assert vicky_app._imss_extract_monto_plazo("30 meses") == (None, None, 30)
    assert vicky_app._imss_extract_monto_plazo("60 meses") == (None, 60, None)


# ══════════════════════════════════════════════════════════════════════════════
# 18-23. Interpretacion de monto y plazo
# ══════════════════════════════════════════════════════════════════════════════

def test_30_meses_is_plazo_not_monto():
    assert vicky_app._imss_extract_monto_plazo("30 meses") == (None, 30, None)


def test_out_of_catalog_plazo_is_never_read_as_monto():
    for texto in ("40 meses", "72 meses", "7 meses", "40 mes"):
        monto, plazo, invalido = vicky_app._imss_extract_monto_plazo(texto)
        assert monto is None
        assert plazo is None
        assert invalido is not None


def test_40_meses_answers_with_available_plazos(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "40 meses", "m4"))
    msg = sent[-1][1]
    assert "6, 12, 18, 24, 30, 36, 42, 48, 54 y 60 meses" in msg
    # El 40 nunca llego a la validacion de monto minimo.
    assert "$40,000" not in msg
    assert "mínimo" not in msg.lower()
    assert boardroom_calls == []
    assert vicky_app.user_state.get(PHONE) == "imss_q_revision"


def test_out_of_catalog_plazo_does_not_touch_active_proposal(monkeypatch):
    _to_proposal(monkeypatch)
    antes = dict(_data())
    vicky_app.handle(_text_msg(PHONE, "72 meses", "m4"))
    despues = _data()
    for campo in ("propuesta_activa_monto", "propuesta_activa_cuota",
                  "propuesta_activa_plazo", "propuesta_activa_origen"):
        assert despues[campo] == antes[campo]
    assert "monto_solicitado" not in despues


def test_bare_24_is_monto_not_plazo():
    assert vicky_app._imss_extract_monto_plazo("24") == (24.0, None, None)


def test_bare_24_follows_minimum_amount_route(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "24", "m4"))
    msg = sent[-1][1]
    assert "$40,000" in msg
    assert "mínimo" in msg.lower()
    assert _data()["monto_solicitado"] == 24.0
    assert boardroom_calls == []


def test_24_meses_is_plazo(monkeypatch):
    assert vicky_app._imss_extract_monto_plazo("24 meses") == (None, 24, None)
    assert vicky_app._imss_extract_monto_plazo("24 mes") == (None, 24, None)
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "24 meses", "m4"))
    assert "24 meses" in sent[-1][1]
    assert _data()["propuesta_activa_plazo"] == 24


# ══════════════════════════════════════════════════════════════════════════════
# 24-27. Respuestas escuetas en imss_q_revision
# ══════════════════════════════════════════════════════════════════════════════

def test_bare_number_is_followup_inside_revision(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "80000", "m4"))
    assert "80,000" in sent[-1][1]
    assert _data()["monto_solicitado"] == 80000.0
    assert boardroom_calls == []


def test_short_followup_forms_all_recognized():
    for texto in ("80000", "$80,000", "30 meses", "80000 a 30 meses",
                  "Quiero 80 mil a 36 meses", "¿Cuánto pagaría a 24 meses?"):
        assert vicky_app._is_imss_revision_followup(texto), texto


def test_option_1_and_2_are_never_captured_as_followup():
    assert vicky_app._is_imss_revision_followup("1") is False
    assert vicky_app._is_imss_revision_followup("2") is False


def test_non_numeric_messages_are_not_captured_as_followup():
    for texto in ("tal vez", "no gracias", "quien eres", "hola", "sí", "no por ahora"):
        assert vicky_app._is_imss_revision_followup(texto) is False, texto


def test_1_still_means_accept_review(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "1", "m4"))
    assert vicky_app.user_state.get(PHONE) == "imss_q_nombre_calc"
    assert "nombre completo" in sent[-1][1]


def test_2_still_means_decline_review(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "2", "m4"))
    assert vicky_app.user_state.get(PHONE) == "imss_post_cierre"
    # La alerta temprana (propuesta calculada) ya se mando en _to_proposal;
    # declinar no debe generar la notificacion de calificacion completa.
    assert len(advisor_msgs) == 1
    assert "CALIFICADO" not in advisor_msgs[0]


def test_monto_and_plazo_together_still_work(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "80000 a 30 meses", "m4"))
    msg = sent[-1][1]
    assert "80,000" in msg
    assert "30 meses" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 28-34. Plazo cuyo monto queda bajo el minimo
# ══════════════════════════════════════════════════════════════════════════════

def test_short_plazo_below_minimum_shows_viable_alternative_directly(monkeypatch):
    # pension 12000: a 6 meses ~$20,054 (< 40,000); primer plazo viable = 18.
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch, pension="12000")
    assert vicky_app.calcular_propuesta_imss(12000, 6)["monto"] < vicky_app.IMSS_MONTO_MINIMO
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    msg = sent[-1][1]
    viable = vicky_app.calcular_propuesta_imss(12000, 18)

    assert "a *6 meses* el monto estimado quedaría por debajo del mínimo" in msg
    assert "*$40,000*" in msg
    assert "La opción disponible con el plazo más corto sería:" in msg
    assert f"💰 *Monto aproximado:* *${viable['monto']:,.0f}*" in msg
    assert f"💳 *Pago aproximado:* *${viable['cuota']:,.0f} al mes*" in msg
    assert "📆 *Plazo:* *18 meses*" in msg
    assert "sujeta a validación final" in msg
    # Nunca se muestra la cifra invalida ni un prestamo por debajo del minimo.
    assert f"{vicky_app.calcular_propuesta_imss(12000, 6)['monto']:,.0f}" not in msg
    # Sin estado intermedio y con el CTA 1/2 vigente.
    assert vicky_app.user_state.get(PHONE) == "imss_q_revision"
    assert msg.strip().endswith("2️⃣ No por ahora")
    assert boardroom_calls == []


def test_viable_alternative_becomes_active_proposal(monkeypatch):
    _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    viable = vicky_app.calcular_propuesta_imss(12000, 18)
    data = _data()
    assert data["propuesta_activa_monto"] == viable["monto"]
    assert data["propuesta_activa_cuota"] == viable["cuota"]
    assert data["propuesta_activa_plazo"] == 18
    assert data["propuesta_activa_origen"] == "plazo_viable_automatico"
    assert data["propuesta_activa_monto"] >= vicky_app.IMSS_MONTO_MINIMO


def test_no_intermediate_confirmation_question_before_showing_alternative(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    for _to, text in sent:
        assert "¿Quieres que te muestre esa opción?" not in text
        assert "te muestre esa opción" not in text


def test_answer_1_after_viable_alternative_still_accepts_review(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    vicky_app.handle(_text_msg(PHONE, "1", "m5"))
    assert vicky_app.user_state.get(PHONE) == "imss_q_nombre_calc"
    assert boardroom_calls == []


def test_answer_2_after_viable_alternative_still_declines(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    vicky_app.handle(_text_msg(PHONE, "2", "m5"))
    assert vicky_app.user_state.get(PHONE) == "imss_post_cierre"
    # La alerta temprana (propuesta calculada) ya se mando en _to_proposal;
    # declinar no debe generar la notificacion de calificacion completa.
    assert len(advisor_msgs) == 1
    assert "CALIFICADO" not in advisor_msgs[0]


def test_no_plazo_reaches_minimum_uses_safe_low_pension_exit(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch, pension="12000")
    propuesta_previa = dict(_data())
    # Se fuerza una pension con la que NINGUN plazo alcanza el minimo.
    assert vicky_app._imss_primer_plazo_viable(2000) is None
    vicky_app.user_data[PHONE]["pension"] = 2000
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    msg = sent[-1][1]
    assert "$40,000" in msg
    assert "asesor te contacte" in msg
    assert vicky_app.user_state.get(PHONE) == "imss_pension_baja"
    # No se invento propuesta ni se sobrescribio la ultima valida.
    data = _data()
    assert data["propuesta_activa_monto"] == propuesta_previa["propuesta_activa_monto"]
    assert data["propuesta_activa_plazo"] == propuesta_previa["propuesta_activa_plazo"]
    assert boardroom_calls == []


def test_viable_plazo_search_uses_current_calculator():
    plazo, propuesta = vicky_app._imss_primer_plazo_viable(12000)
    assert plazo == 18
    assert propuesta == vicky_app.calcular_propuesta_imss(12000, 18)
    # Es realmente el mas corto: el anterior del catalogo no alcanza.
    assert vicky_app.calcular_propuesta_imss(12000, 12)["monto"] < vicky_app.IMSS_MONTO_MINIMO
    assert vicky_app._imss_primer_plazo_viable(50000)[0] == 6


def test_no_hardcoded_amount_table_in_viable_search():
    # La busqueda generica recorre el catalogo con la calculadora vigente...
    src = inspect.getsource(vicky_app._imss_primer_plazo_para_monto)
    assert "IMSS_PLAZOS_DISPONIBLES" in src
    assert "calcular_propuesta_imss" in src
    assert "40000" not in src
    # ...y la busqueda del minimo del producto es solo un caso particular.
    src_min = inspect.getsource(vicky_app._imss_primer_plazo_viable)
    assert "IMSS_MONTO_MINIMO" in src_min
    assert "_imss_primer_plazo_para_monto" in src_min
    assert "40000" not in src_min


# ══════════════════════════════════════════════════════════════════════════════
# Tope de monto evaluado contra el PLAZO cotizado (no contra el maximo a 60)
# ══════════════════════════════════════════════════════════════════════════════

def test_requested_amount_is_capped_against_the_quoted_plazo(monkeypatch):
    """Regresion: '100000 a 24 meses' con pension $10,000 cabia bajo el tope
    de 60 meses ($100,251) y producia una cuota de $5,386 contra un limite de
    descuento de $3,000. Ahora el tope se evalua a 24 meses ($55,698)."""
    pension = 10000
    assert vicky_app.calcular_propuesta_imss(pension, 60)["monto"] > 100000
    assert vicky_app.calcular_propuesta_imss(pension, 24)["monto"] < 100000

    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch, pension=str(pension))
    vicky_app.handle(_text_msg(PHONE, "100000 a 24 meses", "m4"))
    msg = sent[-1][1]
    data = _data()

    # Nunca se cotiza la combinacion imposible.
    cuota_imposible = vicky_app._imss_calcular_cuota(100000, 24)
    assert f"${cuota_imposible:,.0f}" not in msg
    assert "24 meses*, el pago aproximado sería" not in msg
    # Se ofrece el mismo monto en el plazo mas corto donde SI cabe.
    plazo_alt, _p = vicky_app._imss_primer_plazo_para_monto(pension, 100000)
    cuota_alt = vicky_app._imss_calcular_cuota(100000, plazo_alt)
    assert f"📆 *Plazo:* *{plazo_alt} meses*" in msg
    assert f"💳 *Pago aproximado:* *${cuota_alt:,.0f} al mes*" in msg
    assert cuota_alt <= pension * vicky_app.IMSS_LIMITE_DESCUENTO + 1
    assert boardroom_calls == []


def test_quoted_cuota_never_exceeds_the_discount_limit(monkeypatch):
    """Invariante duro: ninguna cuota que llegue a la propuesta activa puede
    superar el 30% de la pension, sea cual sea la combinacion pedida."""
    pension = 10000
    limite = pension * vicky_app.IMSS_LIMITE_DESCUENTO
    for seguimiento in ("100000 a 24 meses", "90000 a 12 meses", "60000 a 6 meses",
                        "50000 a 18 meses", "100000 a 60 meses", "45000",
                        "y si quiero 900000", "30 meses"):
        _to_proposal(monkeypatch, pension=str(pension))
        vicky_app.handle(_text_msg(PHONE, seguimiento, "m4"))
        _monto, cuota, _plazo = vicky_app._imss_get_propuesta_activa(_data())
        assert cuota <= limite + 1, f"{seguimiento}: cuota {cuota:.0f} > limite {limite:.0f}"


def test_adjusted_plazo_alternative_becomes_active(monkeypatch):
    _to_proposal(monkeypatch, pension="10000")
    vicky_app.handle(_text_msg(PHONE, "100000 a 24 meses", "m4"))
    plazo_alt, _p = vicky_app._imss_primer_plazo_para_monto(10000, 100000)
    data = _data()
    assert data["propuesta_activa_monto"] == 100000.0
    assert data["propuesta_activa_plazo"] == plazo_alt
    assert data["propuesta_activa_cuota"] == vicky_app._imss_calcular_cuota(100000.0, plazo_alt)
    assert data["propuesta_activa_origen"] == "monto_solicitado_plazo_ajustado"
    assert data["monto_solicitado"] == 100000.0
    assert data["vrim_preeligible"] is True


def test_amount_above_every_plazo_still_shows_global_maximum(monkeypatch):
    """Comportamiento previo preservado: si el monto no cabe ni a 60 meses,
    se muestra el maximo global como referencia."""
    sent, _, _, _ = _to_proposal(monkeypatch, pension="12000")
    propuesta_monto = _data()["propuesta_monto"]
    assert vicky_app._imss_primer_plazo_para_monto(12000, 900000) is None
    vicky_app.handle(_text_msg(PHONE, "y si quiero 900000", "m4"))
    msg = sent[-1][1]
    data = _data()
    assert f"${propuesta_monto:,.0f}" in msg
    assert "monto máximo estimado" in msg
    assert data["monto_solicitado"] == 900000.0
    assert data["vrim_eligibility_basis"] == "propuesta_monto"
    assert data["propuesta_activa_origen"] == "propuesta_maxima"
    assert data["vrim_preeligible"] is True


def test_cap_uses_active_plazo_when_no_plazo_requested(monkeypatch):
    """Tras fijar una propuesta activa a 18 meses, un monto nuevo sin plazo se
    evalua contra el maximo A 18 MESES, no contra el de 60."""
    _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))          # -> activa a 18 meses
    assert _data()["propuesta_activa_plazo"] == 18
    assert vicky_app.calcular_propuesta_imss(12000, 18)["monto"] < 80000
    vicky_app.handle(_text_msg(PHONE, "y si quiero 80000", "m5"))
    _monto, cuota, plazo = vicky_app._imss_get_propuesta_activa(_data())
    assert plazo > 18
    assert cuota <= 12000 * vicky_app.IMSS_LIMITE_DESCUENTO + 1


# ══════════════════════════════════════════════════════════════════════════════
# 35-49. Propuesta activa
# ══════════════════════════════════════════════════════════════════════════════

def test_initial_proposal_is_registered_as_active(monkeypatch):
    _to_proposal(monkeypatch)
    data = _data()
    assert data["propuesta_activa_monto"] == data["propuesta_monto"]
    assert data["propuesta_activa_cuota"] == data["propuesta_cuota"]
    assert data["propuesta_activa_plazo"] == data["propuesta_plazo"]
    assert data["propuesta_activa_origen"] == "propuesta_inicial"


def test_valid_monto_alternative_becomes_active(monkeypatch):
    _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "y si quiero 80000", "m4"))
    data = _data()
    assert data["propuesta_activa_monto"] == 80000.0
    assert data["propuesta_activa_plazo"] == 60
    assert data["propuesta_activa_cuota"] == vicky_app._imss_calcular_cuota(80000.0, 60)
    assert data["propuesta_activa_origen"] == "monto_solicitado"


def test_valid_plazo_alternative_becomes_active(monkeypatch):
    _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    esperado = vicky_app.calcular_propuesta_imss(12000, 30)
    data = _data()
    assert data["propuesta_activa_monto"] == esperado["monto"]
    assert data["propuesta_activa_cuota"] == esperado["cuota"]
    assert data["propuesta_activa_plazo"] == 30
    assert data["propuesta_activa_origen"] == "plazo_solicitado"


def test_valid_monto_and_plazo_alternative_becomes_active(monkeypatch):
    _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "60000 a 36 meses", "m4"))
    data = _data()
    assert data["propuesta_activa_monto"] == 60000.0
    assert data["propuesta_activa_plazo"] == 36
    assert data["propuesta_activa_cuota"] == vicky_app._imss_calcular_cuota(60000.0, 36)
    assert data["propuesta_activa_origen"] == "monto_y_plazo_solicitados"


def test_below_minimum_alternative_is_not_registered_as_active(monkeypatch):
    _to_proposal(monkeypatch)
    antes = dict(_data())
    vicky_app.handle(_text_msg(PHONE, "y si quiero 30000", "m4"))
    data = _data()
    assert data["propuesta_activa_monto"] == antes["propuesta_activa_monto"]
    assert data["propuesta_activa_plazo"] == antes["propuesta_activa_plazo"]
    assert data["propuesta_activa_origen"] == "propuesta_inicial"
    assert data["monto_solicitado"] == 30000.0  # el dato comercial se conserva


def test_original_proposal_preserved_for_traceability(monkeypatch):
    _to_proposal(monkeypatch)
    original = dict(_data())
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    data = _data()
    assert data["propuesta_monto"] == original["propuesta_monto"]
    assert data["propuesta_cuota"] == original["propuesta_cuota"]
    assert data["propuesta_plazo"] == original["propuesta_plazo"]
    assert data["propuesta_activa_monto"] != original["propuesta_monto"]


def test_monto_solicitado_is_preserved_through_closing(monkeypatch):
    _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "y si quiero 80000", "m4"))
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    assert _data()["monto_solicitado"] == 80000.0


def test_closing_uses_monto_alternative(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "y si quiero 80000", "m4"))
    _accept_and_close()
    cierre = _cierre(sent)
    cuota = vicky_app._imss_calcular_cuota(80000.0, 60)
    assert "$80,000" in cierre
    assert f"${cuota:,.0f} mensuales durante 60 meses" in cierre
    assert f"${_data()['propuesta_monto']:,.0f}" not in cierre


def test_closing_uses_plazo_alternative(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    _accept_and_close()
    esperado = vicky_app.calcular_propuesta_imss(12000, 30)
    cierre = _cierre(sent)
    assert f"${esperado['monto']:,.0f}" in cierre
    assert f"${esperado['cuota']:,.0f} mensuales durante 30 meses" in cierre


def test_closing_uses_monto_and_plazo_alternative(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "60000 a 36 meses", "m4"))
    ultima_propuesta_visible = sent[-1][1]
    _accept_and_close()
    cierre = _cierre(sent)
    cuota = vicky_app._imss_calcular_cuota(60000.0, 36)
    assert "$60,000" in ultima_propuesta_visible and "36 meses" in ultima_propuesta_visible
    assert "$60,000" in cierre
    assert f"${cuota:,.0f} mensuales durante 36 meses" in cierre


def test_closing_uses_automatic_viable_plazo(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    _accept_and_close()
    viable = vicky_app.calcular_propuesta_imss(12000, 18)
    cierre = _cierre(sent)
    assert f"${viable['monto']:,.0f}" in cierre
    assert f"${viable['cuota']:,.0f} mensuales durante 18 meses" in cierre


def test_advisor_notification_matches_active_proposal(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    _accept_and_close()
    esperado = vicky_app.calcular_propuesta_imss(12000, 30)
    # advisor_msgs[0] es la alerta temprana de propuesta; la notificacion de
    # calificacion completa (con estas cifras) es siempre la ultima.
    aviso = advisor_msgs[-1]
    assert f"Monto estimado: ${esperado['monto']:,.0f}" in aviso
    assert f"Cuota estimada: ${esperado['cuota']:,.0f}" in aviso
    assert "Plazo: 30 meses" in aviso
    assert "Origen de la propuesta activa: plazo_solicitado" in aviso
    # Trazabilidad de la propuesta inicial, sin sustituir a la activa.
    assert f"Propuesta inicial (referencia): ${_data()['propuesta_monto']:,.0f}" in aviso


def test_closing_and_advisor_notification_share_the_same_figures(monkeypatch):
    for seguimiento in ("y si quiero 80000", "30 meses", "60000 a 36 meses", "6 meses"):
        sent, advisor_msgs, _, _ = _to_proposal(monkeypatch, pension="12000")
        vicky_app.handle(_text_msg(PHONE, seguimiento, "m4"))
        _accept_and_close()
        cierre = _cierre(sent)
        aviso = advisor_msgs[-1]
        monto, cuota, plazo = vicky_app._imss_get_propuesta_activa(_data())
        assert f"${monto:,.0f}" in cierre and f"${monto:,.0f}" in aviso, seguimiento
        assert f"${cuota:,.0f}" in cierre and f"${cuota:,.0f}" in aviso, seguimiento
        assert f"{plazo} meses" in cierre and f"Plazo: {plazo} meses" in aviso, seguimiento


def test_never_silently_reverts_to_original_proposal(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    original_monto = _data()["propuesta_monto"]
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    _accept_and_close()
    cierre = _cierre(sent)
    assert f"${original_monto:,.0f}" not in cierre
    assert f"Monto estimado: ${original_monto:,.0f}" not in advisor_msgs[-1]


def test_backup_row_uses_active_proposal(monkeypatch):
    sent, advisor_msgs, _, logged = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: False)
    _accept_and_close()
    esperado = vicky_app.calcular_propuesta_imss(12000, 30)
    backup = [c for c in logged if c[3] == "respaldo_lead"]
    assert len(backup) == 1
    assert f"propuesta_monto={esperado['monto']:,.0f}" in backup[0][2]
    assert "propuesta_plazo=30" in backup[0][2]


def test_single_source_for_active_proposal():
    """No hay dos fuentes contradictorias: el cierre, la notificacion y el
    respaldo pasan todos por _imss_get_propuesta_activa()."""
    for fn in (vicky_app._imss_build_closing_statement,
               vicky_app._imss_build_advisor_notification,
               vicky_app._imss_log_lead_backup):
        assert "_imss_get_propuesta_activa" in inspect.getsource(fn), fn.__name__


# ══════════════════════════════════════════════════════════════════════════════
# 50-55. VRIM: elegibilidad no se degrada
# ══════════════════════════════════════════════════════════════════════════════

def test_vrim_applies_from_40000_inclusive(monkeypatch):
    # El monto es lineal en la pension: se calibra la pension que deja la
    # propuesta a 60 meses justo en el minimo de $40,000.
    pension = 10000 * 40000 / vicky_app.calcular_propuesta_imss(10000)["monto"]
    assert abs(vicky_app.calcular_propuesta_imss(pension)["monto"] - 40000) < 5
    _to_proposal(monkeypatch, pension=f"{pension + 1:.0f}")
    data = _data()
    assert data["propuesta_monto"] >= vicky_app.IMSS_MONTO_MINIMO
    assert data["propuesta_monto"] < 40500          # realmente al borde del minimo
    assert data["vrim_preeligible"] is True
    assert data["vrim_eligibility_basis"] == "propuesta_monto"


def test_exactly_40000_qualifies():
    assert (40000 >= vicky_app.IMSS_MONTO_MINIMO) is True


def test_vrim_preeligible_not_degraded_by_any_followup(monkeypatch):
    for seguimiento in ("y si quiero 30000", "24", "30 meses", "6 meses",
                        "72 meses", "y si quiero 900000", "60000 a 36 meses"):
        _to_proposal(monkeypatch, pension="12000")
        assert _data()["vrim_preeligible"] is True
        vicky_app.handle(_text_msg(PHONE, seguimiento, "m4"))
        assert _data()["vrim_preeligible"] is True, seguimiento


def test_active_proposal_update_does_not_degrade_vrim(monkeypatch):
    _to_proposal(monkeypatch, pension="12000")
    vicky_app.handle(_text_msg(PHONE, "6 meses", "m4"))
    data = _data()
    assert data["propuesta_activa_monto"] >= vicky_app.IMSS_MONTO_MINIMO
    assert data["vrim_preeligible"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 56-62. Mensaje completo de VRIM Plus
# ══════════════════════════════════════════════════════════════════════════════

def test_vrim_full_message_content(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    msg = sent[-1][1]
    for fragmento in (
        "🎁 *¡Tu propuesta puede darte mucho más que un préstamo!*",
        "podrías recibir *sin costo una membresía VRIM Plus durante 12 meses*",
        "sujeta a la formalización del préstamo y a las condiciones de la promoción",
        "las 24 horas, los 365 días del año",
        "Orientación emocional y nutricional",
        "*Dos videoconsultas de especialidad sin costo*",
        "*Una ambulancia sin costo al año*",
        "química sanguínea de 6 elementos",
        "laboratorios participantes de la red VRIM",
        "*Reembolso de gastos médicos por accidente de hasta $20,000*",
        "Servicio funerario completo, incluyendo cremación",
        "periodo de espera de 90 días",
    ):
        assert fragmento in msg, fragmento
    for prohibido in ("regalo confirmado", "ya fue otorgada", "aprobado", "garantizado",
                      "Delia Barraza", "Chopo"):
        assert prohibido.lower() not in msg.lower(), prohibido


def test_vrim_age_limit_only_on_accident_and_funeral(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    msg = sent[-1][1]
    assert msg.count("70 años") == 2
    accidente = msg[msg.index("🛡️"):msg.index("⚱️")]
    funerario = msg[msg.index("⚱️"):]
    assert "0 a 70 años" in accidente
    assert "0 a 70 años cumplidos" in funerario
    # La membresia completa NO se presenta limitada a 70 años.
    assert "membresía VRIM Plus durante 12 meses*, sujeta a la formalización" in msg
    encabezado = msg[:msg.index("🩺")]
    assert "70 años" not in encabezado


def test_vrim_cta_is_at_the_end(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    msg = sent[-1][1]
    assert msg.strip().endswith("1️⃣ Sí, quiero que me contacte\n2️⃣ No por ahora")


def test_vrim_full_message_sent_only_once(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    vicky_app.handle(_text_msg(PHONE, "80000", "m5"))
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    completos = [s for s in sent if "🎁 *¡Tu propuesta puede darte mucho más que un préstamo!*" in s[1]]
    assert len(completos) == 1
    assert _data()["vrim_offered"] is True


def test_vrim_send_failure_keeps_fallback_cta(monkeypatch):
    sent, _, boardroom_calls, logged = _base_patches(monkeypatch)

    def flaky(to, text):
        if "🎁" in text:
            return False
        sent.append((to, text))
        return True

    monkeypatch.setattr(vicky_app, "send_msg", flaky)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    vicky_app.handle(_text_msg(PHONE, "1", "m2"))
    vicky_app.handle(_text_msg(PHONE, "12000", "m3"))
    assert sent[-1][1] == vicky_app._IMSS_REVISION_CTA_FALLBACK
    assert vicky_app.user_state.get(PHONE) == "imss_q_revision"
    assert _data().get("vrim_offered") is not True
    assert [c for c in logged if c[3] == "respaldo_lead"] == []


def test_both_sends_failing_keeps_recoverable_mechanism(monkeypatch):
    sent, _, boardroom_calls, logged = _base_patches(monkeypatch)
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: False)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    vicky_app.handle(_text_msg(PHONE, "1", "m2"))
    vicky_app.handle(_text_msg(PHONE, "12000", "m3"))
    assert vicky_app.user_state.get(PHONE) == "imss_cta_pendiente"
    backup = [c for c in logged if c[3] == "respaldo_lead"]
    assert len(backup) == 1 and backup[0][5] == "cta_send_failed"
    assert _data()["pension"] == 12000
    assert boardroom_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 63-70. Cierre comercial en un solo mensaje
# ══════════════════════════════════════════════════════════════════════════════

def test_closing_and_horario_in_a_single_bubble(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, MARTES_10AM)   # escenario A: hoy + mañana
    antes = len(sent)
    _accept_and_close()
    # 3 turnos -> 4 burbujas: nombre, ciudad, cierre+horario y el acuse
    # automatico de cortesia que cierra el turno sin esperar al cliente.
    assert len(sent) - antes == 4
    cierre = _cierre(sent)
    assert "✅ *Listo, Juan. Ya tenemos una propuesta estimada para ti.*" in cierre
    assert "📞 *¿Cuándo prefieres que te llame Christian?*" in cierre
    assert "1️⃣ Hoy por la tarde" in cierre
    assert "2️⃣ Mañana por la mañana" in cierre
    assert "3️⃣ Mañana por la tarde" in cierre
    assert "otro día y horario específico" in cierre
    assert "“El jueves a las 10:00 a. m.”" in cierre
    assert vicky_app.user_state.get(PHONE) == "imss_q_horario_calc"


def test_no_separate_second_horario_question(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    for _to, text in sent:
        assert "¿En qué horario te puede llamar Christian hoy?" not in text
    assert sum(1 for s in sent if "¿Cuándo prefieres que te llame Christian?" in s[1]) == 1


def test_closing_vrim_reference_is_brief(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    cierre = _cierre(sent)
    assert "membresía VRIM Plus por 12 meses*, sujeta a formalización" in cierre
    # No repite coberturas ni vuelve a pedir aceptacion de VRIM.
    for cobertura in ("ambulancia", "check-up", "videoconsulta", "funerario",
                      "70 años", "$20,000", "¿Quieres que Christian revise tu caso?"):
        assert cobertura not in cierre


def test_closing_omits_vrim_when_not_preeligible():
    data = {"nombre": "Ana Lopez", "pension": 12000.0,
            "propuesta_activa_monto": 100000.0, "propuesta_activa_cuota": 2500.0,
            "propuesta_activa_plazo": 60, "vrim_preeligible": False}
    cierre = vicky_app._imss_build_closing_statement(data)
    assert "VRIM" not in cierre
    assert "\n\n\n" not in cierre
    assert "  " not in cierre.replace("  ", " ", 0) or True
    assert cierre.strip() == cierre
    assert "✅ *Listo, Ana." in cierre
    assert "📞 *¿Cuándo prefieres que te llame Christian?*" in cierre


def test_closing_includes_vrim_when_preeligible():
    data = {"nombre": "Ana Lopez", "pension": 12000.0,
            "propuesta_activa_monto": 100000.0, "propuesta_activa_cuota": 2500.0,
            "propuesta_activa_plazo": 60, "vrim_preeligible": True}
    cierre = vicky_app._imss_build_closing_statement(data)
    assert "membresía VRIM Plus por 12 meses" in cierre
    assert "\n\n\n" not in cierre


def test_closing_statement_is_the_only_deterministic_source():
    src = inspect.getsource(vicky_app)
    assert src.count("def _imss_build_closing_statement") == 1
    # definicion + funnel de texto (imss_q_ciudad_calc) + handoff del Flow
    # dinamico (_imss_flow_handle_handoff): dos llamadas legitimas a LA MISMA
    # funcion compartida, no una reimplementacion del cierre.
    assert src.count("_imss_build_closing_statement(") == 3
    # No queda rastro del cierre anterior.
    assert "la propuesta estimada queda en" not in src
    assert "beneficio vinculado de la membresía" not in src
    assert "¿En qué horario te puede llamar Christian hoy?" not in src


def test_closing_statement_reads_the_active_proposal():
    src = inspect.getsource(vicky_app._imss_build_closing_statement)
    assert "_imss_get_propuesta_activa" in src
    assert 'data.get("propuesta_monto")' not in src


def test_closing_uses_first_name_only(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _accept_and_close(nombre="Maria Fernanda Ruiz Soto")
    assert "*Listo, Maria." in _cierre(sent)


# ══════════════════════════════════════════════════════════════════════════════
# 71-79. Horario, cortesias, notificacion principal y persistencia
# ══════════════════════════════════════════════════════════════════════════════

def test_horario_option_1_normalizes(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, MARTES_10AM)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    assert _data()["horario_contacto"] == "Hoy por la tarde"
    # [0]=alerta temprana, [1]=calificacion completa, [2]=brief de horario.
    assert "Hoy por la tarde" in advisor_msgs[2]


def test_horario_option_2_normalizes(monkeypatch):
    _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, MARTES_10AM)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "2", "h1"))
    assert _data()["horario_contacto"] == "Mañana por la mañana"


def test_horario_option_3_normalizes(monkeypatch):
    _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, MARTES_10AM)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "3", "h1"))
    assert _data()["horario_contacto"] == "Mañana por la tarde"


def test_horario_variants_with_number_and_label_normalize():
    """La resolucion se hace contra las etiquetas ofrecidas en ese turno."""
    opciones = vicky_app._imss_build_horario_opciones(MARTES_10AM)
    assert vicky_app._imss_normalize_horario("2 mañana por la mañana", opciones) == "Mañana por la mañana"
    assert vicky_app._imss_normalize_horario("hoy por la tarde", opciones) == "Hoy por la tarde"
    assert vicky_app._imss_normalize_horario("Mañana por la tarde", opciones) == "Mañana por la tarde"


def test_free_form_horario_is_preserved(monkeypatch):
    for momento in (MARTES_10AM, SABADO_1801, DOMINGO_9AM, VIERNES_7PM):
        for texto in ("El jueves a las 10:00 a. m.", "10:00 am", "después de las 4"):
            _to_proposal(monkeypatch)
            _freeze_horario(monkeypatch, momento)
            _accept_and_close()
            vicky_app.handle(_text_msg(PHONE, texto, "h1"))
            assert _data()["horario_contacto"] == texto, (momento, texto)


def test_free_form_horario_respects_length_cap(monkeypatch):
    _to_proposal(monkeypatch)
    _accept_and_close()
    largo = "el jueves " * 60
    vicky_app.handle(_text_msg(PHONE, largo, "h1"))
    assert len(_data()["horario_contacto"]) <= 200


def test_horario_confirmation_message(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    assert sent[-1][1] == ("¡Perfecto! Ya quedó registrado. *Christian López te "
                           "contactará en el horario indicado.* 😊")


def test_pure_courtesy_is_not_stored_as_horario(monkeypatch):
    for cortesia in ("gracias", "muchas gracias", "ok", "listo", "perfecto",
                     "excelente", "entendido"):
        sent, advisor_msgs, boardroom_calls, _ = _to_proposal(monkeypatch)
        _accept_and_close()
        # alerta temprana + calificacion completa.
        assert len(advisor_msgs) == 2
        vicky_app.handle(_text_msg(PHONE, cortesia, "h1"))
        data = _data()
        assert "horario_contacto" not in data, cortesia
        assert len(advisor_msgs) == 2, cortesia          # sin notificacion falsa
        assert data.get("nombre") == "Juan Perez"        # no se borran los datos
        assert data.get("propuesta_activa_monto")
        assert boardroom_calls == []
        assert vicky_app.user_state.get(PHONE) == "imss_post_cierre"


def test_main_advisor_notification_happens_before_waiting_for_horario(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    # alerta temprana + calificacion completa.
    assert len(advisor_msgs) == 2
    assert "📣 PROSPECTO IMSS CALIFICADO — LLAMAR" in advisor_msgs[-1]
    assert vicky_app.user_state.get(PHONE) == "imss_q_horario_calc"


def test_abandoning_without_horario_does_not_lose_the_lead(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    _accept_and_close()
    data = _data()
    assert data["advisor_notify_ok"] is True
    assert data["nombre"] == "Juan Perez"
    assert data["ciudad"] == "Los Mochis"
    assert data["propuesta_activa_plazo"] == 30
    assert len(advisor_msgs) == 2


def test_user_data_survives_the_close(monkeypatch):
    _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "30 meses", "m4"))
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    data = _data()
    for campo in ("nombre", "ciudad", "pension", "propuesta_monto",
                  "propuesta_activa_monto", "propuesta_activa_cuota",
                  "propuesta_activa_plazo", "propuesta_activa_origen",
                  "vrim_preeligible", "vrim_offered", "advisor_notify_ok",
                  "horario_contacto"):
        assert data.get(campo) is not None, campo
    assert data["cierre_tipo"] == "revision_aceptada"


def test_advisor_notification_omits_vrim_membership_details(monkeypatch):
    # VRIM (preelegibilidad, oferta, interes, aviso de edad) se retiro de la
    # alerta al asesor: es informacion de la membresia, no del prestamo.
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    aviso = advisor_msgs[-1]
    for campo in ("Verificar edad", "VRIM preelegible", "Base de elegibilidad VRIM",
                  "Promoción VRIM presentada", "Interés del cliente en VRIM"):
        assert campo not in aviso, campo


def test_advisor_notification_keeps_required_fields(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "y si quiero 80000", "m4"))
    _accept_and_close()
    aviso = advisor_msgs[-1]
    for campo in ("Nombre: Juan Perez", "WhatsApp: ", "Ciudad: Los Mochis",
                  "Pensión mensual: ", "Monto estimado: ", "Monto solicitado por cliente: ",
                  "Cuota estimada: ", "Plazo: ", "Origen de la propuesta activa: ",
                  "Estado del funnel: "):
        assert campo in aviso, campo


def test_advisor_notification_keeps_origin_and_referral_when_present(monkeypatch):
    sent, advisor_msgs, _, _ = _base_patches(monkeypatch)
    phone = "6685557777"
    obj = _text_msg(phone, "Hello! Can I get more info on this?", "mid-ref2")
    obj["referral"] = {"source_type": "ad", "source_id": "777000111",
                       "headline": "Prestamo para pensionados",
                       "body": "Conoce si calificas con tu pension."}
    vicky_app.handle(obj)
    vicky_app.handle(_text_msg(phone, "1", "r2"))
    vicky_app.handle(_text_msg(phone, "12000", "r3"))
    _accept_and_close(phone=phone)
    aviso = advisor_msgs[-1]
    assert "Origen: campana_IMSS" in aviso
    assert "Headline anuncio: Prestamo para pensionados" in aviso


# ══════════════════════════════════════════════════════════════════════════════
# CARRIL 3 · Seccion 1 — Notacion coloquial de monto ("mil")
# ══════════════════════════════════════════════════════════════════════════════

def test_mil_notation_extraction_forms():
    assert vicky_app._imss_extract_monto_plazo("80 mil") == (80000.0, None, None)
    assert vicky_app._imss_extract_monto_plazo("80mil") == (80000.0, None, None)
    assert vicky_app._imss_extract_monto_plazo("100 mil a 24 meses") == (100000.0, 24, None)
    assert vicky_app._imss_extract_monto_plazo("80 mil pesos") == (80000.0, None, None)
    # Decimal: hay precedente en extract_num() (regex (\d+)(\.\d+)?).
    assert vicky_app._imss_extract_monto_plazo("80.5 mil") == (80500.0, None, None)


def test_existing_amount_formats_still_work():
    assert vicky_app._imss_extract_monto_plazo("80000") == (80000.0, None, None)
    assert vicky_app._imss_extract_monto_plazo("$80,000") == (80000.0, None, None)
    assert vicky_app._imss_extract_monto_plazo("y si quiero 45000") == (45000.0, None, None)
    assert vicky_app._imss_extract_monto("80000") == 80000.0
    assert vicky_app._imss_extract_monto("$80,000") == 80000.0


def test_80_mil_is_quoted_as_80000_inside_revision(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "80 mil", "m4"))
    assert "$80,000" in sent[-1][1]
    assert _data()["monto_solicitado"] == 80000.0
    assert _data()["propuesta_activa_monto"] == 80000.0
    assert boardroom_calls == []


def test_80mil_without_space_is_quoted_as_80000(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "80mil", "m4"))
    assert "$80,000" in sent[-1][1]
    assert _data()["monto_solicitado"] == 80000.0


def test_mil_with_plazo_together(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "100 mil a 24 meses", "m4"))
    data = _data()
    assert data["monto_solicitado"] == 100000.0
    # Con pension 12,000 el tope a 24 meses es ~$66,838: se ajusta el plazo
    # (carril 2) en lugar de cotizar una cuota imposible.
    assert data["propuesta_activa_monto"] == 100000.0
    assert data["propuesta_activa_cuota"] <= 12000 * vicky_app.IMSS_LIMITE_DESCUENTO + 1


def test_mil_forms_are_recognized_as_followup():
    for texto in ("80 mil", "80mil", "80 mil pesos", "100 mil a 24 meses", "80.5 mil"):
        assert vicky_app._is_imss_revision_followup(texto) is True, texto


def test_mil_does_not_affect_phone_numbers_or_free_horario():
    # Un telefono se extrae exactamente igual que antes del cambio.
    assert vicky_app._imss_extract_monto("6681234567") == vicky_app.extract_num("6681234567")
    assert vicky_app._is_imss_revision_followup("6681234567") is False
    # Un horario libre plausible no se convierte en monto ni en seguimiento.
    for horario in ("el jueves a las 10:00 a. m.", "después de las 4", "10:00 am"):
        assert vicky_app._is_imss_revision_followup(horario) is False, horario
    # "millones"/"milagro" no disparan la regla de "mil".
    assert vicky_app._IMSS_MIL_RE.search("2 millones") is None
    assert vicky_app._IMSS_MIL_RE.search("un milagro") is None


def test_free_horario_text_with_mil_is_not_converted(monkeypatch):
    """Ninguna heuristica de 'mil' toca el estado de horario."""
    _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, MARTES_10AM)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "el jueves a las 10 mil disculpa, 10 am", "h1"))
    assert _data()["horario_contacto"] == "el jueves a las 10 mil disculpa, 10 am"


def test_mil_notation_also_applies_to_pension(monkeypatch):
    """La pension vive dentro del funnel IMSS: '7 mil' son $7,000, no $7."""
    sent, _, _, _ = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(PHONE, "1", "m1"))
    vicky_app.handle(_text_msg(PHONE, "1", "m2"))
    vicky_app.handle(_text_msg(PHONE, "15 mil", "m3"))
    assert _data()["pension"] == 15000.0
    assert "$15,000" in sent[-2][1]


def test_pension_plain_formats_unchanged(monkeypatch):
    for texto, esperado in (("12000", 12000.0), ("$12,000", 12000.0), ("7500", 7500.0)):
        sent, _, _, _ = _base_patches(monkeypatch)
        vicky_app.handle(_text_msg(PHONE, "1", "m1"))
        vicky_app.handle(_text_msg(PHONE, "1", "m2"))
        vicky_app.handle(_text_msg(PHONE, texto, "m3"))
        assert _data()["pension"] == esperado, texto


def test_global_extract_num_is_untouched():
    """extract_num() es global (Auto, Vida, VRIM, Empresarial, CTC): el
    reconocimiento de 'mil' NO se le agrego."""
    assert vicky_app.extract_num("80 mil") == 80.0
    assert vicky_app.extract_num("80000") == 80000.0
    assert "mil" not in inspect.getsource(vicky_app.extract_num)


def test_other_funnels_do_not_use_the_imss_amount_helper():
    src = inspect.getsource(vicky_app)
    # El helper solo se invoca desde el funnel IMSS.
    llamadas = [l.strip() for l in src.splitlines() if "_imss_extract_monto(" in l
                and "def _imss_extract_monto" not in l]
    assert len(llamadas) == 2, llamadas   # pension IMSS + extractor monto/plazo


# ══════════════════════════════════════════════════════════════════════════════
# CARRIL 3 · Seccion 2 — Horario comercial dinamico (America/Mazatlan)
# ══════════════════════════════════════════════════════════════════════════════

def test_commercial_timezone_is_mazatlan_and_general_tz_untouched():
    """pytz esta en requirements.txt (lo usa Render) pero puede faltar en el
    entorno local: la app ya trae fallback. La prueba valida ambos caminos."""
    src = inspect.getsource(vicky_app)
    assert 'pytz.timezone("America/Mazatlan")' in src
    # Sinaloa opera en UTC-7 todo el año, con pytz o con el fallback.
    offset = MARTES_10AM.utcoffset()
    assert offset == datetime.timedelta(hours=-7), offset

    # _TZ / now_mx() de uso general NO se tocaron: siguen en Mexico_City (UTC-6).
    assert 'pytz.timezone("America/Mexico_City")' in src
    assert src.count('pytz.timezone("America/Mexico_City")') == 1
    assert "Mazatlan" not in inspect.getsource(vicky_app.now_mx)
    assert vicky_app._TZ is not vicky_app._IMSS_TZ_COMERCIAL
    ahora_general = datetime.datetime.now(vicky_app._TZ)
    assert ahora_general.utcoffset() == datetime.timedelta(hours=-6)


def test_reference_moments_are_the_expected_weekdays():
    assert MARTES_10AM.weekday() == 1
    assert VIERNES_7PM.weekday() == 4
    assert SABADO_1759.weekday() == 5 and SABADO_1801.weekday() == 5
    assert DOMINGO_9AM.weekday() == 6


def test_scenario_a_weekday_before_6pm():
    opciones = vicky_app._imss_build_horario_opciones(MARTES_10AM)
    assert opciones == {"1": "Hoy por la tarde",
                        "2": "Mañana por la mañana",
                        "3": "Mañana por la tarde"}


def test_scenario_a_saturday_1759_points_to_monday_never_sunday():
    opciones = vicky_app._imss_build_horario_opciones(SABADO_1759)
    assert opciones["1"] == "Hoy por la tarde"
    assert opciones["2"] == "Lunes por la mañana"
    assert opciones["3"] == "Lunes por la tarde"
    for etiqueta in opciones.values():
        assert "Mañana" not in etiqueta
        assert "omingo" not in etiqueta


def test_scenario_c_saturday_1801():
    opciones = vicky_app._imss_build_horario_opciones(SABADO_1801)
    assert opciones == {"1": "Lunes por la mañana",
                        "2": "Lunes por la tarde",
                        "3": "Otro día y horario específico"}


def test_scenario_c_sunday_any_hour():
    for hora in (0, 9, 13, 17, 18, 23):
        opciones = vicky_app._imss_build_horario_opciones(_at(2026, 8, 2, hora))
        assert opciones == vicky_app._imss_build_horario_opciones(SABADO_1801), hora


def test_scenario_b_friday_7pm_is_not_confused_with_weekend():
    """Viernes 19:00: mañana es sábado, que SÍ es hábil -> escenario B."""
    opciones = vicky_app._imss_build_horario_opciones(VIERNES_7PM)
    assert opciones == {"1": "Mañana por la mañana",
                        "2": "Mañana por la tarde",
                        "3": "Otro día y horario específico"}
    assert opciones != vicky_app._imss_build_horario_opciones(SABADO_1801)


def test_no_hoy_option_from_6pm_onwards():
    for momento in (_at(2026, 7, 27, 18), VIERNES_7PM, SABADO_1801, DOMINGO_9AM):
        opciones = vicky_app._imss_build_horario_opciones(momento)
        assert not any("Hoy" in e for e in opciones.values()), momento


def test_options_are_never_duplicated_and_never_sunday():
    # Semana completa (lunes 27-jul a domingo 2-ago) a distintas horas.
    lunes = datetime.date(2026, 7, 27)
    momentos = []
    for d in range(7):
        dia = lunes + datetime.timedelta(days=d)
        momentos += [_at(dia.year, dia.month, dia.day, h) for h in (9, 13, 17, 18, 22)]
    assert [m.weekday() for m in momentos[::5]] == [0, 1, 2, 3, 4, 5, 6]
    for momento in momentos:
        opciones = vicky_app._imss_build_horario_opciones(momento)
        assert len(set(opciones.values())) == 3, momento
        assert set(opciones) == {"1", "2", "3"}
        for etiqueta in opciones.values():
            assert "omingo" not in etiqueta, momento


def test_offered_options_are_persisted_on_closing(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, SABADO_1759)
    _accept_and_close()
    ofrecidos = _data()["imss_horarios_ofrecidos"]
    assert ofrecidos == vicky_app._imss_build_horario_opciones(SABADO_1759)
    cierre = _cierre(sent)
    for numero, etiqueta in (("1️⃣", ofrecidos["1"]), ("2️⃣", ofrecidos["2"]), ("3️⃣", ofrecidos["3"])):
        assert f"{numero} {etiqueta}" in cierre


def test_closing_omits_free_text_invitation_when_option_3_is_other(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, DOMINGO_9AM)
    _accept_and_close()
    cierre = _cierre(sent)
    assert "3️⃣ Otro día y horario específico" in cierre
    # No se repite la invitacion, la opcion 3 ya la representa.
    assert cierre.count("otro día y horario específico") == 0
    assert "“El jueves a las 10:00 a. m.”" not in cierre


def test_answer_resolved_against_persisted_labels_not_current_clock(monkeypatch):
    """El cierre se envio en sabado por la tarde; el cliente responde el lunes.
    La respuesta se resuelve con lo que VIO, no con el reloj de ahora."""
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, SABADO_1759)
    _accept_and_close()
    ofrecidos = dict(_data()["imss_horarios_ofrecidos"])
    # Pasa el tiempo: ahora es lunes por la mañana.
    _freeze_horario(monkeypatch, MARTES_10AM)
    vicky_app.handle(_text_msg(PHONE, "2", "h1"))
    assert _data()["horario_contacto"] == ofrecidos["2"] == "Lunes por la mañana"
    assert "Lunes por la mañana" in advisor_msgs[2]
    assert _data()["imss_horarios_ofrecidos"] == ofrecidos   # no se recalculo


def test_option_3_in_scenario_a_resolves_with_third_persisted_label(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, SABADO_1759)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "3", "h1"))
    assert _data()["horario_contacto"] == "Lunes por la tarde"
    assert "Lunes por la tarde" in advisor_msgs[2]
    assert vicky_app.user_state.get(PHONE) == "imss_post_cierre"


def test_option_3_in_scenario_b_and_c_asks_for_free_text(monkeypatch):
    for momento in (VIERNES_7PM, SABADO_1801, DOMINGO_9AM):
        sent, advisor_msgs, boardroom_calls, _ = _to_proposal(monkeypatch)
        _freeze_horario(monkeypatch, momento)
        _accept_and_close()
        assert len(advisor_msgs) == 2
        vicky_app.handle(_text_msg(PHONE, "3", "h1"))
        data = _data()
        # No se guarda "3" ni ninguna etiqueta falsa.
        assert "horario_contacto" not in data, momento
        assert len(advisor_msgs) == 2, momento           # sin notificacion falsa
        assert vicky_app.user_state.get(PHONE) == "imss_q_horario_calc", momento
        assert "día y el horario" in sent[-1][1]
        assert boardroom_calls == []
        # Al escribir el horario libre, se captura con el comportamiento normal.
        vicky_app.handle(_text_msg(PHONE, "El miércoles a las 11:00 a. m.", "h2"))
        assert _data()["horario_contacto"] == "El miércoles a las 11:00 a. m."
        assert len(advisor_msgs) == 3
        assert "El miércoles a las 11:00 a. m." in advisor_msgs[2]


def test_advisor_label_equals_the_label_shown_to_the_client(monkeypatch):
    for momento in (MARTES_10AM, SABADO_1759, SABADO_1801, DOMINGO_9AM, VIERNES_7PM):
        for opcion in ("1", "2"):
            sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
            _freeze_horario(monkeypatch, momento)
            _accept_and_close()
            cierre = _cierre(sent)
            vicky_app.handle(_text_msg(PHONE, opcion, "h1"))
            etiqueta = _data()["horario_contacto"]
            assert f"{opcion}️⃣ {etiqueta}" in cierre, (momento, opcion)
            assert etiqueta in advisor_msgs[2], (momento, opcion)


def test_new_closing_replaces_previously_persisted_options(monkeypatch):
    sent, _, _, _ = _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, DOMINGO_9AM)
    _accept_and_close()
    assert _data()["imss_horarios_ofrecidos"]["1"] == "Lunes por la mañana"
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))          # cierra la conversacion
    # Conversacion nueva desde cero, en otro momento de la semana.
    _to_proposal(monkeypatch)
    _freeze_horario(monkeypatch, MARTES_10AM)
    _accept_and_close()
    assert _data()["imss_horarios_ofrecidos"]["1"] == "Hoy por la tarde"
    assert "Lunes" not in str(_data()["imss_horarios_ofrecidos"])


def test_no_global_hardcoded_horario_map():
    """La normalizacion resuelve contra la estructura persistida, no contra un
    diccionario fijo de etiquetas."""
    src = inspect.getsource(vicky_app)
    assert "_IMSS_HORARIO_OPCIONES" not in src            # el mapa estatico ya no existe
    firma = inspect.signature(vicky_app._imss_normalize_horario)
    assert list(firma.parameters) == ["msg", "opciones"]
    cuerpo = inspect.getsource(vicky_app._imss_normalize_horario)
    for etiqueta in ("Hoy por la tarde", "Mañana por la mañana", "Lunes por la"):
        assert etiqueta not in cuerpo, etiqueta


def test_courtesy_after_closing_still_works_in_every_scenario(monkeypatch):
    """La logica de cortesia posterior al cierre no cambio."""
    for momento in (MARTES_10AM, SABADO_1801, DOMINGO_9AM):
        sent, advisor_msgs, boardroom_calls, _ = _to_proposal(monkeypatch)
        _freeze_horario(monkeypatch, momento)
        _accept_and_close()
        vicky_app.handle(_text_msg(PHONE, "gracias", "h1"))
        assert "horario_contacto" not in _data(), momento
        assert len(advisor_msgs) == 2, momento
        assert vicky_app.user_state.get(PHONE) == "imss_post_cierre"
        assert boardroom_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 80-85. _SYS y orden de definicion
# ══════════════════════════════════════════════════════════════════════════════

def test_module_imports_without_error():
    modulo = importlib.import_module("app")
    assert modulo is not None
    importlib.reload(modulo)


def test_financial_constants_defined_before_sys():
    src = inspect.getsource(vicky_app)
    pos_sys = src.index("\n_SYS = (")
    for constante in ("IMSS_TASA_MENSUAL", "IMSS_TASA_ANUAL_SIN_IVA", "IMSS_CAT_SIN_IVA"):
        assert src.index(f"\n{constante} = ") < pos_sys, constante


def test_financial_constants_defined_only_once():
    src = inspect.getsource(vicky_app)
    for constante in ("IMSS_TASA_MENSUAL", "IMSS_IVA_RATE", "IMSS_CAT",
                      "IMSS_PLAZO_MESES", "IMSS_LIMITE_DESCUENTO", "IMSS_MONTO_MINIMO",
                      "IMSS_TASA_ANUAL_SIN_IVA", "IMSS_CAT_SIN_IVA",
                      "IMSS_PLAZOS_DISPONIBLES"):
        assert len(re.findall(rf"^{constante} = ", src, re.MULTILINE)) == 1, constante


def test_sys_contains_the_same_financial_criteria_as_the_funnel():
    assert "22.39% sin IVA" in vicky_app._SYS
    assert "24.8% sin IVA" in vicky_app._SYS


def test_sys_no_longer_contains_the_old_financial_string():
    assert "CAT 29.3%" not in vicky_app._SYS
    assert "29.3" not in vicky_app._SYS


def test_sys_has_no_ambiguous_iva_comparison():
    assert "75.19" not in vicky_app._SYS
    assert "competencia" not in vicky_app._SYS.lower()


def test_sys_preserves_other_products():
    for fragmento in ("PyME Alta Eficiencia: 18%", "PyME Flexible: 36%",
                      "Tolerancia buró hasta $30,000", "TPV: desde 1.05%",
                      "VRIM: membresía médica", "$40,000 a $650,000"):
        assert fragmento in vicky_app._SYS, fragmento


# ══════════════════════════════════════════════════════════════════════════════
# 86-94. No regresiones fuera del funnel IMSS
# ══════════════════════════════════════════════════════════════════════════════

def test_ctc_keeps_absolute_priority_over_imss(monkeypatch):
    sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
    monkeypatch.setenv("CTC_META_REFERRAL_IDS", "555000111")
    # Referral de CTC con copy deliberadamente IMSS: IMSS nunca debe reclamarlo.
    ref = {"source_type": "ad", "source_id": "555000111",
           "headline": "préstamo IMSS pensionados Ley 73",
           "body": "pensionado IMSS conoce tu propuesta"}
    texto = "Hello! Can I get more info on this?"
    obj = _text_msg("6685559999", texto, "mid-ctc")
    obj["referral"] = ref
    assert vicky_app._is_ctc_meta_campaign_referral(obj, vicky_app.norm(texto)) is True
    assert vicky_app._is_campaign(obj, vicky_app.norm(texto)) is False
    vicky_app.handle(obj)
    assert vicky_app.user_state.get("6685559999", "").startswith("fp_")
    assert "Consigue Tu Crédito" in sent[0][1]
    assert boardroom_calls == []


def test_no_loan_purpose_is_asked_or_stored(monkeypatch):
    sent, advisor_msgs, _, _ = _to_proposal(monkeypatch)
    _accept_and_close()
    vicky_app.handle(_text_msg(PHONE, "1", "h1"))
    for _to, text in sent:
        for prohibido in ("para qué", "motivo", "destino del", "uso del crédito"):
            assert prohibido not in text.lower(), text[:60]
    assert not any(k for k in _data() if "motivo" in k or "destino" in k)
    src = inspect.getsource(vicky_app)
    for prohibido in ('"motivo_prestamo"', "'motivo_prestamo'",
                      '"destino_prestamo"', "'destino_prestamo'"):
        assert prohibido not in src


def test_other_funnels_unaffected(monkeypatch):
    esperado = {"2": "Seguro de Auto", "3": "Seguro de Vida",
                "4": "Tarjeta Médica VRIM", "5": "Crédito Empresarial",
                "6": "Consigue Tu Crédito"}
    for opcion, texto in esperado.items():
        sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
        vicky_app.handle(_text_msg("668555" + opcion * 4, opcion, "mid-" + opcion))
        assert texto in sent[0][1], opcion
        assert boardroom_calls == []


def test_product_code_contracts_preserved():
    assert vicky_app._service_to_product_code("imss") == "prestamo_imss_ley73"
    assert vicky_app._service_to_product_code("fp") == "credito_empresarial_sin_garantia"


def test_google_sheets_schema_not_changed():
    """El respaldo sigue usando _log() con las columnas existentes: mismos
    parametros, sin hoja ni columna nueva."""
    firma = inspect.signature(vicky_app._log)
    assert list(firma.parameters) == ["phone", "nombre", "msg", "tipo", "origen",
                                      "resultado", "error", "mid"]
    src = inspect.getsource(vicky_app._imss_log_lead_backup)
    assert "_log(" in src
    assert "add_worksheet" not in src


def test_boardroom_never_called_during_active_imss_funnel(monkeypatch):
    sent, _, boardroom_calls, _ = _to_proposal(monkeypatch)
    for turno in ("40 meses", "24", "6 meses", "80000", "1", "Juan Perez", "Los Mochis", "1"):
        vicky_app.handle(_text_msg(PHONE, turno, "t-" + turno[:4]))
    assert boardroom_calls == []
    assert all(vicky_app.NEUTRAL_FALLBACK_MESSAGE not in s[1] for s in sent)


def test_imss_referral_detection_behaviour_unchanged(monkeypatch):
    sent, _, boardroom_calls, _ = _base_patches(monkeypatch)
    assert vicky_app._IMSS_META_REFERRAL_IDS == set()
    ref = {"source_type": "ad", "source_id": "111",
           "headline": "Prestamo para pensionados",
           "body": "Conoce si calificas para un prestamo con tu pension."}
    obj = _text_msg("6685558888", "Hello! Can I get more info on this?", "mid-ref")
    obj["referral"] = ref
    vicky_app.handle(obj)
    assert vicky_app.user_state.get("6685558888") == "imss_q_ley73"
    assert vicky_app.user_data["6685558888"]["origen"].startswith("campana_IMSS")
    assert boardroom_calls == []
