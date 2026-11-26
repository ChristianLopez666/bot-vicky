"""Conversación de Vicky Redes para GastroRadar.

No tiene datastore ni cliente HTTP propios: usa lo que app.py ya tiene
(estado y datos por teléfono, bitácora y reporte en Sheets, envío de WhatsApp y
aviso al asesor). Cada respuesta es una "vista": texto, botones o lista reales de
WhatsApp y un texto alternativo por si el envío interactivo no es aceptado.
Además guarda en la memoria del proceso un respaldo de sus
marcas, que se pierde al reiniciar. Textos fijos, sin IA y sin mensajes por
iniciativa propia.

Información comercial contrastada con tugastroradar.com (inicio, /planes,
/ayuda) el 25-nov-2026. El beneficio de un año sin costo con TPV Inbursa lo definió
el titular el 26-nov-2026; el sitio no publica sus condiciones ni el precio de la
terminal, así que no se describen.
"""
import os
import re
import threading
import time
import unicodedata
import uuid

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
MSG_KEY = "gr_msg:"                    # estado de un mensaje entrante por su identificador
MSG_DONE_TTL = 48 * 60 * 60
MSG_PROCESSING_TTL = 10 * 60
ECHO_KEY = "gr_eco:"                   # última entrada atendida de un teléfono
ECHO_SECONDS = 8                       # el mismo toque o texto seguido no se contesta dos veces

B_ADVISOR = ("gr_asesor", "Hablar con asesor")
B_HOME = ("gr_inicio", "Volver al menú")
B_CANCEL = ("gr_cancelar", "Cancelar solicitud")
B_DETAILS = ("gr_banners_detalle", "Ver detalles")
B_CHRISTIAN = ("gr_asesor", "Hablar con Christian")
B_REGISTER = ("gr_registro", "Registrar mi negocio")
NAV = (B_ADVISOR, B_HOME)
NAV_REGISTER = (B_REGISTER, B_ADVISOR, B_HOME)
BACK = (B_HOME,)
AFTER_REQUEST = (B_HOME, B_CANCEL)
WELCOME_BUTTONS = (("gr_comida", "Busco comida"), ("gr_negocio", "Tengo un negocio"),
                   ("gr_ayuda", "Necesito ayuda"))
BUSINESS_LIST = ("Ver opciones", (
    ("gr_registro", "Registrar mi negocio"),
    ("gr_planes", "Planes"),
    ("gr_banners", "Publicidad y banners"),
    # La fila admite 24 caracteres: el nombre completo y su condición van en la descripción.
    ("gr_beneficio", "Un año sin costo", "Un año de publicidad sin costo al contratar TPV Inbursa"),
    ("gr_inicio", "Volver al menú")))
ALT_NAV = "Escribe ASESOR para hablar con una persona o MENU para volver."
ALT_BACK = "Escribe MENU para volver."
ALT_REQUEST = "Escribe MENU para volver o CANCELAR si ya no quieres la solicitud."
ALT_WELCOME = ("Responde con un número:\n1. Encontrar comida\n2. Registrar mi negocio\n3. Planes\n"
               "4. Publicidad y banners\n5. Un año de publicidad sin costo (al contratar TPV Inbursa)\n"
               "6. Soporte o atención humana")
ALT_BUSINESS = ("Responde: 2 registro, 3 planes, 4 publicidad y banners, 5 un año de publicidad sin costo "
                "(al contratar TPV Inbursa). MENU para volver.")

WELCOME = ("¡Hola! 👋 Soy Vicky de GastroRadar. ¿Buscas dónde comer, quieres dar a conocer tu negocio "
           "o necesitas ayuda?")
CHANGED = "Ahora atiendo GastroRadar.\n\n" + WELCOME
BUSINESS = "🏪 *Tengo un negocio*\n\n¿Qué quieres hacer?"

LABELS = {"comida": "Encontrar comida", "registro": "Registro de negocio", "planes": "Planes",
          "banners": "Publicidad y banners", "beneficio": "Un año sin costo con TPV Inbursa",
          "soporte": "Soporte", "general": "Atención humana"}

