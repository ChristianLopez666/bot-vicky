# imss_flow.py — Flow dinamico real de IMSS (perfil -> pension -> propuesta ->
# datos de contacto), con endpoint cifrado (data_exchange).
#
# Contrato de Meta verificado contra documentacion oficial en vivo el
# 2026-08-10 (developers.facebook.com/docs/whatsapp/flows/..., paginas
# "Implementar puntos finales para flujos", "Flow JSON", "Componentes",
# "Enviar un proceso", changelog de versiones soportadas). Fuente unica de
# verdad para los valores de este modulo -- ver
# WA2B_DYNAMIC_FLOW_REPORT_2026-08-10.md seccion "Documentacion verificada".
#
# REGLA CRITICA (igual que en la version estatica anterior, sin excepcion):
# este modulo NUNCA calcula una propuesta financiera. No conoce la tasa, no
# reimplementa la busqueda binaria. La unica autoridad de calculo sigue
# siendo calcular_propuesta_imss() en app.py -- este modulo solo transporta,
# valida y cifra/descifra datos.

from __future__ import annotations

import json
import uuid
from base64 import b64decode, b64encode
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import algorithms, Cipher, modes
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# ── Versiones (verificadas contra el changelog oficial, actualizado 2025-11-07,
# "Versiones soportadas actualmente" -- Flow JSON 7.3 recomendada, API de
# datos 4.0 recomendada, version de mensaje 3 recomendada) ────────────────────
FLOW_JSON_VERSION = "7.3"
DATA_API_VERSION = "4.0"
FLOW_MESSAGE_VERSION = "3"

FLOW_TOKEN_PREFIX = "imss:"

SCREEN_PROFILE = "IMSS_PROFILE"
SCREEN_PENSION = "IMSS_PENSION"
SCREEN_PROPOSAL = "IMSS_PROPOSAL"
SCREEN_HANDOFF = "IMSS_HANDOFF"
SCREEN_REJECTED = "IMSS_REJECTED"
SCREEN_SUCCESS = "SUCCESS"  # nombre reservado por Meta, no reutilizable para otra pantalla

VALID_PROFILES = {"1", "2", "3", "4"}

