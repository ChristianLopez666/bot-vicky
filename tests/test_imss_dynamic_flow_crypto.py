"""Cifrado del endpoint del Flow dinamico IMSS -- ida y vuelta real.

No se confia en que el algoritmo "deberia" funcionar: cada test genera un
par de llaves RSA de verdad, SIMULA exactamente lo que hace el cliente de
WhatsApp (cifra una llave AES con la llave publica + cifra un payload con
AES-GCM), llama a decrypt_request() real, y verifica que recupera el
payload original. Para la respuesta, se cifra con encrypt_response() real y
se descifra con una implementacion de "simulador de Meta" separada e
independiente (mismo algoritmo, pero escrita aparte para no compartir un
bug con el codigo de produccion).

Ningun test llama a Meta real ni a ningun servicio externo -- todo el
cifrado ocurre en el proceso de test.
"""

import json
import re
import os
import sys
from base64 import b64decode, b64encode

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers import algorithms, Cipher, modes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import imss_flow


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: par de llaves RSA real (2048 bits, como exige Meta) + helpers que
# simulan el lado de WhatsApp de forma INDEPENDIENTE del codigo de produccion.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_key = private_key.public_key()
    return private_key, public_key, private_pem


def _meta_encrypts_request(public_key, payload: dict) -> tuple[str, str, str, bytes, bytes]:
    """Simula lo que hace el cliente de WhatsApp/Meta al ARMAR una solicitud
    para nuestro endpoint: genera una llave AES-128 y un IV de 16 bytes,
    cifra la llave con RSA-OAEP (SHA-256/MGF1-SHA256) usando la llave
    PUBLICA, y cifra el payload con AES-GCM. Devuelve los 3 campos en base64
    que nuestro endpoint recibiria, mas la llave/iv en claro (para que el
    test pueda comparar despues)."""
    aes_key = os.urandom(16)
    iv = os.urandom(16)

    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv)).encryptor()
    ciphertext = encryptor.update(json.dumps(payload).encode("utf-8")) + encryptor.finalize()
    encrypted_flow_data = ciphertext + encryptor.tag

    encrypted_aes_key = public_key.encrypt(
        aes_key,
        OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )

    return (
        b64encode(encrypted_flow_data).decode("utf-8"),
        b64encode(encrypted_aes_key).decode("utf-8"),
        b64encode(iv).decode("utf-8"),
        aes_key,
        iv,
    )