ANSWERS = {
    "comida": "🍽️ *Encuentra dónde comer*\n\n"
              "• Busca por nombre o tipo de comida.\n"
              "• Elige estado y municipio, o usa «Cerca de mí».\n"
              "• Filtra por forma de pago, servicio o «Abierto ahora».\n\n"
              f"👉 *Siguiente paso:* entra a {SITE} y busca.\n\n"
              "¿Tienes un negocio de comida? Regístralo gratis.",
    "registro": "🏪 *Registra y administra tu negocio*\n\n"
                "*El registro es gratis.* Estos son los pasos:\n\n"
                f"*1. Busca tu negocio*\nEntra a {PLANS_URL} y búscalo por nombre. Puedes indicar el "
                "municipio para encontrarlo más fácilmente.\n\n"
                "*2. Crea tu cuenta*\nCrea tu cuenta en el portal de negocios. Si ya tienes una, inicia "
                "sesión.\n\n"
                "*3. Solicita administrarlo*\nSelecciona tu negocio y solicita acceso para administrar su "
                "información.\n\n"
                "*4. Verifica que lo representas*\nAntes de darte acceso, verificamos por teléfono que "
                "representas al negocio.\n\n"
                "*¿Qué podrás hacer después?*\nCuando se autorice tu acceso, podrás actualizar datos de "
                "contacto, horarios, formas de pago, servicios y fotos. Las fotos se publican después de "
                "revisarlas.\n\n"
                "👉 *Siguiente paso:* busca tu negocio en el enlace del paso 1. Si no lo encuentras o "
                "necesitas ayuda, toca *Hablar con asesor*.",
    "planes": "📋 *Planes* (pesos, IVA incluido)\n\n"
              "*Esencial: gratis*\n• Nombre, dirección y contacto\n• Búsqueda por cercanía\n\n"
              "*Conecta: $39.90 al mes*\n• Agrega «Cómo llegar»\n• Botones de llamada y WhatsApp\n"
              "• Imagen de perfil\n\n"
              "*Presencia: $89.90 al mes*\n• Agrega perfil personalizado y portada\n• Hasta 8 imágenes\n"
              "• Vacantes\n\n"
              "El sitio anuncia primer mes sin costo en Conecta y Presencia. La contratación de planes "
              f"aún no está disponible.\n\n🔗 {PLANS_URL}\n\n"
              "👉 *Siguiente paso:* registra tu negocio gratis o resuelve tus dudas con un asesor.",
    "banners": "📣 *Anuncia tu negocio en GastroRadar*\n\n"
               "$499 MXN al mes, IVA incluido, por posición y ubicación.\n"
               "El espacio se comparte con otros anunciantes.\n\n"
               "👉 *Siguiente paso:* revisa los detalles o habla con un asesor.",
    "beneficio": "🎁 *Un año de publicidad sin costo*\n\n"
                 "El beneficio consiste en *12 meses del Plan Presencia de GastroRadar sin costo* mediante la "
                 "contratación de una terminal punto de venta (TPV) Inbursa, *sujeto a las condiciones "
                 "aplicables*.\n\n"
                 "La publicidad la presta GastroRadar, no Inbursa. Los banners son aparte.\n\n"
                 f"🔗 Qué es el Plan Presencia: {PLANS_URL}\n\n"
                 "👉 *Siguiente paso:* toca *Hablar con Christian* para conocer las condiciones y "
                 "continuar.",
    "soporte": "🛟 *Ayuda y contacto*\n\nPara reportar un problema o corregir una ficha:\n"
               f"🔗 {HELP_URL}\n✉️ {CONTACT_EMAIL}\n\n"
               "No envíes contraseñas ni datos de tarjeta.\n\n"
               "👉 *Siguiente paso:* si prefieres que te atienda una persona, toca *Hablar con asesor*.",
}
BANNER_DETAILS = (
    "📣 *Condiciones del banner*\n\n"
    "• $499 MXN al mes, IVA incluido.\n"
    "• Por posición (superior o lateral) y ubicación contratada.\n"
    "• Cada espacio rota hasta 4 anunciantes.\n"
    "• Es aparte de los planes.\n"
    "• Apartar tu lugar es gratis y no publica nada.\n"
    "• El pago en línea es con Mercado Pago.\n"
    "• Se publica después de revisar tu arte y tu pago.\n\n"
    f"🔗 {PLANS_URL}\n\n"
    "👉 *Siguiente paso:* toca *Hablar con asesor* para continuar.")
BUTTONS_BY_TOPIC = {"comida": NAV_REGISTER, "registro": NAV, "planes": NAV_REGISTER,
                    "banners": (B_DETAILS, B_ADVISOR, B_HOME), "beneficio": (B_CHRISTIAN, B_HOME),
                    "soporte": NAV}
OPTIONS = dict(zip("123456", ("comida", "registro", "planes", "banners", "beneficio", "soporte")))

