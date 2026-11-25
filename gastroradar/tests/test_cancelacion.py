"""VGR-01 (issue #24): una negativa explicita cancela el contacto correspondiente.
Todo con servicios simulados; ver conftest.py."""
import pytest

from vgr.whatsapp import SendOutcome

LATER = 31 * 60  # vence el seguimiento de 30 minutos
ANSWERS = ["Ana Prueba", "Tacos Ficticios"]


def start(world, answered=0, topic="banners"):
    """ASESOR -> SI y contesta `answered` preguntas (0=nombre, 1=negocio, 2=municipio)."""
    world.say(f"Quiero {topic}")
    world.say("asesor")
    world.say("sí")
    for value in ANSWERS[:answered]:
        world.say(value)


def hold_advisor(world):
    """Meta responde 429 a los avisos al asesor: quedan en cola."""
    original, state = world.wa.send, {"blocked": True}

    def send(payload):
        if state["blocked"] and payload["to"] == world.cfg.advisor_number:
            return SendOutcome("retry", detail="http_429")
        return original(payload)
    world.wa.send = send
    return state


def by_service(world, service):
    return next(lead for lead in world.leads() if lead["service"] == service)


def consent_status(world, user):
    return (world.store.get_json(f"consent:{user}") or {}).get("status")


@pytest.mark.parametrize("answered", [0, 1, 2], ids=["nombre", "negocio", "municipio"])
@pytest.mark.parametrize("phrase", ["No quiero un asesor", "ya no quiero asesor", "no necesito un asesor",
                                    "cancelar", "ya no me interesa", "cancela mi solicitud",
                                    "no necesito que me contacten", "no me llame",
                                    "No, no quiero un asesor"])
def test_negativa_en_captura_no_genera_aviso(world, answered, phrase):
    start(world, answered)
    reply = world.say(phrase)
    world.advance(LATER)
    lead = world.leads()[0]
    assert world.advisor_msgs() == []
    assert lead["consent"] != "otorgado" and lead["status"] == "No contactar"
    assert consent_status(world, lead["wa"]) != "otorgado"
    assert "escribe BAJA" not in reply


def test_secuencia_exacta_de_work(world):
    world.say("ASESOR")
    world.say("SÍ")
    world.say("No quiero un asesor")
    world.advance(LATER)
    assert world.advisor_msgs() == []
    assert world.leads()[0]["consent"] == "cancelado"


def test_aviso_en_cola_se_cancela(world):
    hold = hold_advisor(world)
    world.make_lead()
    assert world.advisor_msgs() == []
    reply = world.say("no quiero un asesor")
    assert "no se enviará" in reply and "no puedo deshacer" not in reply
    hold["blocked"] = False
    world.advance(3600)
    world.advance(LATER)
    assert world.advisor_msgs() == []
    assert world.leads()[0]["advisor_delivery"] == "cancelado"


def test_reintentos_y_duplicados_no_reactivan(world):
    start(world, 1)
    payload = world.payload("no quiero un asesor", mid="wamid.NEG")
    for _ in range(3):
        assert world.post(payload).status_code == 200
    world.svc.drain()
    world.say("no quiero un asesor")
    world.boot()
    world.store.revive()
    world.advance(LATER)
    world.advance(24 * 3600)
    lead = world.leads()[0]
    assert world.advisor_msgs() == [] and lead["consent"] == "cancelado"
    assert sum("canceló" in h for h in lead["history"]) == 1
    assert world.svc.renotify(lead["id"]) is False


def test_negativa_despues_de_enviado_no_promete_deshacer(world):
    world.make_lead()
    assert len(world.advisor_msgs()) == 1
    reply = world.say("no quiero un asesor")
    assert "ya se había enviado" in reply and "no puedo deshacerlo" in reply
    assert "no se enviará" not in reply
    msgs = world.advisor_msgs()
    assert len(msgs) == 2 and "NO CONTACTAR" in world.advisor_text(msgs[1])
    world.say("no quiero un asesor")
    world.advance(LATER)
    assert len(world.advisor_msgs()) == 2
    assert world.leads()[0]["consent"] == "cancelado"


