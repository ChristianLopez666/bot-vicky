"""Conversación de Vicky Redes para GastroRadar.

No tiene almacenamiento ni cliente HTTP propios: usa lo que app.py ya tiene
(estado y datos por teléfono, bitácora y reporte en Sheets, envío de WhatsApp y
aviso al asesor). Textos fijos, sin IA y sin mensajes por iniciativa propia.

Información comercial contrastada con tugastroradar.com (inicio, /planes,
/ayuda) el 25-nov-2026. La terminal no tiene texto en el sitio: no se describen
precio, requisitos ni beneficios.
"""
import re
import time
import unicodedata

SITE = "https://tugastroradar.com"
PLANS_URL = SITE + "/planes"
HELP_URL = SITE + "/ayuda"
CONTACT_EMAIL = "contacto@tugastroradar.com"
REQUEST_TTL = 24 * 60 * 60
PRODUCT = "GastroRadar"
KEY = "gr_asesor"                      # dentro de user_data; no toca otros datos

MENU = (
    "1. Encontrar comida\n2. Registrar mi negocio\n3. Planes\n4. Publicidad y banners\n"
    "5. Terminal punto de venta\n6. Soporte o atención humana\n"
    "Responde con el número o escribe tu consulta."
)
GREETING = "Hola, soy Vicky, asistente virtual de GastroRadar. ¿En qué te ayudo?\n\n" + MENU
CHANGED = "Ahora atiendo GastroRadar.\n\n" + GREETING
OFFER = "\n\nEscribe ASESOR si quieres que te contacte una persona, o MENU para volver."

LABELS = {"comida": "Encontrar comida", "registro": "Registro de negocio", "planes": "Planes",
          "banners": "Publicidad y banners", "terminal": "Terminal punto de venta",
          "soporte": "Soporte", "general": "Atención humana"}

ANSWERS = {
    "comida": f"En {SITE} puedes buscar por nombre o tipo de comida, elegir estado y municipio o usar "
              "«Cerca de mí», y filtrar por forma de pago, servicio o «Abierto ahora».\n\n"
              "Escribe MENU para volver.",
    "registro": f"Registrarte es gratis. Busca tu negocio en {PLANS_URL}, crea tu cuenta del portal y "
                "pide administrarlo. Cuando se verifica por teléfono que lo representas, puedes cambiar "
                "contacto, horario, pagos, servicios, descripción, enlaces y fotos (las fotos se publican "
                "después de revisarlas)." + OFFER,
    "planes": "Planes (precios en pesos, IVA incluido):\n"
              "• Esencial, gratis: nombre, dirección, contacto y búsqueda por cercanía.\n"
              "• Conecta, $39.90 al mes: agrega «Cómo llegar», botones de llamada y WhatsApp e imagen de "
              "perfil.\n"
              "• Presencia, $89.90 al mes: agrega perfil personalizado, portada, hasta 8 imágenes y "
              "vacantes.\nEl sitio anuncia primer mes sin costo en Conecta y Presencia. La contratación "
              f"de planes aún no está disponible.\nDetalles: {PLANS_URL}" + OFFER,
    "banners": "Un banner cuesta $499 al mes, IVA incluido, por posición (superior o lateral) y ubicación "
               "contratada. Es aparte de los planes. Cada espacio rota hasta 4 anunciantes. Apartar tu "
               "lugar es gratis y no publica nada; el pago en línea es con Mercado Pago y se publica "
               f"después de revisar tu arte y tu pago.\nDetalles: {PLANS_URL}" + OFFER,
    "terminal": "Sobre la terminal punto de venta de Inbursa, un asesor te informa requisitos y "
                "condiciones." + OFFER,
    "soporte": f"Para reportar un problema o corregir una ficha entra a {HELP_URL} o escribe a "
               f"{CONTACT_EMAIL}. No envíes contraseñas ni datos de tarjeta." + OFFER,
}
OPTIONS = dict(zip("123456", ("comida", "registro", "planes", "banners", "terminal", "soporte")))

# El orden importa: lo específico antes que lo general.
TOPICS = (
    ("terminal", r"\b(terminal|tpv|punto de venta|inbursa|cobrar con tarjeta)\b"),
    ("banners", r"\b(banner|banners|publicidad|anunci\w+|promocionar)\b"),
    ("soporte", r"\b(soporte|ayuda|problema|error|falla|queja|reporte|no funciona|corregir|"
                r"retirar (mi )?ficha|no puedo|mi cuenta|acceso)\b"),
    ("registro", r"\b(registr\w+|dar de alta|inscribir\w*|mi (negocio|restaurante|local|puesto))\b"),
    ("planes", r"\b(plan|planes|conecta|presencia|esencial|mensualidad)\b"),
    ("comida", r"\b(comer|comida|restaurantes?|tacos|sushi|mariscos|pizza|cenar|desayunar|antojo)\b"),
)
PRICE = r"\b(precio|precios|cuesta|cuestan|costo|cuanto|gratis|cobran)\b"
CONTRACT = r"\bcontrat\w+\b"