# ── Flow JSON ──────────────────────────────────────────────────────────────────
# Cada Footer que necesita datos calculados en el backend usa data_exchange
# (Meta llama al endpoint). Las transiciones cuyo destino y datos ya se
# conocen sin backend usan navigate (Meta recomienda explicitamente evitar
# el endpoint cuando no es necesario -- IMSS_PROPOSAL -> IMSS_HANDOFF y el
# "Cambiar monto o plazo" de vuelta a IMSS_PENSION son navigate).
IMSS_FLOW_JSON: Dict[str, Any] = {
    "version": FLOW_JSON_VERSION,
    "data_api_version": DATA_API_VERSION,
    "routing_model": {
        SCREEN_PROFILE: [SCREEN_PENSION, SCREEN_REJECTED],
        SCREEN_PENSION: [SCREEN_PROPOSAL],
        SCREEN_PROPOSAL: [SCREEN_HANDOFF],
        SCREEN_HANDOFF: [SCREEN_PENSION],
        SCREEN_REJECTED: [],
    },
    "screens": [
        {
            "id": SCREEN_PROFILE,
            "title": "Préstamo IMSS",
            "data": {},
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "¿Cuál describe mejor tu situación?"},
                    {
                        "type": "Form",
                        "name": "form_profile",
                        "children": [
                            {
                                "type": "RadioButtonsGroup",
                                "name": "profile",
                                "label": "Selecciona una opción",
                                "required": True,
                                "data-source": [
                                    {"id": "1", "title": "Ya recibo pensión IMSS Ley 73"},
                                    {"id": "2", "title": "Recibo pensión, no sé si es Ley 73"},
                                    {"id": "3", "title": "Estoy por pensionarme"},
                                    {"id": "4", "title": "Ayudo a un familiar pensionado"},
                                ],
                            },
                            {
                                "type": "Footer",
                                "label": "Continuar",
                                "on-click-action": {
                                    "name": "data_exchange",
                                    "payload": {"profile": "${form.profile}"},
                                },
                            },
                        ],
                    },
                ],
            },
        },
        {
            "id": SCREEN_PENSION,
            "title": "Préstamo IMSS",
            "data": {
                "profile": {"type": "string", "__example__": "1"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "¿Cuánto recibes al mes?"},
                    {
                        "type": "Form",
                        "name": "form_pension",
                        "children": [
                            {
                                "type": "TextInput",
                                "name": "pension",
                                "label": "Pensión mensual (MXN)",
                                "input-type": "number",
                                "required": True,
                                "helper-text": "Escribe solo tu pensión mensual",
                            },
                            {
                                "type": "Footer",
                                "label": "Continuar",
                                "on-click-action": {
                                    "name": "data_exchange",
                                    "payload": {
                                        "profile": "${data.profile}",
                                        "pension": "${form.pension}",
                                    },
                                },
                            },
                        ],
                    },
                ],
            },
        },
        {
            "id": SCREEN_PROPOSAL,
            "title": "Préstamo IMSS",
            "data": {
                "profile": {"type": "string", "__example__": "1"},
                "pension": {"type": "string", "__example__": "12000"},
                "monto": {"type": "string", "__example__": "$45,000"},
                "pago": {"type": "string", "__example__": "$1,362"},
                "plazo": {"type": "string", "__example__": "48 meses"},
                "tasa": {"type": "string", "__example__": "22.39%"},
                "cat": {"type": "string", "__example__": "24.8%"},
                "vrim_teaser": {"type": "string", "__example__": ""},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "Tu propuesta estimada"},
                    {"type": "TextSubheading", "text": "Monto aproximado"},
                    {"type": "TextBody", "text": "${data.monto}"},
                    {"type": "TextSubheading", "text": "Pago mensual"},
                    {"type": "TextBody", "text": "${data.pago}"},
                    {"type": "TextSubheading", "text": "Plazo"},
                    {"type": "TextBody", "text": "${data.plazo}"},
                    {
                        "type": "TextCaption",
                        "text": "Tasa fija anual ${data.tasa} sin IVA · CAT informativo ${data.cat} sin IVA. Sujeto a validación final.",
                    },
                    {
                        "type": "TextBody",
                        "text": "${data.vrim_teaser}",
                        "visible": "${data.vrim_teaser != \"\"}",
                    },
                    {
                        "type": "Footer",
                        "label": "Continuar",
                        "on-click-action": {
                            "name": "navigate",
                            "next": {"type": "screen", "name": SCREEN_HANDOFF},
                            "payload": {
                                "profile": "${data.profile}",
                                "pension": "${data.pension}",
                                "monto": "${data.monto}",
                                "pago": "${data.pago}",
                                "plazo": "${data.plazo}",
                            },
                        },
                    },
                ],
            },
        },
        {
            "id": SCREEN_HANDOFF,
            "title": "Préstamo IMSS",
            "terminal": True,
            "success": True,
            "data": {
                "profile": {"type": "string", "__example__": "1"},
                "pension": {"type": "string", "__example__": "12000"},
                "monto": {"type": "string", "__example__": "$45,000"},
                "pago": {"type": "string", "__example__": "$1,362"},
                "plazo": {"type": "string", "__example__": "48 meses"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "Tu asesor te atenderá personalmente"},
                    {
                        "type": "TextBody",
                        "text": "Christian López continuará tu atención por WhatsApp. Es fácil y sin costo.",
                    },
                    {
                        "type": "Form",
                        "name": "form_handoff",
                        "children": [
                            {
                                "type": "TextInput",
                                "name": "nombre",
                                "label": "Nombre completo",
                                "input-type": "text",
                                "required": True,
                            },
                            {
                                "type": "TextInput",
                                "name": "ciudad",
                                "label": "Ciudad",
                                "input-type": "text",
                                "required": True,
                            },
                            {
                                "type": "EmbeddedLink",
                                "text": "Cambiar monto o plazo",
                                "on-click-action": {
                                    "name": "navigate",
                                    "next": {"type": "screen", "name": SCREEN_PENSION},
                                    "payload": {"profile": "${data.profile}"},
                                },
                            },
                            {
                                "type": "Footer",
                                "label": "Quiero que me contacten",
                                "on-click-action": {
                                    "name": "data_exchange",
                                    "payload": {
                                        "profile": "${data.profile}",
                                        "pension": "${data.pension}",
                                        "monto": "${data.monto}",
                                        "pago": "${data.pago}",
                                        "plazo": "${data.plazo}",
                                        "nombre": "${form.nombre}",
                                        "ciudad": "${form.ciudad}",
                                    },
                                },
                            },
                        ],
                    },
                ],
            },
        },
        {
            "id": SCREEN_REJECTED,
            "title": "Préstamo IMSS",
            "terminal": True,
            "success": True,
            "data": {},
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "¡Gracias por tu interés!"},
                    {
                        "type": "TextBody",
                        "text": "Para calcular una propuesta necesitamos que tu pensión ya esté activa. "
                        "Si gustas, Christian puede contactarte cuando te pensiones.",
                    },
                    {
                        "type": "Form",
                        "name": "form_rejected",
                        "children": [
                            {
                                "type": "Footer",
                                "label": "Entendido",
                                "on-click-action": {"name": "data_exchange", "payload": {}},
                            },
                        ],
                    },
                ],
            },
        },
    ],
}


def generate_flow_token() -> str:
    """flow_token correlacionable sin PII: solo un identificador aleatorio
    con el prefijo del flow. Nunca telefono, nombre ni pension."""
    return f"{FLOW_TOKEN_PREFIX}{uuid.uuid4().hex}"


