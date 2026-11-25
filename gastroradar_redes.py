"""Conversación de Vicky Redes para GastroRadar.

No tiene datastore ni cliente HTTP propios: usa lo que app.py ya tiene
(estado y datos por teléfono, bitácora y reporte en Sheets, envío de WhatsApp y
aviso al asesor). Además guarda en la memoria del proceso un respaldo de sus
marcas, que se pierde al reiniciar. Textos fijos, sin IA y sin mensajes por
iniciativa propia.

Información comercial contrastada con tugastroradar.com (inicio, /planes,
/ayuda) el 25-nov-2026. La terminal no tiene texto en el sitio: no se describen
precio, requisitos ni beneficios.
"""
import re
import threading
import time
import unicodedata

SITE = "https://tugastroradar.com"
PLANS_URL = SITE + "/planes"
HELP_URL = SITE + "/ayuda"
CONTACT_EMAIL = "contacto@tugastroradar.com"
REQUEST_TTL = 24 * 60 * 60
PRODUCT = "GastroRadar"
KEY = "gr_asesor"                      # dentro de user_data; no toca otros datos
# Claves en el almacén auxiliar existente.
REF_KEY = "gr_ref:"                    # referencia vigente de la solicitud de un teléfono
STOP_KEY = "gr_baja:"                  # estado del aviso NO CONTACTAR de un teléfono
STOP_WAMID_KEY = "gr_baja_wamid:"      # a qué teléfono se refiere un aviso NO CONTACTAR enviado
REF_TTL = 30 * 24 * 60 * 60            # mucho mayor que el plazo de un reintento a Meta
STOP_TTL = 48 * 60 * 60                # igual que la correlación de avisos de app.py
SENDING_TTL = 2 * 60                   # si el proceso muere enviando, la marca vence sola
WITHDRAWN, SENDING, NOTIFIED, RELEASED = "retirada", "en_curso", "avisado", "liberado"

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
                r"retirar (mi )?ficha|dar de baja|no puedo|mi cuenta|acceso)\b"),
    ("registro", r"\b(registr\w+|dar de alta|inscribir\w*|mi (negocio|restaurante|local|puesto))\b"),
    ("planes", r"\b(plan|planes|conecta|presencia|esencial|mensualidad)\b"),
    ("comida", r"\b(comer|comida|restaurantes?|tacos|sushi|mariscos|pizza|cenar|desayunar|antojo)\b"),
)
PRICE = r"\b(precio|precios|cuesta|cuestan|costo|cuanto|gratis|cobran)\b"
CONTRACT = r"\bcontrat\w+\b"

