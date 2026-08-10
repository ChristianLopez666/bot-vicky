# whatsapp_interactive.py — WA-1: capa de Interactive Messages (Reply Buttons /
# List Messages) para Vicky Redes.
#
# Modulo puro: sin Flask, sin requests, sin logging de negocio. Solo:
#   - normaliza mensajes entrantes (text / button legacy / interactive.*)
#   - construye payloads salientes (reply buttons / list message)
#   - resuelve el puente interactive_id -> servicio canonico existente
#
# El envio real (HTTP a Graph API) vive en app.py, reutilizando _wa_post ya
# existente -- este modulo nunca abre su propio cliente HTTP.
#
# Fuera de alcance de WA-1 (ver auditoria previa, seccion GAP-WA-009 y plan
# WA-0..WA-11): procesamiento de nfm_reply/response_json (WhatsApp Flows).
# Aqui solo se reconoce ese tipo de forma segura, sin procesarlo.

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ── Kinds del contrato normalizado ─────────────────────────────────────────────
KIND_TEXT = "text"
KIND_LEGACY_TEMPLATE_BUTTON = "legacy_template_button"
KIND_BUTTON_REPLY = "interactive_button_reply"
KIND_LIST_REPLY = "interactive_list_reply"
KIND_NFM_REPLY = "interactive_nfm_reply"  # reconocido, NO procesado (Flows = WA-2+)
KIND_INTERACTIVE_UNKNOWN = "interactive_unknown"
KIND_UNKNOWN = "unknown"

# Limites documentados de la Cloud API de Meta para Interactive Messages.
# NOTA: tomados de conocimiento interno del modelo, NO verificados contra
# documentacion oficial en vivo en esta sesion (META_DOC_VERIFIED=false en el
# informe de esta microfase). Se validan localmente para nunca construir un
# payload que Meta rechazaria, pero deben re-confirmarse antes de WA-2.
MAX_REPLY_BUTTONS = 3
MAX_BUTTON_ID_LEN = 256
MAX_BUTTON_TITLE_LEN = 20
MAX_LIST_BUTTON_TEXT_LEN = 20
MAX_LIST_ROWS_TOTAL = 10
MAX_LIST_ROW_ID_LEN = 200
MAX_LIST_ROW_TITLE_LEN = 24
MAX_LIST_ROW_DESCRIPTION_LEN = 72
MAX_LIST_SECTION_TITLE_LEN = 24


_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"", "0", "false", "no", "off"}


def parse_bool_flag(raw: Optional[str]) -> Tuple[bool, bool]:
    """Coerciona un valor crudo de variable de entorno a booleano, con el
    mismo vocabulario que ya usan BUS_ENABLED/BOARDROOM_ENABLED en app.py.

    Devuelve (valor, invalido):
      - ausente/vacio          -> (False, False)
      - reconocido true/false  -> (valor, False)
      - cualquier otra cosa    -> (False, True)  -- el caller decide si loguea
    """
    v = (raw or "").strip().lower()
    if v in _BOOL_TRUE:
        return True, False
    if v in _BOOL_FALSE:
        return False, False
    return False, True