# El orden importa: lo específico antes que lo general.
TOPICS = (
    # Va primero: «publicidad sin costo» también contiene «publicidad».
    ("beneficio", r"\b(terminal|tpv|punto de venta|inbursa|cobrar con tarjeta|beneficio|"
                  r"(un|1) ano( de publicidad)? (sin costo|gratis)|publicidad (sin costo|gratis)|"
                  r"12 meses (sin costo|gratis))\b"),
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
_WHO = r"( un| una| el| al)? (asesor|asesora|ejecutivo|agente|humano|persona|atencion humana|alguien|christian)\b"
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
CONFIRM = ("Si quieres que te contacte una persona, toca *Hablar con asesor*. "
           "No he enviado ninguna solicitud.")
CONFIRM_PENDING = "Tu solicitud ya está enviada al asesor; no he enviado otra."
MENU_WORDS = (r"^(hola|buenos dias|buenas tardes|buenas noches|buenas|menu|inicio|info|0|volver|regresar|"
              r"volver al menu|menu principal)( vicky)?$")
BUSINESS_WORDS = r"^(tengo un negocio|mi negocio|negocios|para negocios)$"
THANKS = r"^(muchas )?(gracias|ok|va|perfecto|excelente|de acuerdo|adios)( gracias)?$"
YES_NO = r"^(si|claro|sii|dale)$"
SKIP = r"^(omitir|no|no gracias|ninguno|despues)$"
# En la pregunta de datos: esto es una consulta, no un nombre.
ASKING = (r"^(que|cuanto|cuanta|como|donde|cuando|cual|quiero|quisiera|necesito|busco|tienen|puedo|hay|ver|"
          r"me interesa|informacion de|info de)( |$)|"
          r"^(comida|registro|planes?|banners?|publicidad|terminal|tpv|beneficio|soporte|ayuda|precios?|costo)$")

ASK_DETAILS = "\n\nSi quieres, compárteme tu nombre y el de tu negocio. Es opcional."
SENT = "✅ Envié el aviso al asesor."
SENT_BENEFIT = "✅ Envié el aviso a Christian sobre el año de publicidad sin costo."
ALREADY = "Tu solicitud ya fue enviada al asesor; no la repito."
FAILED = f"No pude enviar el aviso al asesor. Por favor escribe a {CONTACT_EMAIL}."
DETAILS_OK = "Gracias, envié tus datos al asesor. Si necesitas algo más, toca *Volver al menú*."
DETAILS_FAILED = f"No pude enviar tus datos al asesor. Puedes escribirlos a {CONTACT_EMAIL}."
SKIPPED = "De acuerdo. Tu solicitud ya fue enviada. Si necesitas algo más, toca *Volver al menú*."
STOP_SENT = ("El aviso anterior ya se había enviado y no puedo deshacerlo. Le envié al asesor la "
             "indicación de no contactarte, pero no puedo asegurar que la vea a tiempo.")
STOP_RECORDED = "Envié al asesor la indicación de no contactarte."
STOP_UNKNOWN = ("Envié al asesor la indicación de no contactarte. Si ya había recibido una solicitud "
                "tuya, no la puedo deshacer.")
STOP_ALREADY = ("La indicación de no contactarte ya se envió al asesor; no la repito. No puedo asegurar "
                "que la haya visto.")
STOP_SENDING = "Ya estoy enviando al asesor la indicación de no contactarte; no la repito."
STOP_FAILED = f"No pude avisar al asesor. Para que no te contacten escribe a {CONTACT_EMAIL}."
NOTHING_PENDING = "De acuerdo, no pediré que te contacten."
THANKS_REPLY = "¡Con gusto! 😊"
YES_REPLY = "Si quieres que te contacte una persona, toca *Hablar con asesor*."
UNKNOWN = "No tengo una respuesta confirmada para eso. Puedes hablar con un asesor o volver al menú."
ONLY_TEXT = "Por ahora solo puedo leer mensajes de texto y los botones."


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


def view(text, buttons=BACK, rows=None, image=False, alt=ALT_BACK) -> dict:
    return {"text": text, "buttons": tuple(buttons or ()), "list": rows, "image": image, "alt": alt}


# ── Un mensaje entrante se atiende una sola vez, aunque haya un reinicio ──────
_process = (None, "")


def _process_id() -> str:
    """Distinto en cada proceso: gunicorn crea procesos nuevos a partir del mismo código."""
    global _process
    if _process[0] != os.getpid():
        _process = (os.getpid(), f"{os.getpid()}-{uuid.uuid4().hex[:8]}")
    return _process[1]


def claim_message(mid, aux_claim, aux_get, aux_set) -> str:
    """"" si hay que atenderlo; si no, el motivo para descartarlo.

    Si lo dejó a medias otro proceso (reinicio), se vuelve a atender: es preferible
    una respuesta repetida a dejar a la persona sin respuesta. Si el almacén falla,
    se atiende."""
    if not mid:
        return ""
    key, mine = MSG_KEY + str(mid), "en_proceso:" + _process_id()
    if aux_claim(key, mine, MSG_PROCESSING_TTL) is not False:
        return ""
    current = aux_get(key)
    if current == "atendido":
        return "ya_atendido"
    if current == mine:
        return "en_proceso"
    aux_set(key, mine, MSG_PROCESSING_TTL)
    return ""


def finish_message(mid, aux_set) -> None:
    if mid:
        aux_set(MSG_KEY + str(mid), "atendido", MSG_DONE_TTL)


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
           action=None, now=time.time):
    """Atiende un mensaje. `states` y `store` son user_state y user_data de app.py.

    `send(phone, vista)` devuelve False si WhatsApp no aceptó la respuesta.
    `action` es el id del botón u opción de lista que tocó la persona.
    `lookup(phone)` lee la fila de GastroRadar del reporte: dict, {} si no hay,
    o None si no se pudo leer.

    Devuelve "repetido" si no contestó por ser la misma entrada recién atendida.
    """
    if mtype in ("reaction", "system"):
        return None
    signature = action or normalize(text) or f"[{mtype}]"
    last = (aux_get(ECHO_KEY + str(phone)) or "").split("|", 1)
    if len(last) == 2 and last[1] == signature and 0 <= now() - float(last[0]) < ECHO_SECONDS:
        return "repetido"
    outcome = {"repeatable": True}

    def deliver(to, reply):
        if send(to, reply) is False:
            outcome["repeatable"] = False         # no llegó: la persona puede insistir de inmediato

    _converse(phone, text, action, outcome, states=states, store=store, send=deliver, notify=notify,
              report=report, lookup=lookup, aux_get=aux_get, aux_set=aux_set, now=now)
    if outcome["repeatable"]:
        aux_set(ECHO_KEY + str(phone), f"{now()}|{signature}", 60)
    return None