def _meta_decrypts_response(response_b64: str, aes_key: bytes, iv: bytes) -> dict:
    """Simula lo que hace el cliente de WhatsApp al LEER nuestra respuesta:
    invierte el IV (XOR 0xFF cada byte) y descifra con AES-GCM usando la
    MISMA llave AES de la solicitud. Implementacion independiente de
    imss_flow.encrypt_response() a proposito -- si comparten un bug, este
    test no lo detectaria."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    raw = b64decode(response_b64)
    ciphertext, tag = raw[:-16], raw[-16:]
    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv, tag)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    return json.loads(plaintext.decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Ida y vuelta real
# ─────────────────────────────────────────────────────────────────────────────

def test_decrypt_request_recupera_el_payload_original(keypair):
    private_key, public_key, private_pem = keypair
    original = {
        "version": "4.0",
        "action": "data_exchange",
        "screen": "IMSS_PENSION",
        "data": {"profile": "1"},
        "flow_token": "imss:abc123",
    }
    enc_data, enc_key, iv_b64, _, _ = _meta_encrypts_request(public_key, original)

    decrypted, aes_key, iv = imss_flow.decrypt_request(enc_data, enc_key, iv_b64, private_pem)

    assert decrypted == original
    assert len(aes_key) == 16
    assert len(iv) == 16


def test_encrypt_response_es_descifrable_por_un_simulador_independiente(keypair):
    private_key, public_key, private_pem = keypair
    original = {"version": "4.0", "action": "INIT", "screen": None, "data": {}, "flow_token": "imss:xyz"}
    enc_data, enc_key, iv_b64, aes_key_claro, iv_claro = _meta_encrypts_request(public_key, original)

    _, aes_key, iv = imss_flow.decrypt_request(enc_data, enc_key, iv_b64, private_pem)
    assert aes_key == aes_key_claro
    assert iv == iv_claro

    response = imss_flow.build_next_screen_response("IMSS_PENSION", {"profile": "1"})
    response_b64 = imss_flow.encrypt_response(response, aes_key, iv)

    recovered = _meta_decrypts_response(response_b64, aes_key, iv)
    assert recovered == response


def test_roundtrip_completo_INIT_hasta_respuesta(keypair):
    """Ciclo completo: solicitud real cifrada -> decrypt_request real ->
    logica de negocio (aqui solo un builder) -> encrypt_response real ->
    un tercero (el simulador) puede leerla."""
    private_key, public_key, private_pem = keypair
    request_payload = {
        "version": "4.0", "action": "ping", "screen": None, "data": {}, "flow_token": "",
    }
    enc_data, enc_key, iv_b64, _, _ = _meta_encrypts_request(public_key, request_payload)

    decrypted, aes_key, iv = imss_flow.decrypt_request(enc_data, enc_key, iv_b64, private_pem)
    parsed = imss_flow.parse_decrypted_request(decrypted)
    assert parsed["action"] == "ping"

    response = imss_flow.build_health_check_response()
    response_b64 = imss_flow.encrypt_response(response, aes_key, iv)
    assert _meta_decrypts_response(response_b64, aes_key, iv) == {"data": {"status": "active"}}


def test_decrypt_con_llave_privada_incorrecta_lanza_flow_decryption_error(keypair):
    _, public_key, _ = keypair
    otra_privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    otra_pem = otra_privada.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("utf-8")

    enc_data, enc_key, iv_b64, _, _ = _meta_encrypts_request(public_key, {"action": "ping"})

    with pytest.raises(imss_flow.FlowDecryptionError):
        imss_flow.decrypt_request(enc_data, enc_key, iv_b64, otra_pem)


def test_decrypt_datos_corruptos_lanza_flow_decryption_error(keypair):
    _, public_key, private_pem = keypair
    enc_data, enc_key, iv_b64, _, _ = _meta_encrypts_request(public_key, {"action": "ping"})
    corrupto = b64encode(b64decode(enc_data)[:-1] + b"\x00").decode("utf-8")

    with pytest.raises(imss_flow.FlowDecryptionError):
        imss_flow.decrypt_request(corrupto, enc_key, iv_b64, private_pem)


def test_decrypt_base64_invalido_no_crashea_fuera_de_flow_decryption_error(keypair):
    _, _, private_pem = keypair
    with pytest.raises(imss_flow.FlowDecryptionError):
        imss_flow.decrypt_request("no es base64 valido!!", "tampoco", "ni esto", private_pem)


def test_response_iv_esta_invertido_bit_a_bit(keypair):
    """Verifica explicitamente la regla 'XOR cada byte con 0xFF' -- un IV
    de puros ceros debe convertirse en puros 0xFF."""
    aes_key = os.urandom(16)
    iv = b"\x00" * 16
    response = {"data": {"status": "active"}}
    response_b64 = imss_flow.encrypt_response(response, aes_key, iv)

    flipped_iv_esperado = b"\xff" * 16
    raw = b64decode(response_b64)
    ciphertext, tag = raw[:-16], raw[-16:]
    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv_esperado, tag)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    assert json.loads(plaintext) == response


def test_no_expone_la_llave_privada_en_el_mensaje_de_error(keypair):
    _, public_key, private_pem = keypair
    enc_data, enc_key, iv_b64, _, _ = _meta_encrypts_request(public_key, {"action": "ping"})
    corrupto = b64encode(b"\x00" * 32).decode("utf-8")
    try:
        imss_flow.decrypt_request(corrupto, enc_key, iv_b64, private_pem)
        assert False, "deberia haber lanzado FlowDecryptionError"
    except imss_flow.FlowDecryptionError as e:
        assert "BEGIN" not in str(e)
        assert "PRIVATE KEY" not in str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Formas de respuesta (sin cifrado) -- verificadas contra los ejemplos
# textuales exactos de la guia oficial
# ─────────────────────────────────────────────────────────────────────────────

def test_build_next_screen_response_forma_exacta():
    r = imss_flow.build_next_screen_response("IMSS_PENSION", {"profile": "1"})
    assert r == {"screen": "IMSS_PENSION", "data": {"profile": "1"}}


def test_build_next_screen_response_con_error_message():
    r = imss_flow.build_next_screen_response("IMSS_PENSION", {}, error_message="Monto inválido")
    assert r["data"]["error_message"] == "Monto inválido"


def test_build_success_response_forma_exacta():
    r = imss_flow.build_success_response("imss:abc", {"resultado": "ok"})
    assert r["screen"] == "SUCCESS"
    assert r["data"]["extension_message_response"]["params"]["flow_token"] == "imss:abc"
    assert r["data"]["extension_message_response"]["params"]["resultado"] == "ok"


def test_build_health_check_response_forma_exacta():
    assert imss_flow.build_health_check_response() == {"data": {"status": "active"}}


def test_build_error_ack_response_forma_exacta():
    assert imss_flow.build_error_ack_response() == {"data": {"acknowledged": True}}


def test_parse_decrypted_request_defensivo_nunca_lanza():
    assert imss_flow.parse_decrypted_request({}) == {
        "version": "", "action": "", "screen": None, "data": {}, "flow_token": "",
    }
    assert imss_flow.parse_decrypted_request(None) == {
        "version": "", "action": "", "screen": None, "data": {}, "flow_token": "",
    }
    assert imss_flow.parse_decrypted_request("texto")["action"] == ""


def test_validate_profile():
    assert imss_flow.validate_profile("1") == "1"
    assert imss_flow.validate_profile("5") is None
    assert imss_flow.validate_profile(None) is None
    assert imss_flow.validate_profile("") is None


def test_validate_pension():
    assert imss_flow.validate_pension("12000") == 12000.0
    assert imss_flow.validate_pension("0") is None
    assert imss_flow.validate_pension("-500") is None
    assert imss_flow.validate_pension("999999") is None
    assert imss_flow.validate_pension("no es numero") is None
    assert imss_flow.validate_pension(None) is None


def test_validate_nonempty_text():
    assert imss_flow.validate_nonempty_text("  Juan Pérez  ") == "Juan Pérez"
    assert imss_flow.validate_nonempty_text("") is None
    assert imss_flow.validate_nonempty_text(None) is None
    assert imss_flow.validate_nonempty_text("x" * 300, max_len=200) == "x" * 200


# ─────────────────────────────────────────────────────────────────────────────
# Estructura del Flow JSON (sin cifrado, sin HTTP)
# ─────────────────────────────────────────────────────────────────────────────

def test_flow_json_es_serializable():
    raw = json.dumps(imss_flow.IMSS_FLOW_JSON, ensure_ascii=False)
    reparsed = json.loads(raw)
    assert reparsed["version"] == imss_flow.FLOW_JSON_VERSION
    assert reparsed["data_api_version"] == imss_flow.DATA_API_VERSION


def test_flow_json_screen_ids_unicos_y_esperados():
    ids = [s["id"] for s in imss_flow.IMSS_FLOW_JSON["screens"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == {
        imss_flow.SCREEN_PROFILE, imss_flow.SCREEN_PENSION,
        imss_flow.SCREEN_PROPOSAL, imss_flow.SCREEN_HANDOFF,
        imss_flow.SCREEN_REJECTED,
    }


def test_flow_json_routing_model_referencia_solo_screens_validos():
    valid_ids = {s["id"] for s in imss_flow.IMSS_FLOW_JSON["screens"]}
    routing = imss_flow.IMSS_FLOW_JSON["routing_model"]
    assert set(routing.keys()) <= valid_ids
    for targets in routing.values():
        assert set(targets) <= valid_ids


def test_flow_json_pantallas_terminales_son_handoff_y_rejected():
    terminales = {s["id"] for s in imss_flow.IMSS_FLOW_JSON["screens"] if s.get("terminal")}
    assert terminales == {imss_flow.SCREEN_HANDOFF, imss_flow.SCREEN_REJECTED}


def _componentes(screen):
    """Recorre TODO el arbol de la pantalla, no solo los hijos de Form.

    Las versiones anteriores de estas pruebas solo miraban dentro de Form y se
    volvieron ciegas al meter NavigationList, que vive fuera. Un chequeo que
    solo cubre parte del arbol se ve igual de verde que uno que cubre todo.
    """
    pendientes = [screen["layout"]]
    while pendientes:
        nodo = pendientes.pop()
        if isinstance(nodo, dict):
            if "type" in nodo:
                yield nodo
            pendientes.extend(nodo.values())
        elif isinstance(nodo, list):
            pendientes.extend(nodo)


def _acciones(screen):
    for comp in _componentes(screen):
        accion = comp.get("on-click-action")
        if isinstance(accion, dict):
            yield accion


def test_flow_json_data_exchange_no_lleva_next_estatico():
    """data_exchange deja que el ENDPOINT decida la siguiente pantalla -- un
    'next' estatico ahi seria ignorado y confunde la intencion del diseno."""
    for screen in imss_flow.IMSS_FLOW_JSON["screens"]:
        for accion in _acciones(screen):
            if accion.get("name") == "data_exchange":
                assert "next" not in accion


def test_flow_json_navigate_actions_apuntan_a_screens_validos():
    valid_ids = {s["id"] for s in imss_flow.IMSS_FLOW_JSON["screens"]}
    for screen in imss_flow.IMSS_FLOW_JSON["screens"]:
        for accion in _acciones(screen):
            if accion.get("name") == "navigate":
                assert accion["next"]["name"] in valid_ids


def test_flow_json_referencias_dinamicas_son_cadena_completa():
    """Flow JSON solo sustituye ${data.x} cuando ocupa TODA la cadena.

    Incrustado dentro de una frase lo imprime literal: el 2026-08-18 los
    clientes vieron "Tasa fija anual ${data.tasa} sin IVA" en la pantalla de
    propuesta. Lo que necesite texto alrededor se arma en el backend y viaja
    como un solo campo (ver disclaimer y vrim_teaser).
    """
    for screen in imss_flow.IMSS_FLOW_JSON["screens"]:
        for comp in _componentes(screen):
            texto = comp.get("text")
            if isinstance(texto, str) and "${" in texto:
                assert re.fullmatch(r"\$\{[^}]+\}", texto.strip()), (
                    f'{screen["id"]}/{comp.get("type")}: referencia incrustada -> {texto!r}'
                )


def test_flow_json_no_contiene_secretos():
    raw = json.dumps(imss_flow.IMSS_FLOW_JSON)
    for forbidden in ("META_TOKEN", "PRIVATE_KEY", "Bearer ", "api_key", "secret"):
        assert forbidden.lower() not in raw.lower()


def test_flow_json_profile_options_coinciden_con_perfiles_validos():
    profile_screen = next(
        s for s in imss_flow.IMSS_FLOW_JSON["screens"] if s["id"] == imss_flow.SCREEN_PROFILE
    )
    lista = next(c for c in _componentes(profile_screen) if c["type"] == "NavigationList")
    ids = {item["id"] for item in lista["list-items"]}
    assert ids == imss_flow.VALID_PROFILES
    # Cada renglon avanza por si solo: sin boton de confirmar, y llevando su
    # propio perfil. Si alguien lo cambia a payload dinamico, todos mandarian
    # lo mismo y el enrutamiento del endpoint se rompe en silencio.
    for item in lista["list-items"]:
        assert item["on-click-action"]["payload"] == {"profile": item["id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Sender (build_flow_message_payload) -- puro, sin HTTP
# ─────────────────────────────────────────────────────────────────────────────

def test_build_flow_message_payload_forma_verificada():
    token = imss_flow.generate_flow_token()
    payload = imss_flow.build_flow_message_payload("6681234567", "123456789", token)
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "flow"
    params = payload["interactive"]["action"]["parameters"]
    assert params["flow_message_version"] == "3"
    assert params["flow_id"] == "123456789"
    assert params["flow_token"] == token
    assert params["flow_action"] == "navigate"
    assert params["flow_action_payload"]["screen"] == imss_flow.SCREEN_PROFILE
    # Regresion 2026-08-18: Meta rechaza flow_action_payload con "data": {}
    # con "(#131009) Parameter value is not valid". El campo es opcional y
    # solo debe viajar si lleva contenido real.
    assert "data" not in params["flow_action_payload"]


def test_build_flow_message_payload_respeta_limites_de_whatsapp():
    """Limites de la tarjeta interactiva. El del boton es el que muerde: 20
    caracteres. Un texto mas largo lo rechaza Meta al enviar, o sea que se
    descubre en produccion y no en pruebas."""
    payload = imss_flow.build_flow_message_payload(
        "6681234567", "123456789", imss_flow.generate_flow_token())
    interactive = payload["interactive"]
    cta = interactive["action"]["parameters"]["flow_cta"]
    assert len(cta) <= 20, f"flow_cta de {len(cta)} caracteres: {cta!r}"
    assert len(interactive["header"]["text"]) <= 60
    assert len(interactive["footer"]["text"]) <= 60
    assert len(interactive["body"]["text"]) <= 1024


def test_build_flow_message_payload_sin_flow_id_falla():
    with pytest.raises(ValueError):
        imss_flow.build_flow_message_payload("6681234567", "", imss_flow.generate_flow_token())


def test_generate_flow_token_sin_pii():
    token = imss_flow.generate_flow_token()
    assert token.startswith("imss:")
    assert "5216" not in token
