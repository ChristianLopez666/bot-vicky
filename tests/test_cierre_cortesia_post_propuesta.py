"""
Cierre de cortesia post-propuesta (Vicky Redes).

Antes, al terminar el cuestionario el prospecto recibia la propuesta y la
conversacion se quedaba muda: el agradecimiento y el "Christian te contactara"
solo salian si el cliente escribia otra vez ("listo", "gracias"). Ademas una
negativa ("no gracias") en la pregunta de horario se guardaba como
horario_contacto y el asesor recibia una hora que el cliente nunca dio.

Esta bateria cubre las cuatro piezas del cierre:
  1. acuse automatico en cuanto se entrega la propuesta (Flow y texto),
  2. respuesta de cortesia sin genero, con invitacion al menu, una sola vez,
  3. despedida cuando el cliente responde que no,
  4. recordatorio a la hora si no responde, entregado una sola vez y cancelado
     por cualquier mensaje entrante.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app
import cierre_cortesia as cc


def _text_msg(phone: str, text: str, mid: str) -> dict:
    return {"from": phone, "id": mid, "type": "text", "text": {"body": text}}


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None, name=None):
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
    # El barrido es un bucle infinito: en pruebas se invoca a mano
    # (nudge_sweep_once), nunca desde un hilo.
    monkeypatch.setattr(vicky_app, "CIERRE_NUDGE_SWEEPER", False)
    monkeypatch.setattr(vicky_app._state_store, "_nudge_mem", {})

    sent = []
    advisor_msgs = []

    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_msgs.append(msg) or True)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_report_upsert_lead", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_imss_report_lead_qualified", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "Test")
    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction", lambda payload: (None, "n/a"))

    return sent, advisor_msgs


def _llegar_a_horario(monkeypatch, phone: str):
    """Recorre el funnel de texto hasta dejar al prospecto con su propuesta
    entregada y esperando el horario de contacto."""
    sent, advisor_msgs = _base_patches(monkeypatch)
    vicky_app.handle(_text_msg(phone, "1", "m1"))        # menu -> filtro Ley 73
    vicky_app.handle(_text_msg(phone, "1", "m2"))        # Ley 73 = si -> pension
    vicky_app.handle(_text_msg(phone, "9000", "m3"))     # pension -> propuesta
    vicky_app.handle(_text_msg(phone, "1", "m4"))        # si quiero revision
    vicky_app.handle(_text_msg(phone, "Juan Perez", "m5"))
    vicky_app.handle(_text_msg(phone, "Culiacan", "m6"))  # cierre + acuse
    return sent, advisor_msgs


# ── 1. Acuse automatico ───────────────────────────────────────────────────────

def test_acuse_automatico_tras_la_propuesta_en_el_funnel_de_texto(monkeypatch):
    phone = "6683334401"
    sent, _ = _llegar_a_horario(monkeypatch, phone)

    textos = [t for _, t in sent]
    assert cc.ACUSE_PROPUESTA in textos, textos
    # Es lo ULTIMO que ve el cliente, y no requirio otro mensaje suyo.
    assert textos[-1] == cc.ACUSE_PROPUESTA
    assert vicky_app.user_state.get(phone) == "imss_q_horario_calc"


def test_acuse_automatico_tras_terminar_el_flow_dinamico(monkeypatch):
    sent, _ = _base_patches(monkeypatch)
    phone = "6683334402"
    vicky_app.user_data[phone] = {
        "pension": 9000.0, "propuesta_monto": 90000, "propuesta_cuota": 2700,
        "propuesta_plazo": 60, "vrim_preeligible": False,
    }
    vicky_app.user_state[phone] = "imss_q_revision"

    vicky_app._imss_flow_handle_handoff(phone, {"nombre": "Juan Perez"}, "tok-acuse")

    textos = [t for _, t in sent]
    assert textos[-1] == cc.ACUSE_PROPUESTA
    assert vicky_app.user_state.get(phone) == "imss_q_horario_calc"


def test_el_acuse_no_sale_si_el_cierre_no_se_pudo_entregar(monkeypatch):
    sent, _ = _base_patches(monkeypatch)
    phone = "6683334403"
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) and False)
    vicky_app.user_data[phone] = {"pension": 9000.0, "propuesta_monto": 90000,
                                  "propuesta_cuota": 2700, "propuesta_plazo": 60}
    vicky_app.user_state[phone] = "imss_q_revision"

    vicky_app._imss_flow_handle_handoff(phone, {"nombre": "Juan Perez"}, "tok-falla")

    assert cc.ACUSE_PROPUESTA not in [t for _, t in sent]


# ── 2. Cortesia sin genero ────────────────────────────────────────────────────

def test_gracias_recibe_cortesia_sin_genero_con_invitacion_al_menu(monkeypatch):
    phone = "6683334404"
    sent, advisor_msgs = _llegar_a_horario(monkeypatch, phone)
    sent.clear()

    vicky_app.handle(_text_msg(phone, "gracias", "m7"))

    assert [t for _, t in sent] == [cc.CORTESIA_FINAL]
    assert "atenderle" in cc.CORTESIA_FINAL
    assert "menú" in cc.CORTESIA_FINAL
    assert "tarifa preferencial" in cc.CORTESIA_FINAL
    # No se invento un horario de contacto para el asesor.
    assert not any("HORARIO DE CONTACTO" in m for m in advisor_msgs)


def test_la_cortesia_no_se_repite_si_el_cliente_agradece_dos_veces(monkeypatch):
    phone = "6683334405"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    vicky_app.handle(_text_msg(phone, "gracias", "m7"))
    sent.clear()

    vicky_app.handle(_text_msg(phone, "ok gracias", "m8"))

    assert sent == []


# ── 3. Respuesta negativa ─────────────────────────────────────────────────────

def test_negativa_en_la_pregunta_de_horario_agradece_y_no_inventa_horario(monkeypatch):
    phone = "6683334406"
    sent, advisor_msgs = _llegar_a_horario(monkeypatch, phone)
    sent.clear()

    vicky_app.handle(_text_msg(phone, "no gracias", "m7"))

    assert [t for _, t in sent] == [cc.DESPEDIDA_NEGATIVA]
    assert not any("HORARIO DE CONTACTO" in m for m in advisor_msgs)
    assert vicky_app.user_data[phone].get("horario_contacto") is None


def test_negativa_despues_de_la_cortesia_cierra_la_conversacion(monkeypatch):
    phone = "6683334407"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    vicky_app.handle(_text_msg(phone, "gracias", "m7"))
    sent.clear()

    vicky_app.handle(_text_msg(phone, "no, gracias", "m8"))
    assert [t for _, t in sent] == [cc.DESPEDIDA_NEGATIVA]

    # Una cortesia posterior ya no vuelve a ofrecer nada.
    sent.clear()
    vicky_app.handle(_text_msg(phone, "gracias", "m9"))
    assert sent == []


def test_un_mensaje_con_intencion_nueva_no_se_traga_como_negativa(monkeypatch):
    phone = "6683334408"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    vicky_app.handle(_text_msg(phone, "gracias", "m7"))
    sent.clear()

    vicky_app.handle(_text_msg(phone, "no, mejor quiero ver el menu", "m8"))

    textos = [t for _, t in sent]
    assert cc.DESPEDIDA_NEGATIVA not in textos
    assert any("Servicios Financieros Inbursa" in t for t in textos), textos


# ── 4. Recordatorio a la hora ─────────────────────────────────────────────────

def test_el_recordatorio_queda_armado_tras_el_acuse(monkeypatch):
    phone = "6683334409"
    _llegar_a_horario(monkeypatch, phone)

    assert phone in vicky_app._state_store._nudge_mem
    due, ctx = vicky_app._state_store._nudge_mem[phone]
    assert due - time.time() > vicky_app.CIERRE_NUDGE_SECONDS - 60
    assert ctx["motivo"] == "acuse_propuesta_texto"


def test_el_recordatorio_se_entrega_una_sola_vez_cuando_vence(monkeypatch):
    phone = "6683334410"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    sent.clear()

    ahora = time.time()
    vicky_app._state_store._nudge_mem[phone] = (
        ahora - 1, {"armed_at": ahora - vicky_app.CIERRE_NUDGE_SECONDS, "motivo": "x"})

    assert vicky_app.nudge_sweep_once() == 1
    assert [t for _, t in sent] == [cc.NUDGE]

    sent.clear()
    assert vicky_app.nudge_sweep_once() == 0
    assert sent == []


def test_si_el_cliente_agradece_el_recordatorio_ya_no_existe(monkeypatch):
    """El recorrido real: propuesta -> "gracias" -> cortesia. Despues de eso NO
    puede quedar nada armado: el cliente respondio, asi que "si no responde en
    una hora" dejo de aplicar. Se comprueba sobre el almacen y recorriendo el
    webhook, no llamando a nudge_cancel() a mano -- el defecto que esto cubre
    era justamente que la cortesia volvia a armarlo despues de que el webhook
    ya lo habia cancelado."""
    phone = "6683334411"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    assert phone in vicky_app._state_store._nudge_mem

    vicky_app.handle(_text_msg(phone, "gracias", "m7"))

    assert [t for _, t in sent][-1] == cc.CORTESIA_FINAL
    assert phone not in vicky_app._state_store._nudge_mem
    sent.clear()
    assert vicky_app.nudge_sweep_once() == 0
    assert sent == []


def test_si_el_cliente_elige_horario_el_recordatorio_ya_no_existe(monkeypatch):
    phone = "6683334413"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    assert phone in vicky_app._state_store._nudge_mem

    vicky_app.handle(_text_msg(phone, "1", "m7"))  # elige horario

    assert phone not in vicky_app._state_store._nudge_mem
    sent.clear()
    assert vicky_app.nudge_sweep_once() == 0
    assert sent == []


def test_si_el_cliente_responde_que_no_el_recordatorio_ya_no_existe(monkeypatch):
    phone = "6683334414"
    sent, _ = _llegar_a_horario(monkeypatch, phone)

    vicky_app.handle(_text_msg(phone, "no gracias", "m7"))

    assert phone not in vicky_app._state_store._nudge_mem
    sent.clear()
    assert vicky_app.nudge_sweep_once() == 0
    assert sent == []


def test_tras_la_cortesia_una_segunda_negativa_tampoco_arma_nada(monkeypatch):
    """La cortesia deja una oferta abierta ("escriba menu..."). Ni esa oferta
    ni la despedida posterior vuelven a armar el recordatorio."""
    phone = "6683334415"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    vicky_app.handle(_text_msg(phone, "gracias", "m7"))
    vicky_app.handle(_text_msg(phone, "no, gracias", "m8"))

    assert phone not in vicky_app._state_store._nudge_mem


def test_un_recordatorio_muy_atrasado_ya_no_se_entrega(monkeypatch):
    phone = "6683334412"
    sent, _ = _llegar_a_horario(monkeypatch, phone)
    sent.clear()

    ahora = time.time()
    vicky_app._state_store._nudge_mem[phone] = (
        ahora - 1,
        {"armed_at": ahora - vicky_app.CIERRE_NUDGE_SECONDS - vicky_app.CIERRE_NUDGE_MAX_ATRASO - 60,
         "motivo": "x"},
    )

    assert vicky_app.nudge_sweep_once() == 0
    assert sent == []


# ── 5. Clasificador de negativa ───────────────────────────────────────────────

def test_es_respuesta_negativa_reconoce_declinaciones_corteses():
    for texto in ("no", "no gracias", "no muchas gracias", "por ahora no",
                  "ya no gracias", "asi esta bien gracias", "ninguno gracias",
                  "nada mas gracias"):
        assert cc.es_respuesta_negativa(texto), texto


def test_es_respuesta_negativa_no_se_traga_mensajes_con_contenido():
    for texto in ("", "gracias", "no entiendo", "no me interesa el seguro de auto",
                  "no quiero el seguro", "cuanto me prestan"):
        assert not cc.es_respuesta_negativa(texto), texto
