"""Conversacion determinista. Funcion sin red ni Redis.

El modelo de IA nunca cambia consentimiento ni crea prospectos: solo puede
proponer un tema y, en temas no comerciales, un texto ya validado.
"""
import re
import unicodedata

from . import knowledge as kb


def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


OPTOUT = re.compile(
    r"^(baja|stop|alto|unsubscribe|cancelar suscripcion)$|"
    r"\bdar(me)? de baja\b(?! (a )?(mi|la|el|un) (negocio|ficha|restaurante|local|puesto))|"
    r"\bdenme de baja\b|"
    r"\bno quiero que me (contacten|llamen|escriban|busquen|manden|envien)\b|"
    r"\b(dejen|deja) de (escribirme|llamarme|mandarme|enviarme|contactarme|molestarme)\b|"
    r"\bno me (manden|mandes|envien|envies)( mas)? (mensajes|nada)\b|"
    r"\bno quiero (recibir )?(mas )?mensajes\b|"
    r"\bno (me )?(contacten|contactes|llamen|llames|escriban|escribas|busquen)\b|"
    r"\bya no quiero que me (contacten|llamen|escriban|busquen)\b|"
    r"\bno quiero que nadie me (contacte|llame|escriba|busque|marque)\b|"
    r"\bno (necesito|deseo|ocupo) que me (contacten|llamen|escriban|busquen)\b|"
    r"\b(no me|no (quiero|necesito|deseo) que me) (contacte|llame|escriba|busque|marque|marquen)\b"
    r"(?! la atencion)|"
    r"\b(retiro|revoco|cancelo) (mi )?(consentimiento|autorizacion|permiso)\b|\bno autorizo\b")
DELETE = re.compile(r"\b(borr|elimin|quit)\w* (todos )?(mis datos|datos|mi numero|mi informacion)\b")
# Negativa a la atencion humana: cancela el contacto, sin exigir BAJA.
NO_HUMAN = re.compile(
    r"\b(no|sin|ni) (quiero|necesito|ocupo|requiero|deseo)( hablar con)?( un| una| el| al| ningun)? "
    r"(asesor|asesora|humano|persona|agente|ejecutivo)\b|"
    r"\bno (quiero|necesito|deseo)( el| ningun)? contacto\b|"
    r"\bno hace falta( un| el)? (asesor|asesora)\b|"
    r"^(ya )?(por favor )?(quiero )?(cancela|cancele|cancelar|cancelen|anula|anular|anulen)( la| mi| el)? "
    r"(solicitud|asesor|contacto|llamada|cita)\b")
# "No, quiero un asesor" es una peticion: el "no" con puntuacion contesta a otra cosa.
LEADING_NO = re.compile(r"^\s*no\s*[,.;!]+", re.IGNORECASE)
# Solo acota si la frase TERMINA nombrando el servicio; ante la duda, la negativa es general.
SCOPE = re.compile(r" ?(para|sobre)( la| el| lo de| mi| los| las)? "
                   r"(terminal|tpv|punto de venta|publicidad|banner|banners|registro|plan|planes)")
# Frases que son peticiones o preguntas, no un nombre, negocio o municipio.
NOT_DATA = re.compile(r"^(no (quiero|me|te|gracias)|para que|por que|ver |quiero|quisiera|necesito|"
                      r"me interesa|cuanto|cuando|como (es|se|le|puedo|funciona)|"
                      r"que (es|son|cuesta|planes|precio|incluye)|donde|puedo|tienen|hay |dame|ayuda)")
REFUSE_FIELD = re.compile(r"^(no quiero|prefiero no|no te voy a|no voy a)( \w+)? (dar|decir|compartir)")
INJECTION = re.compile(
    r"\b(ignora|olvida|omite)\b.*\b(instrucciones|reglas|prompt)\b|\bsystem prompt\b|"
    r"\b(tus|las) instrucciones (internas|del sistema)\b|\b(muestra|revela|dime|imprime)\w*\b.*"
    r"\b(prompt|token|api key|clave|contrasena|variables de entorno|credencial)|"
    r"\bactua como\b|\bahora eres\b|\bmodo (desarrollador|admin)\b|\bignore (all|previous)\b")
