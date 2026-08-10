"""Endpoint /ext/flow/imss -- ciclo completo cifrado a traves del cliente de
prueba REAL de Flask (no se llaman los handlers de negocio directamente: cada
request pasa por _verify_sig, decrypt_request, dispatch por accion/pantalla,
los handlers reales y encrypt_response, exactamente como lo haria Meta).

El "simulador de Meta" (cifrar solicitud / descifrar respuesta) esta escrito
aqui de forma independiente de imss_flow.py y de test_imss_dynamic_flow_crypto.py
a proposito -- si production y el simulador compartieran un bug de cifrado,
un test que reuse el mismo codigo no lo detectaria.
"""

import json
import os
import sys
from base64 import b64decode, b64encode

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers import algorithms, Cipher, modes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app
import imss_flow


# ─────────────────────────────────────────────────────────────────────────────
# Simulador de Meta (independiente de imss_flow.py)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_key.public_key(), private_pem


def _encrypt_request(public_key, payload: dict) -> tuple[dict, bytes, bytes]:
    aes_key = os.urandom(16)
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv)).encryptor()
    ciphertext = encryptor.update(json.dumps(payload).encode("utf-8")) + encryptor.finalize()
    encrypted_aes_key = public_key.encrypt(
        aes_key, OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    body = {
        "encrypted_flow_data": b64encode(ciphertext + encryptor.tag).decode("utf-8"),
        "encrypted_aes_key": b64encode(encrypted_aes_key).decode("utf-8"),
        "initial_vector": b64encode(iv).decode("utf-8"),
    }
    return body, aes_key, iv


def _decrypt_response(response_text: str, aes_key: bytes, iv: bytes) -> dict:
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    raw = b64decode(response_text)
    ciphertext, tag = raw[:-16], raw[-16:]
    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv, tag)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    return json.loads(plaintext.decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures de aislamiento (mismo patron que test_imss_interactive_buttons.py)
# ─────────────────────────────────────────────────────────────────────────────

PHONE = "5216681234567"


@pytest.fixture
def cliente(monkeypatch, keypair):
    _, private_pem = keypair
    monkeypatch.setattr(vicky_app, "user_state", {})
    monkeypatch.setattr(vicky_app, "user_data", {})
    monkeypatch.setattr(vicky_app, "_state_store", vicky_app.StateStore())
    monkeypatch.setattr(vicky_app, "_verify_sig", lambda raw, hdr: True)
    monkeypatch.setattr(vicky_app, "IMSS_FLOW_PRIVATE_KEY", private_pem)
    monkeypatch.setattr(vicky_app, "WHATSAPP_IMSS_DYNAMIC_FLOW_ENABLED", True)

    sent = []
    monkeypatch.setattr(vicky_app, "send_msg", lambda to, text: sent.append((to, text)) or True)

    advisor_calls = []
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: advisor_calls.append(msg) or True)

    boardroom_calls = []
    monkeypatch.setattr(
        vicky_app, "_notify_boardroom_lead_qualified",
        lambda phone, product_code, data: (
            boardroom_calls.append((phone, product_code, dict(data))) or True),
    )
    monkeypatch.setattr(vicky_app, "_log", lambda *a, **k: None)
    monkeypatch.setattr(vicky_app, "_imss_log_lead_backup", lambda *a, **k: None)

    vicky_app.app.config["TESTING"] = True
    client = vicky_app.app.test_client()
    yield client, sent, advisor_calls, boardroom_calls


def _correlate(phone: str) -> str:
    token = imss_flow.generate_flow_token()
    vicky_app._state_store.aux_set(f"imss_flow_token:{token}", phone, vicky_app._IMSS_FLOW_TOKEN_TTL)
    return token


def _post(client, public_key, payload):
    body, aes_key, iv = _encrypt_request(public_key, payload)
    resp = client.post("/ext/flow/imss", json=body)
    return resp, aes_key, iv


def _post_and_decrypt(client, public_key, payload):
    resp, aes_key, iv = _post(client, public_key, payload)
    assert resp.status_code == 200
    return _decrypt_response(resp.get_data(as_text=True), aes_key, iv)


# ─────────────────────────────────────────────────────────────────────────────
# Casos de borde de transporte (firma / configuracion / cifrado)
# ─────────────────────────────────────────────────────────────────────────────