def test_nueva_autorizacion_explicita(world):
    start(world)
    world.say("no quiero un asesor")
    assert "autoriz" in world.say("asesor").lower()
    assert world.leads()[0]["consent"] == "cancelado"   # pedir asesor no basta: falta el SI
    world.say("sí")
    for value in ANSWERS + ["Culiacán"]:
        world.say(value)
    assert len(world.advisor_msgs()) == 1
    assert world.leads()[0]["consent"] == "otorgado"


def test_seguimiento_viejo_no_actua_sobre_la_autorizacion_nueva(world):
    start(world)
    world.advance(20 * 60)
    world.say("cancelar")
    world.say("asesor")
    world.say("sí")
    world.advance(11 * 60)          # vence el seguimiento de la solicitud cancelada
    assert world.advisor_msgs() == []
    world.advance(20 * 60)          # vence el de la nueva
    assert len(world.advisor_msgs()) == 1


def test_aviso_viejo_en_cola_no_revive_al_reautorizar(world):
    hold = hold_advisor(world)
    world.make_lead()
    world.say("no quiero un asesor")
    world.say("asesor")
    world.say("sí")                  # el perfil se conserva: queda completo de nuevo
    hold["blocked"] = False
    for _ in range(3):
        world.advance(3600)
    assert len(world.advisor_msgs()) == 1   # solo el aviso de la autorizacion nueva


def test_sin_negativa_el_aviso_legitimo_sigue(world):
    start(world, 1)
    world.advance(LATER)
    msgs = world.advisor_msgs()
    assert len(msgs) == 1 and "incompleto" in world.advisor_text(msgs[0])


@pytest.mark.parametrize("phrase", ["No, quiero un asesor", "no. necesito un asesor"])
def test_no_con_coma_y_peticion_no_es_negativa(world, phrase):
    reply = world.say(phrase)
    assert "autoriz" in reply.lower()


def test_negativa_no_toca_solicitud_ya_enviada_de_otro_servicio(world):
    start(world, topic="banners")
    world.advance(LATER)             # banners ya llego al asesor
    world.say("menu")
    world.say("terminal")
    world.say("asesor")
    reply = world.say("no quiero un asesor")
    world.advance(LATER)
    assert len(world.advisor_msgs()) == 1
    assert by_service(world, "publicidad")["consent"] == "otorgado"
    assert by_service(world, "tpv")["consent"] == "cancelado"
    assert "sin cambios" in reply


def test_negativa_con_servicio_cancela_solo_ese(world):
    start(world, topic="banners")
    world.say("menu")
    world.say("terminal")
    world.say("asesor")
    world.say("no quiero un asesor para la terminal")
    world.advance(LATER)
    assert by_service(world, "tpv")["consent"] == "cancelado"
    assert by_service(world, "publicidad")["consent"] == "otorgado"
    msgs = world.advisor_msgs()
    assert len(msgs) == 1 and by_service(world, "publicidad")["id"] in world.advisor_text(msgs[0])


def test_negativa_generica_cancela_todo_lo_no_enviado(world):
    start(world, topic="banners")
    world.say("menu")
    world.say("terminal")
    world.say("asesor")
    world.say("no quiero un asesor")
    world.advance(LATER)
    assert world.advisor_msgs() == []
    assert {lead["consent"] for lead in world.leads()} == {"cancelado"}


def test_cancelar_en_captura_solo_cancela_la_solicitud_actual(world):
    start(world, topic="banners")
    world.say("menu")
    world.say("terminal")
    world.say("asesor")
    world.say("cancelar")
    assert by_service(world, "tpv")["consent"] == "cancelado"
    assert by_service(world, "publicidad")["consent"] == "otorgado"


def test_baja_posterior_sigue_revocando_todo(world):
    start(world)
    world.say("no quiero un asesor")
    world.say("BAJA")
    world.advance(LATER)
    lead = world.leads()[0]
    assert lead["consent"] == "revocado" and consent_status(world, lead["wa"]) == "revocado"
    assert world.advisor_msgs() == []


