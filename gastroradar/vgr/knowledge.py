"""Unica fuente de textos comerciales. Contrastada con https://tugastroradar.com/planes
el 25-nov-2026. Cualquier cambio de precio se hace aqui y en las pruebas."""

SITE = "https://tugastroradar.com"
PLANS_URL = SITE + "/planes"
HELP_URL = SITE + "/ayuda"
PRIVACY_URL = SITE + "/aviso"
CONTACT_EMAIL = "contacto@tugastroradar.com"
CONSENT_VERSION = "vicky-contacto-v1"

SERVICES = {
    "registro": "Registro de comercio", "planes": "Contratación de plan",
    "publicidad": "Publicidad y banners", "tpv": "Terminal punto de venta",
    "soporte": "Soporte", "humano": "Atención humana", "consumidor": "Consumidor",
}
NEEDS_BUSINESS = {"registro", "planes", "publicidad", "tpv"}

MENU = ("1. Encontrar comida\n2. Registrar mi negocio\n3. Planes\n4. Publicidad y banners\n"
        "5. Terminal punto de venta\n6. Soporte o atención humana")
GREETING = ("Hola, soy Vicky, asistente virtual de GastroRadar. ¿En qué te ayudo?\n" + MENU +
            "\nResponde con el número o escribe tu consulta.")
OFFER_ADVISOR = "Responde ASESOR si deseas que te contacte una persona."


def info(topic: str, cfg) -> str:
    if topic == "consumidor":
        return (f"Puedes buscar dónde comer en {SITE} por nombre, municipio, tipo de comida o con "
                "«Cerca de mí». No necesitas cuenta.")
    if topic == "registro":
        return (f"Registrarte es gratis. Entra a {PLANS_URL}, busca tu negocio y pide administrarlo; "
                "verificamos por teléfono que lo representas. El Plan Esencial no tiene cobro.\n"
                + OFFER_ADVISOR)
    if topic == "planes":
        status = ("" if cfg.online_contracting else
                  "\nPor ahora la contratación en línea no está disponible.")
        return ("Planes (IVA incluido):\n• Esencial: gratis\n• Conecta: $39.90 al mes\n"
                "• Presencia: $89.90 al mes\nEl sitio anuncia primer mes sin costo en Conecta y Presencia."
                f"{status}\nBeneficios: {PLANS_URL}\n" + OFFER_ADVISOR)
    if topic == "publicidad":
        return ("Los banners cuestan $499 al mes, IVA incluido, por posición y ubicación contratada. "
                "Se contratan aparte y no requieren un plan. La disponibilidad y las fechas se confirman "
                f"en cada caso. Detalles: {PLANS_URL}\n" + OFFER_ADVISOR)
    if topic == "tpv":
        benefit = ("Hay un beneficio de un año del Plan Presencia sin costo con la terminal Inbursa, "
                   "sujeto a condiciones autorizadas. " if cfg.tpv_benefit_verified else "")
        return (benefit + "La terminal es un servicio de Inbursa, distinto de los planes de GastroRadar. "
                "Un asesor te confirma requisitos y condiciones.\n" + OFFER_ADVISOR)
    if topic == "soporte":
        return (f"Puedes reportar el problema en {HELP_URL} o escribir a {CONTACT_EMAIL}. "
                "No envíes contraseñas ni datos de tarjeta.\n" + OFFER_ADVISOR)
    return GREETING


def consent_question(service: str) -> str:
    return (f"Para que un asesor te contacte sobre «{SERVICES.get(service, service)}» necesito tu "
            "autorización para guardar tu número y los datos que me des, y usarlos solo para atender "
            f"esta solicitud. Aviso: {PRIVACY_URL}\n¿Autorizas? Responde SÍ o NO. "
            "Puedes retirar tu autorización escribiendo BAJA.")


QUESTIONS = {
    "name": "¿Cuál es tu nombre?",
    "business": "¿Cómo se llama tu negocio? (puedes responder OMITIR)",
    "municipality": "¿En qué municipio estás? (puedes responder OMITIR)",
}
