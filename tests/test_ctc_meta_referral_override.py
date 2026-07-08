"""
Override CTC por referral de anuncio Meta (hotfix f8ecff5).

La campana publicada en Meta es de Consigue Tu Credito (CTC), pero el copy
real del anuncio (source_id 6951847773049) trae senales empresariales
genericas ("financiamiento para empresas", "credito empresarial", "factoraje")
que la logica por keywords clasifica como emp. El override por ID de
campana/anuncio debe ganarle a ese copy y rutear a fp (funnel CTC), sin
cambiar el comportamiento de mensajes directos sin referral ni el de
referrals empresariales de otros anuncios.

Payloads basados en el referral REAL observado en logs de produccion
(2026-07-08 00:10 UTC, lead phone_last4=4791), no en referrals sinteticos
favorables que digan "consigue tu credito".
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


DIRECT_TEXT = "Me interesa crédito empresarial"

# Copy real del anuncio CTC (fuente: logs de produccion, META_REFERRAL_DETECTED).
_CTC_AD_HEADLINE = "financiamiento para empresas"
_CTC_AD_BODY = (
    "Impulsa tu empresa con liquidez. Si necesitas capital para proveedores, "
    "inventario, operación o crecimiento, en COHIFIS podemos ayudarte a revisar "
    "opciones de crédito empresarial y factoraje. Financiamiento desde 100 mil "
    "para empresas y negocios."
)
CTC_AD_SOURCE_ID = "6951847773049"


def _referral(source_id: str) -> dict:
    return {
        "source_type": "ad",
        "source_id": source_id,
        "source_url": "https://fb.me/test",
        "headline": _CTC_AD_HEADLINE,
        "body": _CTC_AD_BODY,
        "ctwa_clid": "test-clid",
    }


def _msg(phone: str, text: str, mid: str, referral: dict | None = None) -> dict:
    obj = {"from": phone, "id": mid, "type": "text", "text": {"body": text}}
    if referral is not None:
        obj["referral"] = referral
    return obj


def _base_patches(monkeypatch):
    """Aisla handle() de I/O real: WhatsApp, Sheets, notificaciones, Boardroom HTTP."""
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

    monkeypatch.setattr(vicky_app, "send_msg", fake_send_msg)
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: True)
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_nombre", lambda phone: "")

    boardroom_calls = []

    def fake_request_boardroom_instruction(payload):
        boardroom_calls.append(payload)
        return None, "should_not_be_called"

    monkeypatch.setattr(vicky_app, "_request_boardroom_instruction",
                        fake_request_boardroom_instruction)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_notify_boardroom_document", lambda *a, **k: None)
    monkeypatch.delenv("CTC_META_REFERRAL_IDS", raising=False)
    monkeypatch.delenv("CTC_META_REFERRAL_HINTS", raising=False)
    return sent, boardroom_calls


# ── Prueba obligatoria 1: mensaje directo sin referral sigue siendo emp ───────

def test_direct_text_without_referral_detects_emp():
    assert vicky_app.detect_svc(DIRECT_TEXT) == "emp"


def test_direct_text_without_referral_routes_to_emp_funnel(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    vicky_app.handle(_msg("5216681111111", DIRECT_TEXT, "mid-direct-emp"))
    assert vicky_app.user_state.get("5216681111111", "").startswith("emp_")
    assert sent and "Crédito Empresarial" in sent[0][1]
    assert boardroom_calls == []


# ── Prueba obligatoria 2: referral CTC real (copy empresarial) → fp ───────────

def test_ctc_referral_real_payload_detected_as_fp():
    msg_obj = _msg("5216682222222", DIRECT_TEXT, "mid-ctc-detect",
                   referral=_referral(CTC_AD_SOURCE_ID))
    assert vicky_app._detect_meta_referral_svc(msg_obj, DIRECT_TEXT) == "fp"


def test_ctc_referral_real_payload_opens_ctc_funnel(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    phone = "5216682222222"
    vicky_app.handle(_msg(phone, DIRECT_TEXT, "mid-ctc-e2e",
                          referral=_referral(CTC_AD_SOURCE_ID)))
    assert vicky_app.user_state.get(phone, "").startswith("fp_")
    assert sent and "Consigue Tu Crédito" in sent[0][1]
    assert vicky_app.user_data[phone]["origen"] == "meta_referral_ctc_campaign_override"
    assert boardroom_calls == []


# ── Prueba obligatoria 3: referral empresarial con OTRO source_id sigue emp ───

def test_generic_emp_referral_other_id_detected_as_emp():
    msg_obj = _msg("5216683333333", DIRECT_TEXT, "mid-emp-detect",
                   referral=_referral("9999999999999"))
    assert vicky_app._detect_meta_referral_svc(msg_obj, DIRECT_TEXT) == "emp"


def test_generic_emp_referral_other_id_opens_emp_funnel(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    phone = "5216683333333"
    vicky_app.handle(_msg(phone, DIRECT_TEXT, "mid-emp-e2e",
                          referral=_referral("9999999999999")))
    assert vicky_app.user_state.get(phone, "").startswith("emp_")
    assert sent and "Crédito Empresarial" in sent[0][1]
    assert vicky_app.user_data[phone]["origen"] == "meta_referral_emp"
    assert boardroom_calls == []


# ── Prueba obligatoria 4: env var CTC_META_REFERRAL_IDS ──────────────────────

def test_env_var_id_matches_same_realistic_payload(monkeypatch):
    # Caso literal del prompt: el ID tambien en env var, mismo payload real.
    monkeypatch.setenv("CTC_META_REFERRAL_IDS", CTC_AD_SOURCE_ID)
    msg_obj = _msg("5216684444444", DIRECT_TEXT, "mid-env-1",
                   referral=_referral(CTC_AD_SOURCE_ID))
    assert vicky_app._detect_meta_referral_svc(msg_obj, DIRECT_TEXT) == "fp"


def test_env_var_adds_future_ad_id_without_code_change(monkeypatch):
    # Caso fuerte: un anuncio FUTURO cuyo ID solo existe en la env var
    # (no hardcodeado) debe rutear a fp con el mismo copy empresarial.
    monkeypatch.setenv("CTC_META_REFERRAL_IDS", "7777000011112222, 8888000011112222")
    msg_obj = _msg("5216685555555", DIRECT_TEXT, "mid-env-2",
                   referral=_referral("7777000011112222"))
    assert vicky_app._detect_meta_referral_svc(msg_obj, DIRECT_TEXT) == "fp"


def test_env_var_empty_does_not_break_hardcoded_id(monkeypatch):
    monkeypatch.setenv("CTC_META_REFERRAL_IDS", "")
    msg_obj = _msg("5216686666666", DIRECT_TEXT, "mid-env-3",
                   referral=_referral(CTC_AD_SOURCE_ID))
    assert vicky_app._detect_meta_referral_svc(msg_obj, DIRECT_TEXT) == "fp"


# ── Prueba obligatoria 5: IMSS no se afecta ───────────────────────────────────

def test_imss_referral_still_routes_to_imss(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    phone = "5216687777777"
    imss_ref = {
        "source_type": "ad",
        "source_id": "1234567890123",
        "headline": "préstamo IMSS pensionados Ley 73",
        "body": "Pensionado IMSS: conoce tu propuesta de préstamo con tu pensión.",
    }
    vicky_app.handle(_msg(phone, "Quiero información", "mid-imss", referral=imss_ref))
    assert vicky_app.user_state.get(phone, "").startswith("imss_")
    assert sent and "Préstamo IMSS" in sent[0][1]
    assert boardroom_calls == []


def test_imss_direct_text_still_routes_to_imss(monkeypatch):
    sent, boardroom_calls = _base_patches(monkeypatch)
    phone = "5216688888888"
    vicky_app.handle(_msg(phone, "cuánto me prestan con mi pensión", "mid-imss-txt"))
    assert vicky_app.user_state.get(phone, "").startswith("imss_")
    assert boardroom_calls == []