# ── Contacto: bajas y negativas ───────────────────────────────────────────────
# "baja": retiro de contacto. Siempre se le indica al asesor.
OPT_OUT = (r"^(baja|stop)$|\bdar(me)? de baja\b|\bdenme de baja\b|"
           r"\bno (me )?(contacten|contacte|llamen|llame|escriban|escriba|busquen|busque|marquen|marque)\b|"
           r"\bno (quiero|necesito|deseo) que (nadie )?me (contacten|contacte|llamen|llame|escriban|"
           r"escriba|busquen|busque)\b|\bno quiero (recibir )?(mas )?mensajes\b|"
           r"\b(dejen|deja) de (escribirme|llamarme|contactarme|mandarme mensajes)\b")
# "rechazo": no quiere asesor.
REFUSAL = (r"\b(no|sin|ni) (quiero|necesito|ocupo|requiero|deseo)( hablar con)?( un| una| el| al| ningun)? "
           r"(asesor|asesora|humano|persona|agente|ejecutivo|contacto)\b|"
           r"\bno hace falta( un| el)? asesor\b|^sin (asesor|contacto)( gracias)?$")
# "cancelar": retira la solicitud en curso, si la hay.
CANCEL = (r"^((ya )?(quiero )?(cancelar|cancela|cancelen|cancelalo)( (mi |la )?(solicitud|cita|llamada))?|"
          r"olvidalo|olvidenlo|siempre no|(ya )?no me interesa|(mejor )?ya no( gracias)?|"
          r"ya no (lo )?quiero( nada)?|ya no es necesario|dejalo asi|"
          r"(cancela|cancelar|cancelen) (el )?(asesor|contacto))$")
PERSON = (r"\b(asesor|asesora|humano|persona|ejecutivo|que me (llamen|contacten)|"
          r"hablar con alguien|que me atienda alguien|atencion humana)\b")
NEGATION = r"\b(no|ni|sin)\b"
MENU_WORDS = r"^(hola|buenos dias|buenas tardes|buenas noches|buenas|menu|inicio|info|0)( vicky)?$"
THANKS = r"^(muchas )?(gracias|ok|va|perfecto|excelente|de acuerdo|adios)( gracias)?$"
YES_NO = r"^(si|claro|sii|dale)$"
SKIP = r"^(omitir|no|no gracias|ninguno|despues)$"
# En la pregunta de datos: esto es una consulta, no un nombre.
ASKING = (r"^(que|cuanto|cuanta|como|donde|cuando|cual|quiero|quisiera|necesito|busco|tienen|puedo|hay|ver|"
          r"me interesa|informacion de|info de)( |$)|"
          r"^(comida|registro|planes?|banners?|publicidad|terminal|soporte|ayuda|precios?|costo)$")

ASK_DETAILS = ("\n\nSi quieres, escribe tu nombre y el de tu negocio para pasárselos. "
               "También puedes escribir OMITIR o MENU.")
SENT = ("Envié tu solicitud a un asesor. No puedo prometer un horario de respuesta. "
        "Si ya no la quieres, escribe CANCELAR.")
ALREADY = "Tu solicitud ya fue enviada al asesor; no la repito. Escribe MENU para ver las opciones."
FAILED = f"No pude enviar el aviso al asesor. Por favor escribe a {CONTACT_EMAIL}."
DETAILS_OK = "Gracias, envié tus datos al asesor. Escribe MENU para ver las opciones."
DETAILS_FAILED = f"No pude enviar tus datos al asesor. Puedes escribirlos a {CONTACT_EMAIL}."
SKIPPED = "De acuerdo. Tu solicitud ya fue enviada. Escribe MENU para ver las opciones."
STOP_SENT = ("El aviso anterior ya se había enviado y no puedo deshacerlo. Le envié al asesor la "
             "indicación de no contactarte, pero no puedo asegurar que la vea a tiempo.")
STOP_RECORDED = "Envié al asesor la indicación de no contactarte."
STOP_FAILED = f"No pude avisar al asesor. Para que no te contacten escribe a {CONTACT_EMAIL}."
NOTHING_PENDING = "De acuerdo, no pediré que te contacten. Escribe MENU para ver las opciones."
THANKS_REPLY = "Con gusto. Escribe MENU si necesitas algo más."
YES_REPLY = "Para pedir un asesor escribe ASESOR. Para ver las opciones escribe MENU."
UNKNOWN = ("No tengo una respuesta confirmada para eso. Escribe ASESOR para que te atienda una persona, "
           "o MENU para ver las opciones.")
ONLY_TEXT = "Por ahora solo puedo leer mensajes de texto. Escribe MENU para ver las opciones."