def _converse(phone, text, action, outcome, *, states, store, send, notify, report, lookup, aux_get,
              aux_set, now):
    previous = states.get(phone, "") or ""
    mine = previous.startswith("gr_")
    topic_now = previous[3:] if mine and previous[3:] in ANSWERS else None
    data = dict(store.get(phone) or {})
    stored = data.get(KEY)
    # La antigüedad solo permite volver a pedir asesor; no da por terminada la solicitud.
    request = stored if stored and now() - stored.get("ts", 0) <= REQUEST_TTL else None
    # Los botones propios se enrutan por su id; el título visible no decide.
    commands = {"gr_inicio": "menu", "gr_asesor": "asesor", "gr_cancelar": "cancelar solicitud"}
    if action in commands:
        text = commands[action]
    t = normalize(text)

    def finish(reply, state="gr_menu", buttons=BACK, rows=None, image=False, alt=ALT_BACK, retry=False):
        states[phone] = state
        if retry:
            outcome["repeatable"] = False         # tras un fallo se permite reintentar enseguida
        send(phone, view(reply, buttons, rows, image, alt))

    def welcome(body=WELCOME):
        finish(body, buttons=WELCOME_BUTTONS, image=True, alt=ALT_WELCOME)

    def business():
        finish(BUSINESS, "gr_negocio", buttons=(), rows=BUSINESS_LIST, alt=ALT_BUSINESS)

    def topic_reply(topic, state=None):
        finish(ANSWERS[topic], state or "gr_" + topic, buttons=BUTTONS_BY_TOPIC[topic], alt=ALT_NAV)

    if action == "gr_negocio":
        return business()
    if action == "gr_terminal":                    # botón de una lista enviada antes de este cambio
        action = "gr_beneficio"
    if action == "gr_ayuda":
        return topic_reply("soporte")
    if action == "gr_banners_detalle":
        return finish(BANNER_DETAILS, "gr_banners", buttons=NAV, alt=ALT_NAV)
    if action and action.startswith("gr_") and action[3:] in ANSWERS:
        return topic_reply(action[3:])

    def product(topic):
        return f"{PRODUCT}: {LABELS.get(topic, LABELS['general'])}"

    def record(topic, estado, nombre="", producto=""):
        report(phone, producto=producto or product(topic), estado=estado, nombre=nombre)

    def forget_request():
        data.pop(KEY, None)
        store[phone] = data

    if not t:
        return finish(ONLY_TEXT, previous if mine else "gr_menu")
    if re.search(BUSINESS_WORDS, t):
        return business()

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
            return finish(STOP_FAILED, retry=True)
        if stored or (row or {}).get("Estado") == "Solicita asesor":
            return finish(STOP_SENT)
        return finish(STOP_UNKNOWN if unreadable else STOP_RECORDED)

    # 2) Atención humana.
    if (re.search(EXPLICIT, t) and "?" not in text and not re.search(NEGATION, t)
            and not re.search(DOUBT, t)):
        asked = topic_of(t) or topic_now
        if request and asked in (None, request.get("tema")) and str(phone) not in _unconfirmed:
            return finish(ALREADY, buttons=AFTER_REQUEST, alt=ALT_REQUEST)
        topic = asked or (request or {}).get("tema") or "general"
        # Una solicitud nueva permite un nuevo retiro y su aviso.
        _stop_set(phone, RELEASED, STOP_TTL, aux_set, now)
        ref = _request_ref(phone, aux_get, aux_set, now)
        ok = notify(f"GastroRadar: piden un asesor. Ref {ref}. WhatsApp: {phone}. "
                    f"Tema: {LABELS[topic]}. Mensaje: {text[:200]}")
        if ok:
            # Antes que Sheets: si el proceso se reinicia al escribir, el aviso no se repite.
            _unconfirmed.discard(str(phone))
            data[KEY] = {"ts": now(), "tema": topic, "ref": ref}
            store[phone] = data
        record(topic, "Solicita asesor" if ok else "Solicita asesor - aviso fallido")
        if not ok:
            return finish(FAILED, retry=True)
        return finish((SENT_BENEFIT if topic == "beneficio" else SENT) + ASK_DETAILS, "gr_datos",
                      buttons=AFTER_REQUEST, alt=ALT_REQUEST)

    if re.search(PERSON, t):
        return finish(CONFIRM_PENDING if request else CONFIRM, previous if mine else "gr_menu",
                      buttons=BACK if request else NAV, alt=ALT_BACK if request else ALT_NAV)

    if re.search(MENU_WORDS, t):
        return welcome()

    # 3) Sesión que venía de la conversación anterior: se pasa al menú nuevo sin
    #    interpretar su respuesta ("1", "sí", un monto) y sin borrar sus datos.
    if previous and not mine:
        return welcome(CHANGED)

    # 4) Opciones numéricas: solo si ya se mostró este menú.
    if t in OPTIONS:
        return topic_reply(OPTIONS[t]) if mine else welcome()

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
            return finish(DETAILS_OK if ok else DETAILS_FAILED, buttons=AFTER_REQUEST, alt=ALT_REQUEST,
                          retry=not ok)

    if re.search(THANKS, t):
        return finish(THANKS_REPLY, previous if topic_now else "gr_menu")
    if re.search(YES_NO, t):
        return finish(YES_REPLY, previous if topic_now else "gr_menu", buttons=NAV, alt=ALT_NAV)

    topic = topic_of(t)
    if topic == "banners" and re.search(CONTRACT + r"|\b(detalles|condiciones|requisitos)\b", t):
        return finish(BANNER_DETAILS, "gr_banners", buttons=NAV, alt=ALT_NAV)
    if topic:
        return topic_reply(topic)
    if topic_now == "banners" and re.search(CONTRACT + r"|\b(detalles|condiciones|requisitos)\b", t):
        return finish(BANNER_DETAILS, "gr_banners", buttons=NAV, alt=ALT_NAV)
    if re.search(CONTRACT, t):
        return topic_reply(topic_now if topic_now in ("beneficio", "planes") else "planes")
    if topic_now == "beneficio" and re.search(r"\b(condiciones|requisitos|detalles)\b", t):
        return topic_reply("beneficio")

    # 5) Pregunta de seguimiento sobre el tema abierto.
    if re.search(PRICE, t):
        if topic_now in ("planes", "banners", "registro"):
            return topic_reply(topic_now, previous)
        if topic_now == "beneficio":
            return finish("El beneficio está sujeto a las condiciones aplicables y no tengo confirmado el "
                          "costo de la terminal.\n\n👉 *Siguiente paso:* toca *Hablar con Christian* para "
                          "que te las explique.", previous, buttons=(B_CHRISTIAN, B_HOME), alt=ALT_NAV)
        return topic_reply("planes")

    if not mine:
        return welcome()
    return finish(UNKNOWN, previous if topic_now else "gr_menu", buttons=NAV, alt=ALT_NAV)
