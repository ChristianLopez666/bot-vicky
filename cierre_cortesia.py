"""Cierre de cortesia post-propuesta (Vicky Redes).

Modulo puro: textos y clasificadores del tramo final de la conversacion,
despues de que el prospecto ya recibio su propuesta. Sin I/O, sin estado
global -- el wiring (envio, estado, temporizador) vive en app.py.

Secuencia que implementa:

  1. El prospecto termina el cuestionario y recibe la propuesta.
     Vicky agradece y avisa que Christian lo contactara, SIN esperar otro
     mensaje del cliente (ACUSE_PROPUESTA).
  2. Si el cliente agradece -> CORTESIA_FINAL (sin genero, con invitacion
     al menu y mencion del seguro de auto con tarifa preferencial).
  3. Si el cliente responde que no -> DESPEDIDA_NEGATIVA y se cierra.
  4. Si no responde en una hora -> NUDGE, una sola vez.
"""

# ── Textos ────────────────────────────────────────────────────────────────────
# Nota de registro: los mensajes de cortesia usan "usted" y evitan cualquier
# marca de genero ("atenderle", nunca "bienvenido/a"), tal como se pidio.

ACUSE_PROPUESTA = (
    "🙏 *Gracias por tu tiempo.*\n"
    "Christian López ya recibió tu información y se pondrá en contacto contigo "
    "personalmente para revisar tu caso.\n\n"
    "Si prefieres un horario en particular, respóndeme *1*, *2* o *3*."
)

CORTESIA_FINAL = (
    "Es un gusto atenderle 😊\n\n"
    "Si requiere algún otro servicio, escriba *menú*. Por ejemplo, el *seguro para "
    "su auto con tarifa preferencial por ser pensionado*."
)

DESPEDIDA_NEGATIVA = (
    "Le agradezco su tiempo y su atención 🙏\n"
    "Quedo a sus órdenes cuando lo necesite."
)

NUDGE = "Quedo atenta para cualquier otra consulta, saludos 😊"


# ── Clasificador de respuesta negativa ────────────────────────────────────────
# Mismo patron que _is_pure_courtesy_message() en app.py: el mensaje solo
# cuenta como negativa de cierre si, quitando negacion, cortesia y relleno, no
# queda nada sustantivo. Asi "no gracias" cierra, pero "no entiendo" o "no, me
# interesa el seguro de auto" NO se tragan como despedida.
_NEG_KW = {"no", "nel", "nop", "nope", "negativo", "ninguno", "ninguna",
           "nada", "tampoco", "nunca", "jamas"}
# Declinaciones corteses que no llevan una palabra de negacion explicita.
_NEG_PHRASES = ("asi esta bien", "esta bien asi", "asi lo dejamos", "queda asi",
                "asi le dejamos")
_NEG_FILLER = {"gracias", "muchas", "muy", "amable", "ok", "okay", "por",
               "ahora", "el", "momento", "de", "todo", "bien", "ya", "es",
               "eso", "seria", "sera", "todos", "modos", "igual", "mas",
               "esta", "asi", "vicky", "solo", "era", "esto", "por", "hoy"}


def es_respuesta_negativa(n_msg: str) -> bool:
    """True si el mensaje normalizado (sin acentos, minusculas, sin
    puntuacion -- lo que devuelve app.norm) es una negativa de cierre pura."""
    n_msg = (n_msg or "").strip()
    if not n_msg:
        return False
    working = n_msg
    tiene_frase = False
    for frase in _NEG_PHRASES:
        if frase in working:
            tiene_frase = True
            working = working.replace(frase, " ")
    toks = set(working.split())
    if not (tiene_frase or (toks & _NEG_KW)):
        return False
    return not (toks - _NEG_KW - _NEG_FILLER)