HUMAN = re.compile(r"\b(asesor|asesora|humano|persona real|una persona|ejecutivo|agente|"
                   r"que me (llamen|contacten))\b")
INTENTS = [
    ("tpv", r"\b(terminal|tpv|punto de venta|cobrar con tarjeta|inbursa)\b"),
    ("publicidad", r"\b(banner|banners|publicidad|anunci\w+|promocionar)\b"),
    ("soporte", r"\b(soporte|problema|error|no funciona|no puedo|falla|reporte|queja|corregir|retirar|"
                r"de baja)\b"),
    ("registro", r"\b(registr\w+|dar de alta|alta de|inscribir\w*|mi (negocio|restaurante|local|puesto))\b"),
    ("planes", r"\b(plan|planes|precio|precios|cuesta|costo|conecta|presencia|esencial|contratar|"
               r"mensualidad)\b"),
    ("consumidor", r"\b(comer|comida|restaurante|restaurantes|tacos|sushi|mariscos|pizza|cenar|"
                   r"desayunar|hamburguesas|antojo|donde)\b"),
]
MENU_OPTIONS = {"1": "consumidor", "2": "registro", "3": "planes", "4": "publicidad",
                "5": "tpv", "6": "soporte"}
GREET = re.compile(r"^(hola|buenos dias|buenas tardes|buenas noches|buenas|menu|inicio|info|"
                   r"informacion)( vicky)?$")
THANKS = re.compile(r"^(muchas )?(gracias|ok|okay|va|perfecto|excelente|de acuerdo|adios|bye)( gracias)?$")
YES = re.compile(r"^(si|sii+|s|claro|acepto|autorizo|si autorizo|si acepto|si claro|si por favor|dale)$")
NO = re.compile(r"^(no|nel|no gracias|no acepto|rechazo|mejor no)$")
SKIP = re.compile(r"^(omitir|saltar|no se|no tengo|ninguno|no aplica|prefiero no|no quiero decir\w*)$")
# Retiro claro en cualquier momento.
CANCEL = re.compile(r"^(cancelar|cancela|cancelalo|cancelenlo|olvidalo|olvidenlo|siempre no|"
                    r"(ya )?no me interesa)( gracias| por favor)?$")
# Solo mientras se piden datos: fuera de ahi pueden ser una simple despedida.
CANCEL_CAPTURE = re.compile(
    r"^(salir|(mejor )?ya no( gracias| por favor)?|(mejor |no )?gracias no|mejor no( gracias)?|no gracias|"
    r"(ya )?no es necesario|(ya )?no hace falta|ya no (lo )?quiero( nada| el servicio)?|"
    r"(ya )?no (quiero|deseo) (nada|seguir|continuar)|dejalo asi|dejenlo asi|detente|no sigas|para ya)$")


def withdraws(t: str, capturing: bool) -> bool:
    return bool(CANCEL.match(t) or (capturing and CANCEL_CAPTURE.match(t)))


QUESTIONISH = re.compile(r"\b(cuanto|como|que|cual|donde|cuando|por que|tienen|puedo|hay)\b")
COMMERCIAL = ("registro", "planes", "publicidad", "tpv")


def detect(text_n: str):
    for name, pattern in INTENTS:
        if re.search(pattern, text_n):
            return name
    return None


def intent_text(text: str) -> str:
    """Texto normalizado para evaluar bajas y negativas."""
    return norm(LEADING_NO.sub("", text or "", count=1))


def declined_service(match, text_n: str) -> str:
    """Servicio nombrado en la negativa ("... para la terminal"); "" si es general."""
    scope = SCOPE.fullmatch(text_n[match.end():])
    service = detect(scope.group(3)) if scope else None
    return service if service in kb.SERVICES else ""


def is_stop(text: str) -> bool:
    """Baja, negativa o cancelacion: nunca se descarta por antiguedad ni por frecuencia."""
    t = intent_text(text)
    return is_optout(text) or bool(NO_HUMAN.search(t)) or withdraws(t, True)