def normalize(text: str) -> str:
    # "No, quiero un asesor" es una petición: el "no" seguido de puntuación contesta otra cosa.
    text = text or ""
    rest = re.sub(r"^\s*no\s*[,.;!]+", "", text, flags=re.IGNORECASE)
    text = rest if re.search(r"\w", rest) else text
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def topic_of(t: str):
    return next((name for name, pattern in TOPICS if re.search(pattern, t)), None)


def handle(phone, mtype, text, *, states, store, send, notify, report, now=time.time):
    """Atiende un mensaje. `states` y `store` son user_state y user_data de app.py."""
    if mtype in ("reaction", "system"):
        return
    previous = states.get(phone, "") or ""
    mine = previous.startswith("gr_")
    topic_now = previous[3:] if mine and previous[3:] in ANSWERS else None
    data = dict(store.get(phone) or {})
    request = data.get(KEY)
    if request and now() - request.get("ts", 0) > REQUEST_TTL:
        request = None
    t = normalize(text)

    def finish(reply, state="gr_menu"):
        states[phone] = state
        send(phone, reply)

    def record(topic, estado, nombre=""):
        report(phone, producto=f"{PRODUCT}: {LABELS.get(topic, LABELS['general'])}",
               estado=estado, nombre=nombre)

    def forget_request():
        data.pop(KEY, None)
        store[phone] = data

    if not t:
        return finish(ONLY_TEXT, previous if mine else "gr_menu")

    # 1) Retiro de contacto: antes que cualquier otra interpretación.
    opt_out = bool(re.search(OPT_OUT, t))
    cancel = bool(re.search(CANCEL, t))
    if opt_out or re.search(REFUSAL, t) or cancel:
        if opt_out or request:
            topic = (request or {}).get("tema", topic_now or "general")
            ok = notify(f"GastroRadar: NO CONTACTAR. WhatsApp: {phone}. La persona retiró su solicitud.")
            record(topic, "No contactar" if ok else "No contactar - aviso fallido")
            forget_request()
            if not ok:
                return finish(STOP_FAILED)
            return finish(STOP_SENT if request else STOP_RECORDED)
        return finish(NOTHING_PENDING)

    # 2) Atención humana.
    if re.search(PERSON, t) and not re.search(NEGATION, t):
        asked = topic_of(t) or topic_now
        if request and asked in (None, request.get("tema")):
            return finish(ALREADY)
        topic = asked or "general"
        ok = notify(f"GastroRadar: piden un asesor. Tema: {LABELS[topic]}. WhatsApp: {phone}. "
                    f"Mensaje: {text[:200]}")
        record(topic, "Solicita asesor" if ok else "Solicita asesor - aviso fallido")
        if not ok:
            return finish(FAILED)
        data[KEY] = {"ts": now(), "tema": topic}
        store[phone] = data
        return finish(SENT + ASK_DETAILS, "gr_datos")

    if re.search(PERSON, t):
        return finish(YES_REPLY, previous if mine else "gr_menu")

    if re.search(MENU_WORDS, t):
        return finish(GREETING)

    # 3) Sesión que venía de la conversación anterior: se pasa al menú nuevo sin
    #    interpretar su respuesta ("1", "sí", un monto) y sin borrar sus datos.
    if previous and not mine:
        return finish(CHANGED)

    # 4) Opciones numéricas: solo si ya se mostró este menú.
    if t in OPTIONS:
        return finish(ANSWERS[OPTIONS[t]], "gr_" + OPTIONS[t]) if mine else finish(GREETING)

    if previous == "gr_datos":
        if re.search(SKIP, t):
            return finish(SKIPPED)
        looks_like_data = not (re.search(ASKING, t) or re.search(THANKS, t) or re.search(YES_NO, t)
                               or "?" in text)
        if looks_like_data and request:
            clean = re.sub(r"\s+", " ", text).strip()[:150]
            ok = notify(f"GastroRadar: datos de quien pidió asesor. WhatsApp: {phone}. {clean}")
            record(request.get("tema", "general"), "Solicita asesor", nombre=clean)
            return finish(DETAILS_OK if ok else DETAILS_FAILED)

    if re.search(THANKS, t):
        return finish(THANKS_REPLY, previous if topic_now else "gr_menu")
    if re.search(YES_NO, t):
        return finish(YES_REPLY, previous if topic_now else "gr_menu")

    topic = topic_of(t)
    if topic:
        return finish(ANSWERS[topic], "gr_" + topic)
    if re.search(CONTRACT, t):
        topic = topic_now if topic_now in ("banners", "terminal", "planes") else "planes"
        return finish(ANSWERS[topic], "gr_" + topic)

    # 5) Pregunta de seguimiento sobre el tema abierto.
    if re.search(PRICE, t):
        if topic_now in ("planes", "banners", "registro"):
            return finish(ANSWERS[topic_now], previous)
        if topic_now == "terminal":
            return finish("No tengo precio confirmado de la terminal." + OFFER, previous)
        return finish(ANSWERS["planes"], "gr_planes")

    return finish(UNKNOWN, previous if topic_now else "gr_menu") if mine else finish(GREETING)