# ── Contacto: bajas y negativas ───────────────────────────────────────────────
# "baja": retiro de contacto. Siempre se le indica al asesor.
OPT_OUT = (r"^(baja|stop)$|"
           r"\bdar(me)? de baja\b(?! (a )?(mi|la|el|un|una) (negocio|ficha|restaurante|local|puesto))|"
           r"\bdenme de baja\b|"
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
# Pedir contacto sin ambigüedad. Nombrar al asesor o preguntar por él no basta.
_LEAD = r"^((hola|buenas|buenos dias|buenas tardes|buenas noches|si|ok|oye|vicky|por favor) )*"
_WHO = r"( un| una| el| al)? (asesor|asesora|ejecutivo|agente|humano|persona|atencion humana|alguien)\b"
_ASK = r"(yo )?(quiero|quisiera|necesito|ocupo|solicito|pido|deseo|requiero|me gustaria)( por favor)?"
EXPLICIT = _LEAD + (
    r"((un |una )?(asesor|asesora|ejecutivo|humano|persona real|atencion humana)"
    r"( por favor| gracias| (para|sobre) .+)?$|"
    # "persona" o "alguien" solos pueden ser quien busca personal: exigen pedir trato.
    + _ASK + r"( un| una| el| al)? (asesor|asesora|ejecutivo|agente|atencion humana)\b|"
    + _ASK + r" (hablar con|contactar a|que me atienda|que me contacte|que me llame)" + _WHO + "|"
    + _ASK + r" que me (contacten|contacte|llamen|llame)\b|"
    r"que me (contacten|llamen)\b|"
    r"que me (contacte|llame|atienda)" + _WHO + "|"
    r"(pasame|paseme|comunicame|comuniqueme|contactame|ponme|transfiereme) con" + _WHO + "|"
    r"(contactar|hablar) (a|con)" + _WHO + ")")
DOUBT = r"\b(o no|o puedo|o es|si o no)\b"
CONFIRM = ("Si quieres que te contacte una persona, escribe ASESOR. No he enviado ninguna solicitud. "
           "Para ver las opciones escribe MENU.")
CONFIRM_PENDING = ("Tu solicitud ya está enviada al asesor; no he enviado otra. "
                   "Escribe MENU para ver las opciones.")
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
STOP_UNKNOWN = ("Envié al asesor la indicación de no contactarte. Si ya había recibido una solicitud "
                "tuya, no la puedo deshacer.")
STOP_ALREADY = ("La indicación de no contactarte ya se envió al asesor; no la repito. No puedo asegurar "
                "que la haya visto.")
STOP_SENDING = "Ya estoy enviando al asesor la indicación de no contactarte; no la repito."
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


# Respaldo en memoria del proceso. Sirve cuando el almacén auxiliar falla; no persiste.
_lock = threading.Lock()
_revoked_until = {}      # teléfono -> las referencias hasta este número están retiradas
_unconfirmed = set()     # teléfonos con un reintento bloqueado sin poder verificar
_stop_local = {}         # teléfono -> (vence, estado, quedó guardado en el almacén)
_wamid_local = {}        # wamid de un NO CONTACTAR -> teléfono


def reset_memory() -> None:
    """Equivale a reiniciar el proceso (lo usan las pruebas)."""
    for store in (_revoked_until, _unconfirmed, _stop_local, _wamid_local):
        store.clear()


def _number(value) -> int:
    return int(value) if str(value or "").isdigit() else 0


def _request_ref(phone, aux_get, aux_set, now) -> str:
    """Referencia de una solicitud nueva. Reutiliza la vigente para que otra
    solicitud no retirada conserve su reintento."""
    cutoff = _revoked_until.get(str(phone), 0)
    current = _number(aux_get(REF_KEY + str(phone)))
    ref = current if current > cutoff else max(int(now() * 1000), cutoff + 1)
    aux_set(REF_KEY + str(phone), str(ref), REF_TTL)      # si falla, no habrá reintento
    return str(ref)


def _revoke(phone, aux_get, aux_set, now) -> None:
    phone = str(phone)
    _revoked_until[phone] = max(int(now() * 1000), _number(aux_get(REF_KEY + phone)),
                                _revoked_until.get(phone, 0))
    aux_set(REF_KEY + phone, WITHDRAWN, REF_TTL)
    _unconfirmed.discard(phone)


# Avisos que se refieren a una solicitud que la persona puede retirar.
_TIED = re.compile(r"^GastroRadar: (?:piden un asesor|datos de quien pidió asesor)\. "
                   r"Ref (\d+)\. WhatsApp: (\d+)\.")
_STOP_NOTICE = re.compile(r"^GastroRadar: NO CONTACTAR\. WhatsApp: (\d+)\.")


def resend_block(body, aux_get, lookup):
    """Antes de reintentar un aviso. None: se puede reenviar. "no_vigente": consta
    que no. "sin_verificar": no se pudo comprobar; tampoco se reenvía.
    El aviso NO CONTACTAR y los avisos ajenos a esta conversación no cambian."""
    match = _TIED.match(body or "")
    if not match:
        return None
    ref, phone = match.groups()
    if int(ref) <= _revoked_until.get(phone, 0):
        return "no_vigente"
    current = aux_get(REF_KEY + phone)
    if current is None:                       # lectura fallida o clave ausente
        reason = "sin_verificar"
    elif current != ref:
        return "no_vigente"
    else:
        # Segunda fuente: cubre un retiro que no se pudo guardar en el almacén auxiliar.
        row = lookup(phone)
        if row is None:
            reason = "sin_verificar"
        elif row.get("Estado", "").startswith("No contactar"):
            return "no_vigente"
        else:
            _unconfirmed.discard(phone)
            return None
    _unconfirmed.add(phone)                   # un ASESOR nuevo podrá volver a avisar
    return reason


def _stop_state(phone, aux_get, now):
    """SENDING, NOTIFIED o None si no hay un aviso NO CONTACTAR vigente."""
    expires, local, saved = _stop_local.get(str(phone), (0, None, True))
    if expires <= now():
        local, saved = None, True
    if not saved:                             # lo del almacén es anterior a esta marca
        state = local
    else:
        stored = aux_get(STOP_KEY + str(phone))
        # "liberado" guardado por otro proceso manda; si la lectura falla queda la memoria.
        state = None if stored == RELEASED else local or stored
    return state if state in (SENDING, NOTIFIED) else None


def _stop_set(phone, state, ttl, aux_set, now) -> None:
    saved = aux_set(STOP_KEY + str(phone), state, ttl)
    _stop_local[str(phone)] = (now() + ttl, state, saved is not False)


def note_advisor_send(wamid, body, aux_set) -> None:
    """Relaciona un NO CONTACTAR enviado con su teléfono. El cuerpo para reintentos
    vence antes que la correlación; esta relación permite liberar la marca."""
    match = _STOP_NOTICE.match(body or "")
    if match:
        _wamid_local[wamid] = match.group(1)
        aux_set(STOP_WAMID_KEY + wamid, match.group(1), STOP_TTL)


def release_stop(wamid, aux_get, aux_set, body=None, now=time.time) -> None:
    """Consta que ese NO CONTACTAR no llegó ni se reintentará: la siguiente
    negativa debe volver a avisar. Para otros avisos no hace nada."""
    match = _STOP_NOTICE.match(body or "")
    phone = match.group(1) if match else (_wamid_local.get(wamid) or aux_get(STOP_WAMID_KEY + wamid))
    if phone:
        _stop_set(phone, RELEASED, STOP_TTL, aux_set, now)


def _pending(row) -> bool:
    estado = (row or {}).get("Estado", "")
    return estado.startswith("Solicita asesor") or estado == "No contactar - aviso fallido"


def handle(phone, mtype, text, *, states, store, send, notify, report, lookup, aux_get, aux_set,
           now=time.time):
    """Atiende un mensaje. `states` y `store` son user_state y user_data de app.py.

    `lookup(phone)` lee la fila de GastroRadar del reporte: dict, {} si no hay,
    o None si no se pudo leer.
    """
    if mtype in ("reaction", "system"):
        return
    previous = states.get(phone, "") or ""
    mine = previous.startswith("gr_")
    topic_now = previous[3:] if mine and previous[3:] in ANSWERS else None
    data = dict(store.get(phone) or {})
    stored = data.get(KEY)
    # La antigüedad solo permite volver a pedir asesor; no da por terminada la solicitud.
    request = stored if stored and now() - stored.get("ts", 0) <= REQUEST_TTL else None
    t = normalize(text)

    def finish(reply, state="gr_menu"):
        states[phone] = state
        send(phone, reply)

    def product(topic):
        return f"{PRODUCT}: {LABELS.get(topic, LABELS['general'])}"

    def record(topic, estado, nombre="", producto=""):
        report(phone, producto=producto or product(topic), estado=estado, nombre=nombre)

    def forget_request():
        data.pop(KEY, None)
        store[phone] = data

    if not t:
        return finish(ONLY_TEXT, previous if mine else "gr_menu")

    # 1) Retiro de contacto: antes que cualquier otra interpretación.
    opt_out = bool(re.search(OPT_OUT, t))
    cancel = bool(re.search(CANCEL, t))
    if opt_out or re.search(REFUSAL, t) or cancel:
        # Un NO CONTACTAR en curso no se duplica. Uno ya aceptado tampoco, salvo que la sesión
        # tenga una solicitud: es posterior a ese aviso y su retiro sí se comunica.
        repeated = _stop_state(phone, aux_get, now)
        if repeated == SENDING:
            return finish(STOP_SENDING)
        if repeated == NOTIFIED and not stored:
            row = lookup(phone)
            if row is not None:               # completa el reporte si antes no se pudo escribir
                record("general", "No contactar", producto=row.get("Producto", ""))
            return finish(STOP_ALREADY)
        # Sin sesión (vencida o borrada), el reporte dice si quedó una solicitud.
        row = None if stored else lookup(phone)
        unreadable = not stored and row is None
        if not (opt_out or stored or unreadable or _pending(row)):
            return finish(NOTHING_PENDING)
        # Primero se invalidan los reintentos de los avisos anteriores.
        _revoke(phone, aux_get, aux_set, now)
        topic = (stored or {}).get("tema", topic_now or "general")
        with _lock:                           # comprobar y marcar sin que otro hilo se cuele
            current = _stop_state(phone, aux_get, now)
            if current == SENDING or (current and not stored):
                return finish(STOP_SENDING)
            _stop_set(phone, SENDING, SENDING_TTL, aux_set, now)
        ok = False
        try:
            ok = notify(f"GastroRadar: NO CONTACTAR. WhatsApp: {phone}. La persona retiró su solicitud.")
        finally:
            # Si el aviso se liberó mientras se enviaba, no se vuelve a marcar.
            if not ok:
                _stop_set(phone, RELEASED, STOP_TTL, aux_set, now)
            elif _stop_state(phone, aux_get, now):
                _stop_set(phone, NOTIFIED, STOP_TTL, aux_set, now)
        record(topic, "No contactar" if ok else "No contactar - aviso fallido",
               producto=(row or {}).get("Producto", ""))
        forget_request()
        if not ok:
            return finish(STOP_FAILED)
        if stored or (row or {}).get("Estado") == "Solicita asesor":
            return finish(STOP_SENT)
        return finish(STOP_UNKNOWN if unreadable else STOP_RECORDED)

    # 2) Atención humana.
    if (re.search(EXPLICIT, t) and "?" not in text and not re.search(NEGATION, t)
            and not re.search(DOUBT, t)):
        asked = topic_of(t) or topic_now
        if request and asked in (None, request.get("tema")) and str(phone) not in _unconfirmed:
            return finish(ALREADY)
        topic = asked or (request or {}).get("tema") or "general"
        # Una solicitud nueva permite un nuevo retiro y su aviso.
        _stop_set(phone, RELEASED, STOP_TTL, aux_set, now)
        ref = _request_ref(phone, aux_get, aux_set, now)
        ok = notify(f"GastroRadar: piden un asesor. Ref {ref}. WhatsApp: {phone}. "
                    f"Tema: {LABELS[topic]}. Mensaje: {text[:200]}")
        record(topic, "Solicita asesor" if ok else "Solicita asesor - aviso fallido")
        if not ok:
            return finish(FAILED)
        _unconfirmed.discard(str(phone))
        data[KEY] = {"ts": now(), "tema": topic, "ref": ref}
        store[phone] = data
        return finish(SENT + ASK_DETAILS, "gr_datos")

    if re.search(PERSON, t):
        return finish(CONFIRM_PENDING if request else CONFIRM, previous if mine else "gr_menu")

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
            ok = notify(f"GastroRadar: datos de quien pidió asesor. Ref {request.get('ref', 0)}. "
                        f"WhatsApp: {phone}. {clean}")
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