def test_firma_invalida_devuelve_403(monkeypatch, cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    monkeypatch.setattr(vicky_app, "_verify_sig", lambda raw, hdr: False)
    body, _, _ = _encrypt_request(public_key, {"action": "ping"})
    resp = client.post("/ext/flow/imss", json=body)
    assert resp.status_code == 403


def test_sin_llave_privada_configurada_devuelve_503(monkeypatch, cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    monkeypatch.setattr(vicky_app, "IMSS_FLOW_PRIVATE_KEY", "")
    body, _, _ = _encrypt_request(public_key, {"action": "ping"})
    resp = client.post("/ext/flow/imss", json=body)
    assert resp.status_code == 503


def test_solicitud_indescifrable_devuelve_421(cliente):
    client, *_ = cliente
    resp = client.post("/ext/flow/imss", json={
        "encrypted_flow_data": b64encode(b"basura").decode(),
        "encrypted_aes_key": b64encode(b"basura").decode(),
        "initial_vector": b64encode(b"0" * 16).decode(),
    })
    assert resp.status_code == 421


def test_ping_responde_health_check_cifrado(cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    decrypted = _post_and_decrypt(client, public_key, {"action": "ping"})
    assert decrypted == {"data": {"status": "active"}}


def test_accion_error_responde_ack_cifrado(cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    decrypted = _post_and_decrypt(client, public_key, {
        "action": "error", "data": {"error_message": "algo fallo del lado del cliente"},
    })
    assert decrypted == {"data": {"acknowledged": True}}


def test_flow_token_sin_correlacion_devuelve_sesion_expirada(cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    decrypted = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PROFILE,
        "data": {"profile": "1"}, "flow_token": "imss:no-existe",
    })
    assert decrypted["screen"] == imss_flow.SCREEN_PROFILE
    assert "expiró" in decrypted["data"]["error_message"]


# ─────────────────────────────────────────────────────────────────────────────
# Camino feliz completo: perfil -> pension -> propuesta -> handoff -> SUCCESS
# ─────────────────────────────────────────────────────────────────────────────

def test_camino_feliz_completo_perfil_pensionado_ley73(cliente, keypair):
    client, sent, advisor_calls, boardroom_calls = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r1 = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PROFILE,
        "data": {"profile": "1"}, "flow_token": token,
    })
    assert r1["screen"] == imss_flow.SCREEN_PENSION
    assert vicky_app.user_state[PHONE] == "imss_q_pension_calc"

    r2 = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PENSION,
        "data": {"profile": "1", "pension": "12000"}, "flow_token": token,
    })
    assert r2["screen"] == imss_flow.SCREEN_PROPOSAL
    assert r2["data"]["monto"].startswith("$")
    assert r2["data"]["pago"].startswith("$")
    assert "meses" in r2["data"]["plazo"]
    assert len(advisor_calls) == 1
    assert "PROPUESTA CALCULADA" in advisor_calls[0]

    r3 = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_HANDOFF,
        "data": {"profile": "1", "pension": "12000", "monto": r2["data"]["monto"],
                 "pago": r2["data"]["pago"], "plazo": r2["data"]["plazo"],
                 "nombre": "Juan Pérez", "ciudad": "Culiacán"},
        "flow_token": token,
    })
    assert r3["screen"] == "SUCCESS"
    assert r3["data"]["extension_message_response"]["params"]["resultado"] == "calificado"
    assert vicky_app.user_state[PHONE] == "imss_q_horario_calc"
    assert len(advisor_calls) == 2
    assert "PROSPECTO IMSS CALIFICADO" in advisor_calls[1]
    assert len(boardroom_calls) == 1
    assert any("Christian López revisará personalmente" in txt for _, txt in sent)

    # Idempotencia: mismo flow_token, mismo paso -- Meta reintenta la
    # solicitud (timeout del lado del cliente, doble tap) y no debe volver a
    # notificar al asesor ni a Boardroom.
    r3_retry = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_HANDOFF,
        "data": {"nombre": "Juan Pérez", "ciudad": "Culiacán"}, "flow_token": token,
    })
    assert r3_retry["data"]["extension_message_response"]["params"]["resultado"] == "duplicado"
    assert len(advisor_calls) == 2
    assert len(boardroom_calls) == 1


def test_perfil_por_pensionarse_va_a_pantalla_de_rechazo(cliente, keypair):
    client, sent, advisor_calls, boardroom_calls = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r1 = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PROFILE,
        "data": {"profile": "3"}, "flow_token": token,
    })
    assert r1["screen"] == imss_flow.SCREEN_REJECTED
    assert len(advisor_calls) == 1
    assert "INTERÉS FUTURO" in advisor_calls[0]

    r2 = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_REJECTED,
        "data": {}, "flow_token": token,
    })
    assert r2["screen"] == "SUCCESS"
    assert r2["data"]["extension_message_response"]["params"]["resultado"] == "no_calificado"
    assert vicky_app.user_state[PHONE] == "imss_post_cierre"
    assert len(boardroom_calls) == 0


def test_perfil_invalido_repite_pantalla_con_error(cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PROFILE,
        "data": {"profile": "9"}, "flow_token": token,
    })
    assert r["screen"] == imss_flow.SCREEN_PROFILE
    assert "error_message" in r["data"]


def test_pension_bajo_minimo_repite_pantalla_con_error_y_no_notifica(cliente, keypair):
    client, sent, advisor_calls, boardroom_calls = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PENSION,
        "data": {"profile": "1", "pension": "1500"}, "flow_token": token,
    })
    assert r["screen"] == imss_flow.SCREEN_PENSION
    assert "error_message" in r["data"]
    assert len(advisor_calls) == 0


def test_pension_invalida_repite_pantalla_con_error(cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_PENSION,
        "data": {"profile": "1", "pension": "no es numero"}, "flow_token": token,
    })
    assert r["screen"] == imss_flow.SCREEN_PENSION
    assert "error_message" in r["data"]


def test_handoff_sin_nombre_o_ciudad_repite_pantalla_con_error(cliente, keypair):
    client, sent, advisor_calls, boardroom_calls = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": imss_flow.SCREEN_HANDOFF,
        "data": {"nombre": "", "ciudad": "Culiacán"}, "flow_token": token,
    })
    assert r["screen"] == imss_flow.SCREEN_HANDOFF
    assert "error_message" in r["data"]
    assert len(advisor_calls) == 0
    assert len(boardroom_calls) == 0


def test_pantalla_desconocida_responde_ack_sin_lanzar(cliente, keypair):
    client, *_ = cliente
    public_key, _ = keypair
    token = _correlate(PHONE)

    r = _post_and_decrypt(client, public_key, {
        "action": "data_exchange", "screen": "PANTALLA_QUE_NO_EXISTE",
        "data": {}, "flow_token": token,
    })
    assert r == {"data": {"acknowledged": True}}