def test_negativa_durante_envio_espera_y_luego_avisa_que_no_contacte(world):
    """La negativa entra mientras Meta esta recibiendo el aviso: no puede
    procesarse a la mitad ni responder que no se envio."""
    original, seen = world.wa.send, {}

    def send(payload):
        if payload["to"] == world.cfg.advisor_number and not seen:
            seen["x"] = True
            world.post(world.payload("no quiero un asesor", mid="wamid.CARRERA"))
            for _ in range(20):
                world.svc.work_once()
            seen["procesado_en_medio"] = world.leads()[0]["consent"] != "otorgado"
        return original(payload)
    world.wa.send = send
    world.make_lead()
    world.advance(5)
    assert seen["procesado_en_medio"] is False
    replies = "\n".join(p["text"]["body"] for p in world.wa.sent if p["to"] != world.cfg.advisor_number)
    assert "no puedo deshacerlo" in replies
    assert len(world.advisor_msgs()) == 2


# ── segunda vuelta adversarial sobre la correccion ──────────────────────────

@pytest.mark.parametrize("phrase", ["ya no quiero", "ya no gracias", "ya no quiero nada",
                                    "mejor ya no", "No, ya no me interesa"])
def test_otras_formas_de_cancelar_no_se_guardan_como_dato(world, phrase):
    start(world)
    world.say(phrase)
    world.advance(LATER)
    assert world.advisor_msgs() == []
    assert world.leads()[0]["name"] == "" and world.leads()[0]["consent"] == "cancelado"


def test_limite_de_frecuencia_no_descarta_una_negativa():
    from conftest import World
    world = World(user_msgs_per_10min=3)
    start(world)                       # ya son 3 mensajes
    world.say("no quiero un asesor")
    world.advance(LATER)
    assert world.advisor_msgs() == []


def test_negativa_atrasada_mas_de_24h_si_se_aplica(world):
    hold = hold_advisor(world)
    world.make_lead()
    world.post(world.payload("no quiero un asesor", ts=world.clock.t - 25 * 3600))
    world.svc.drain()
    hold["blocked"] = False
    for _ in range(3):
        world.advance(3600)
    assert world.advisor_msgs() == []


def test_negativa_recibida_pero_sin_procesar_detiene_el_aviso(world):
    hold = hold_advisor(world)
    world.make_lead()
    world.advance(60)
    world.post(world.payload("no quiero un asesor"))   # llego, aun en cola
    hold["blocked"] = False
    for _ in range(5):
        world.advance(60)
    assert world.advisor_msgs() == []


@pytest.mark.parametrize("phrase", [
    "no quiero un asesor por ahora, solo quiero ver los planes",
    "no quiero un asesor por el precio",
    "no necesito asesor para saber los planes",
])
def test_mencionar_otro_tema_no_desvia_la_cancelacion(world, phrase):
    start(world, topic="terminal")
    world.say(phrase)
    world.advance(LATER)
    assert world.advisor_msgs() == []
    assert world.leads()[0]["consent"] == "cancelado"


@pytest.mark.parametrize("phrase", ["no quiero cancelar mi solicitud", "¿cómo cancelo mi solicitud?"])
def test_frases_que_no_cancelan(world, phrase):
    start(world)
    world.say(phrase)
    assert world.leads()[0]["consent"] == "otorgado"


@pytest.mark.parametrize("answered", [0, 2], ids=["nombre", "municipio"])
@pytest.mark.parametrize("phrase", [
    "siempre no", "cancélalo", "olvídenlo", "ya no es necesario", "no hace falta el asesor",
    "no quiero que nadie me llame", "mejor no", "no gracias", "ya no lo quiero", "detente", "para ya",
    "no quiero nada", "déjalo así", "ya no quiero el servicio", "no quiero seguir", "ya no deseo continuar",
    "no sigas",
])
def test_retiro_coloquial_en_captura(world, answered, phrase):
    start(world, answered)
    world.say(phrase)
    world.advance(LATER)
    assert world.advisor_msgs() == [] and world.leads()[0]["consent"] != "otorgado"


@pytest.mark.parametrize("phrase", ["no gracias", "ya no, gracias", "mejor no", "no quiero nada", "salir"])
def test_despedida_despues_de_completar_no_cancela(world, phrase):
    world.make_lead()
    world.say(phrase)
    assert world.leads()[0]["consent"] == "otorgado" and len(world.advisor_msgs()) == 1


@pytest.mark.parametrize("value", ["omitir", "no tengo", "no"])
def test_omitir_un_dato_sigue_funcionando(world, value):
    start(world, 2)
    world.say(value)
    assert world.leads()[0]["consent"] == "otorgado" and len(world.advisor_msgs()) == 1