def build_flow_message_payload(to: str, flow_id: str, flow_token: str) -> Dict[str, Any]:
    """Payload de envio del Flow (Cloud API). Verificado contra la guia
    oficial "Enviar un proceso": interactive.type="flow",
    action.parameters.flow_message_version="3". No hace ningun request HTTP.

    flow_action="navigate" hacia IMSS_PROFILE: la primera pantalla no tiene
    parametros que dependan del backend, asi que se evita llamar al endpoint
    en INIT (recomendacion explicita de Meta)."""
    flow_id = (flow_id or "").strip()
    if not flow_id:
        raise ValueError("flow_id no puede estar vacio")
    if not flow_token or not flow_token.startswith(FLOW_TOKEN_PREFIX):
        raise ValueError("flow_token invalido")

    return {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": "Préstamo IMSS"},
            "body": {
                "text": "Responde unas preguntas rápidas y te preparo una propuesta estimada, "
                "con el resultado en la misma conversación."
            },
            "footer": {"text": "Christian López · COHIFIS"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": FLOW_MESSAGE_VERSION,
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": "Calcular propuesta",
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": SCREEN_PROFILE, "data": {}},
                },
            },
        },
    }


# ── Cifrado del endpoint (data_api_version 4.0, compatible con 3.0) ───────────
# Algoritmo verificado contra el ejemplo de codigo oficial Python/Django de
# "Implementar puntos finales para flujos": RSA-OAEP (SHA-256, MGF1-SHA256)
# para la llave AES de 128 bits; AES-128-GCM con tag de 16 bytes al final del
# array cifrado; respuesta cifrada con la MISMA llave y el IV con todos los
# bits invertidos (XOR 0xFF), AAD vacio, tag de 128 bits.
_AES_TAG_LEN = 16


class FlowDecryptionError(Exception):
    """La solicitud no se pudo descifrar -- el caller debe responder HTTP 421
    (codigo exacto documentado por Meta para forzar el recambio de llave
    publica del lado del cliente)."""


def decrypt_request(
    encrypted_flow_data_b64: str,
    encrypted_aes_key_b64: str,
    initial_vector_b64: str,
    private_key_pem: str,
) -> Tuple[Dict[str, Any], bytes, bytes]:
    """(payload_descifrado, aes_key, iv). Lanza FlowDecryptionError si algo
    falla -- nunca deja escapar el traceback original (podria filtrar
    detalles de la clave privada en logs)."""
    try:
        flow_data = b64decode(encrypted_flow_data_b64)
        iv = b64decode(initial_vector_b64)
        encrypted_aes_key = b64decode(encrypted_aes_key_b64)

        private_key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        aes_key = private_key.decrypt(
            encrypted_aes_key,
            OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )

        encrypted_body = flow_data[:-_AES_TAG_LEN]
        tag = flow_data[-_AES_TAG_LEN:]
        decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv, tag)).decryptor()
        decrypted_bytes = decryptor.update(encrypted_body) + decryptor.finalize()
        payload = json.loads(decrypted_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("decrypted payload is not a JSON object")
        return payload, aes_key, iv
    except Exception as e:
        raise FlowDecryptionError(str(e)) from e


def encrypt_response(response: Dict[str, Any], aes_key: bytes, iv: bytes) -> str:
    """Cifra la respuesta con la misma llave AES y el IV invertido bit a
    bit, tal como especifica la guia oficial. Devuelve base64 (texto plano,
    Content-Type: text/plain en la respuesta HTTP -- responsabilidad del
    caller en app.py)."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv)).encryptor()
    ciphertext = encryptor.update(json.dumps(response).encode("utf-8")) + encryptor.finalize()
    return b64encode(ciphertext + encryptor.tag).decode("utf-8")


# ── Construccion de respuestas (formas verificadas contra la guia oficial) ────

def build_next_screen_response(screen: str, data: Dict[str, Any], error_message: Optional[str] = None) -> Dict[str, Any]:
    payload_data = dict(data)
    if error_message:
        payload_data["error_message"] = error_message
    return {"screen": screen, "data": payload_data}


def build_success_response(flow_token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    all_params = {"flow_token": flow_token}
    if params:
        all_params.update(params)
    return {
        "screen": SCREEN_SUCCESS,
        "data": {"extension_message_response": {"params": all_params}},
    }


def build_health_check_response() -> Dict[str, Any]:
    return {"data": {"status": "active"}}


def build_error_ack_response() -> Dict[str, Any]:
    return {"data": {"acknowledged": True}}


# ── Parseo defensivo de la solicitud descifrada ────────────────────────────────

def parse_decrypted_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extrae version/action/screen/data/flow_token de forma defensiva.
    Nunca lanza -- campos ausentes quedan como valores por defecto seguros,
    y el caller decide que hacer con una solicitud incompleta."""
    if not isinstance(payload, dict):
        payload = {}
    return {
        "version": str(payload.get("version") or ""),
        "action": str(payload.get("action") or ""),
        "screen": payload.get("screen") or None,
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        "flow_token": str(payload.get("flow_token") or ""),
    }


def validate_profile(raw: Any) -> Optional[str]:
    profile = str(raw).strip() if raw is not None else ""
    return profile if profile in VALID_PROFILES else None


def validate_pension(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 200000:
        return None
    return value


def validate_nonempty_text(raw: Any, max_len: int = 200) -> Optional[str]:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    return text[:max_len]