def is_optout(text: str) -> bool:
    t = intent_text(text)
    return bool(OPTOUT.search(t) or DELETE.search(t))


def clean_value(text: str, limit=60) -> str:
    text = "".join(c for c in text if c == " " or not unicodedata.category(c).startswith("C"))
    return re.sub(r"\s+", " ", text).strip(" .,;:")[:limit]


def valid_field(name: str, raw: str) -> str:
    value = clean_value(raw)
    value = re.sub(r"(?i)^(me llamo|mi nombre es|soy|se llama|mi negocio es|estoy en|en)\s+", "", value)
    letters = sum(c.isalpha() for c in value)
    if letters < 2 or len(raw) > 80 or "http" in raw.lower() or len(re.findall(r"\d", value)) > 6:
        return ""
    if name == "name" and len(value.split()) > 6:
        return ""
    return value


class Result:
    def __init__(self):
        self.replies = []
        # (evento, servicio): consent_granted, consent_denied, consent_revoked,
        # data_deletion, contact_declined, request_cancelled, lead_update, lead_ready.
        # La respuesta de las dos cancelaciones la redacta el servicio, que sabe que se envio.
        self.events = []
        self.needs_ai = False

    def say(self, text):
        self.replies.append(text)


def missing_field(session: dict, service: str):
    profile = session.setdefault("profile", {})
    for name in ("name", "business", "municipality"):
        if name == "business" and service not in kb.NEEDS_BUSINESS:
            continue
        if name not in profile:
            return name
    return None


def _advance(session, res):
    service = session["service"]
    pending = missing_field(session, service)
    if pending:
        session["step"] = pending
        res.say(kb.QUESTIONS[pending])
    else:
        session["step"] = None
        res.events.append(("lead_ready", service))
        res.say("Gracias. Registré tu solicitud para que un asesor la revise. "
                "No puedo prometer un horario de respuesta. Escribe BAJA si ya no quieres que te contacten.")


def _start_contact(session, res, service, consented):
    session["service"] = service
    if consented:
        res.events.append(("lead_update", service))
        _advance(session, res)
    else:
        session["step"] = "consent"
        res.say(kb.consent_question(service))


