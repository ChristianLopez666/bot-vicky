"""Política mínima de persistencia para la bitácora de Google Sheets.

No decide ni modifica ningún flujo comercial. Solo determina qué eventos ya
procesados por Vicky merecen persistirse en la bitácora cruda.
"""


def should_persist_log(tipo, origen, resultado="", error="") -> bool:
    """Devuelve False únicamente para respuestas automáticas exitosas del bot.

    Esos mensajes ya existen en WhatsApp y no aportan información comercial
    adicional en Sheets. Se conservan explícitamente mensajes entrantes,
    eventos del asesor, respaldos de leads y cualquier error de envío.
    """
    tipo_norm = str(tipo or "").strip().lower()
    origen_norm = str(origen or "").strip().lower()
    resultado_norm = str(resultado or "").strip().lower()
    error_norm = str(error or "").strip()

    successful_bot_reply = (
        tipo_norm == "saliente"
        and origen_norm == "bot"
        and resultado_norm == "ok"
        and not error_norm
    )
    return not successful_bot_reply