def normalize_incoming_message(msg_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza un mensaje entrante de la Cloud API a un contrato minimo comun.

    No reemplaza `_message_text`/`_canonical_message_type` de app.py (que ya
    cubren text/button legacy correctamente) -- esta funcion es la que agrega
    soporte a `type: "interactive"` sin tocar lo existente. Es seguro llamarla
    con cualquier msg_obj: mensajes text/button legacy tambien se reconocen,
    para que el caller pueda usar un solo punto de entrada si lo necesita.

    Devuelve siempre un dict con estas claves (nunca lanza excepcion):
        kind, text, interactive_id, interactive_title,
        interactive_description, raw_type, message_id
    """
    mtype = msg_obj.get("type", "") or ""
    message_id = msg_obj.get("id", "") or ""

    base = {
        "kind": KIND_UNKNOWN,
        "text": "",
        "interactive_id": None,
        "interactive_title": None,
        "interactive_description": None,
        "raw_type": mtype,
        "message_id": message_id,
    }

    if mtype == "text":
        base["kind"] = KIND_TEXT
        base["text"] = ((msg_obj.get("text") or {}).get("body") or "").strip()[:500]
        return base

    if mtype == "button":
        # Quick-reply de plantilla (template) -- contrato distinto al Interactive
        # Message API. Se reconoce aqui solo para completar el enum del
        # contrato normalizado; app.py sigue resolviendolo con su propio
        # _message_text, sin duplicar logica.
        btn = msg_obj.get("button") or {}
        base["kind"] = KIND_LEGACY_TEMPLATE_BUTTON
        base["text"] = (btn.get("text") or btn.get("payload") or "").strip()[:500]
        return base

    if mtype != "interactive":
        return base

    interactive = msg_obj.get("interactive") or {}
    itype = interactive.get("type", "") or ""
    base["raw_type"] = f"interactive:{itype}" if itype else "interactive"

    if itype == "button_reply":
        br = interactive.get("button_reply") or {}
        rid = (br.get("id") or "").strip()
        title = (br.get("title") or "").strip()
        if not rid:
            base["kind"] = KIND_INTERACTIVE_UNKNOWN
            return base
        base["kind"] = KIND_BUTTON_REPLY
        base["interactive_id"] = rid
        base["interactive_title"] = title
        base["text"] = title[:500]
        return base

    if itype == "list_reply":
        lr = interactive.get("list_reply") or {}
        rid = (lr.get("id") or "").strip()
        title = (lr.get("title") or "").strip()
        description = (lr.get("description") or "").strip() or None
        if not rid:
            base["kind"] = KIND_INTERACTIVE_UNKNOWN
            return base
        base["kind"] = KIND_LIST_REPLY
        base["interactive_id"] = rid
        base["interactive_title"] = title
        base["interactive_description"] = description
        base["text"] = title[:500]
        return base

    if itype == "nfm_reply":
        # WhatsApp Flows. Reconocido de forma segura, sin tocar response_json:
        # el procesamiento completo es WA-2+. No se marca como error ni se
        # descarta en silencio -- el caller debe loguear este kind y retornar
        # sin enviar nada al usuario.
        base["kind"] = KIND_NFM_REPLY
        return base

    base["kind"] = KIND_INTERACTIVE_UNKNOWN
    return base


def build_reply_buttons_payload(
    to: str,
    body_text: str,
    buttons: List[Tuple[str, str]],
) -> Dict[str, Any]:
    """Construye el payload de un Interactive Reply Buttons message.

    `buttons` es una lista de (id, title), maximo 3 (limite de Meta). No hace
    ningun request HTTP -- solo construye y valida el dict.
    """
    if not body_text or not body_text.strip():
        raise ValueError("body_text no puede estar vacio")
    if not buttons:
        raise ValueError("buttons no puede estar vacio")
    if len(buttons) > MAX_REPLY_BUTTONS:
        raise ValueError(
            f"maximo {MAX_REPLY_BUTTONS} reply buttons soportados por Meta, "
            f"se recibieron {len(buttons)}"
        )

    seen_ids = set()
    button_objs = []
    for btn_id, title in buttons:
        btn_id = (btn_id or "").strip()
        title = (title or "").strip()
        if not btn_id:
            raise ValueError("button id no puede estar vacio")
        if not title:
            raise ValueError(f"button title no puede estar vacio (id={btn_id!r})")
        if len(btn_id) > MAX_BUTTON_ID_LEN:
            raise ValueError(f"button id excede {MAX_BUTTON_ID_LEN} caracteres: {btn_id!r}")
        if len(title) > MAX_BUTTON_TITLE_LEN:
            raise ValueError(
                f"button title excede {MAX_BUTTON_TITLE_LEN} caracteres: {title!r}"
            )
        if btn_id in seen_ids:
            raise ValueError(f"button id duplicado: {btn_id!r}")
        seen_ids.add(btn_id)
        button_objs.append({"type": "reply", "reply": {"id": btn_id, "title": title}})

    return {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": button_objs},
        },
    }


def build_list_message_payload(
    to: str,
    body_text: str,
    button_text: str,
    sections: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Construye el payload de un Interactive List Message.

    `sections` es una lista de {"title": str opcional, "rows": [{"id","title",
    "description" opcional}, ...]}. No hace ningun request HTTP.
    """
    if not body_text or not body_text.strip():
        raise ValueError("body_text no puede estar vacio")
    if not button_text or not button_text.strip():
        raise ValueError("button_text no puede estar vacio")
    if len(button_text) > MAX_LIST_BUTTON_TEXT_LEN:
        raise ValueError(
            f"button_text excede {MAX_LIST_BUTTON_TEXT_LEN} caracteres: {button_text!r}"
        )
    if not sections:
        raise ValueError("sections no puede estar vacio")

    seen_ids = set()
    total_rows = 0
    section_objs = []
    for section in sections:
        rows = section.get("rows") or []
        if not rows:
            raise ValueError("cada seccion requiere al menos 1 row")
        section_title = (section.get("title") or "").strip()
        if section_title and len(section_title) > MAX_LIST_SECTION_TITLE_LEN:
            raise ValueError(
                f"section title excede {MAX_LIST_SECTION_TITLE_LEN} caracteres: "
                f"{section_title!r}"
            )
        row_objs = []
        for row in rows:
            rid = (row.get("id") or "").strip()
            title = (row.get("title") or "").strip()
            description = (row.get("description") or "").strip()
            if not rid:
                raise ValueError("row id no puede estar vacio")
            if not title:
                raise ValueError(f"row title no puede estar vacio (id={rid!r})")
            if len(rid) > MAX_LIST_ROW_ID_LEN:
                raise ValueError(f"row id excede {MAX_LIST_ROW_ID_LEN} caracteres: {rid!r}")
            if len(title) > MAX_LIST_ROW_TITLE_LEN:
                raise ValueError(
                    f"row title excede {MAX_LIST_ROW_TITLE_LEN} caracteres: {title!r}"
                )
            if description and len(description) > MAX_LIST_ROW_DESCRIPTION_LEN:
                raise ValueError(
                    f"row description excede {MAX_LIST_ROW_DESCRIPTION_LEN} caracteres: "
                    f"{description!r}"
                )
            if rid in seen_ids:
                raise ValueError(f"row id duplicado: {rid!r}")
            seen_ids.add(rid)
            total_rows += 1
            row_obj = {"id": rid, "title": title}
            if description:
                row_obj["description"] = description
            row_objs.append(row_obj)

        section_obj: Dict[str, Any] = {"rows": row_objs}
        if section_title:
            section_obj["title"] = section_title
        section_objs.append(section_obj)

    if total_rows > MAX_LIST_ROWS_TOTAL:
        raise ValueError(
            f"maximo {MAX_LIST_ROWS_TOTAL} rows totales soportados por Meta, "
            f"se recibieron {total_rows}"
        )

    return {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_text, "sections": section_objs},
        },
    }


# ── Puente interactive_id -> servicio canonico existente ──────────────────────
# La identidad de routing es el id, nunca el titulo visible (permite cambiar
# copy sin romper el routing). Mismos codigos que ya consumen detect_svc()/
# route() en app.py -- no se inventan servicios nuevos aqui.
INTERACTIVE_ID_TO_SERVICE = {
    "menu_imss": "imss",
    "menu_auto": "auto",
    "menu_vida": "vida",
    "menu_vrim": "vrim",
    "menu_emp": "emp",
    "menu_fp": "fp",
}


def resolve_service_from_interactive_id(interactive_id: Optional[str]) -> Optional[str]:
    """ID desconocido -> None (fallback seguro: el caller debe tratar el
    evento como si no tuviera id resuelto, nunca como error)."""
    if not interactive_id:
        return None
    return INTERACTIVE_ID_TO_SERVICE.get(interactive_id)