def handle(session: dict, text: str, consented: bool, cfg, ai=None) -> Result:
    """Modifica `session` y devuelve respuestas y eventos.

    `ai`: None = aun no se consulto; False = no disponible; dict = salida validada.
    Si hace falta el modelo se devuelve needs_ai=True sin tocar la sesion.
    """
    res = Result()
    t = norm(text)
    step = session.get("step")

    # La baja se evalua primero, en cualquier estado.
    negative = intent_text(text)
    if DELETE.search(negative):
        session.update(step=None, profile={})
        res.events.append(("data_deletion", session.get("service") or ""))
        res.say("Retiré tu autorización de contacto. El borrado de datos lo atiende una persona: "
                f"escribe a {kb.CONTACT_EMAIL} indicando este número. Aviso: {kb.PRIVACY_URL}")
        return res
    if OPTOUT.search(negative):
        session["step"] = None
        res.events.append(("consent_revoked", session.get("service") or ""))
        res.say("Listo. Registré que no quieres que te contacten. Puedes seguir escribiéndome cuando quieras.")
        return res
    if INJECTION.search(t):
        res.say("Solo puedo ayudarte con GastroRadar y no comparto información interna.\n" + kb.MENU)
        return res

    declined = NO_HUMAN.search(negative)
    if declined:
        session["step"] = None
        res.events.append(("contact_declined", declined_service(declined, negative)))
        return res
    # "Cancelar" retira la solicitud en curso. "Salir" solo cuenta mientras se piden datos.
    capturing = step in kb.QUESTIONS
    if step != "consent" and (capturing or session.get("service")) and (
            withdraws(t, capturing) or withdraws(negative, capturing and negative != "gracias")):
        session["step"] = None
        res.events.append(("request_cancelled", session["service"]))
        return res

    # Pedir el menu o saludar saca de la captura de datos: nadie queda atrapado.
    if step in kb.QUESTIONS and (GREET.match(t) or t in MENU_OPTIONS):
        session["step"] = step = None

    if step == "consent":
        if YES.match(t):
            res.events.append(("consent_granted", session["service"]))
            _advance(session, res)
            return res
        if NO.match(t):
            session["step"] = None
            res.events.append(("consent_denied", session["service"]))
            res.say("De acuerdo, no registraré tus datos para contactarte. ¿Te ayudo con otra cosa?")
            return res
        if withdraws(t, True) or GREET.match(t):
            session["step"] = None
            res.say(kb.GREETING)
            return res
        topic = detect(t)
        if topic:
            res.say(kb.info(topic, cfg).replace("\n" + kb.OFFER_ADVISOR, ""))
        res.say("Para continuar con el contacto responde SÍ o NO.")
        return res

    if step in kb.QUESTIONS:
        if REFUSE_FIELD.match(t) or ((SKIP.match(t) or NO.match(t)) and step != "name"):
            session["profile"][step] = ""
            _advance(session, res)
            return res
        topic = detect(t)
        # En "negocio" son validos nombres como "Tacos El Guero"; en los otros campos, un tema no es un dato.
        asks = ("?" in text or NOT_DATA.match(t) or (topic and QUESTIONISH.search(t))
                or (step != "business" and topic in ("publicidad", "planes", "tpv", "soporte", "registro")))
        not_a_value = asks or HUMAN.search(t) or any(p.match(t) for p in (THANKS, YES, SKIP, NO))
        value = "" if not_a_value else valid_field(step, text)
        if value:
            session["misses"] = 0
            session["profile"][step] = value
            res.events.append(("lead_update", session["service"]))
            _advance(session, res)
            return res
        if asks:
            res.say(kb.info(topic, cfg).replace("\n" + kb.OFFER_ADVISOR, "") if topic
                    else "Esa duda la puede revisar el asesor.")
        session["misses"] = session.get("misses", 0) + 1
        if session["misses"] >= 2:
            # No se insiste una tercera vez: se deja el dato vacio.
            session["misses"] = 0
            session["profile"][step] = ""
            _advance(session, res)
        else:
            res.say(kb.QUESTIONS[step])
        return res

    if t == "6":
        session["topic"] = "soporte"
        res.say(kb.info("soporte", cfg))
        return res
    topic = MENU_OPTIONS.get(t)
    if not topic:
        if HUMAN.search(t):
            service = detect(t) or session.get("topic")
            if not service or service == "consumidor":
                service = "humano"
            _start_contact(session, res, service, consented)
            return res
        if GREET.match(t):
            res.say(kb.GREETING)
            return res
        if THANKS.match(t):
            res.say("Con gusto. Si necesitas algo más, escribe MENU.")
            return res
        topic = detect(t)

    # Temas comerciales y soporte: texto fijo; el modelo no redacta precios ni condiciones.
    if topic in COMMERCIAL or topic == "soporte":
        session["topic"] = topic
        res.say(kb.info(topic, cfg))
        return res
    if ai is None and t not in MENU_OPTIONS and len(t.split()) > 1:
        res.needs_ai = True
        return res
    if ai:
        intent = ai["intent"]
        if intent in COMMERCIAL:
            session["topic"] = intent
            res.say(kb.info(intent, cfg))
        elif intent == "humano" or ai.get("needs_human"):
            res.say("Esto conviene revisarlo con una persona.\n" + kb.OFFER_ADVISOR)
        else:
            if intent in kb.SERVICES:
                session["topic"] = intent
            res.say(ai["reply"] or (kb.info(topic, cfg) if topic else kb.GREETING))
        return res
    if topic:
        session["topic"] = topic
        res.say(kb.info(topic, cfg))
    else:
        res.say("No estoy segura de haber entendido. Puedes elegir una opción o escribir ASESOR.\n" + kb.MENU)
    return res
