"""Fallos buscados en la auditoria adversarial. Cada uno se reprodujo antes de corregirse
o protege una garantia del diseno."""
import json
import time

import pytest

from conftest import USER, World


@pytest.mark.parametrize("phrase", [
    "no quiero que me contacten", "ya no quiero que me llamen", "dejen de escribirme",
    "ya no me manden mensajes", "no quiero recibir mensajes", "eliminen mi número",
    "quiero darme de baja", "No me contacten por favor"])
def test_frases_de_baja(world, phrase):
    world.make_lead()
    world.say(phrase)
    assert world.store.get_json(f"consent:{USER}")["status"] == "revocado"


def test_baja_de_ficha_no_es_revocacion(world):
    reply = world.say("quiero dar de baja mi negocio")
    assert "/ayuda" in reply and world.store.get_json(f"consent:{USER}") is None


@pytest.mark.parametrize("phrase", ["no quiero un asesor", "no necesito asesor"])
def test_negar_asesor_no_inicia_contacto(world, phrase):
    assert "¿Autorizas?" not in world.say(phrase)


@pytest.mark.parametrize("phrase", [
    "quiero publicidad", "no quiero dar mi nombre", "para que lo necesitas", "no me interesa",
    "cuanto es", "ver planes"])
def test_frases_no_se_guardan_como_nombre(world, phrase):
    world.say("asesor")
    world.say("sí")
    world.say(phrase)
    assert all(lead["name"] == "" for lead in world.leads())


@pytest.mark.parametrize("exit_word", ["menu", "hola", "3"])
def test_se_puede_salir_de_la_captura(world, exit_word):
    world.say("asesor")
    world.say("sí")
    reply = world.say(exit_word)
    assert "¿Cuál es tu nombre?" not in reply
    assert all(lead["name"] == "" for lead in world.leads())


def test_datos_completos_tras_aviso_incompleto_si_se_avisan(world):
    world.say("quiero publicidad")
    world.say("asesor")
    world.say("sí")
    world.advance(31 * 60)
    assert len(world.advisor_msgs()) == 1 and "incompleto" in world.advisor_text(world.advisor_msgs()[0])
    for value in ("Ana Prueba", "Tacos Ficticios", "Culiacán"):
        world.say(value)
    assert len(world.advisor_msgs()) == 2 and "Ana Prueba" in world.advisor_text(world.advisor_msgs()[1])


@pytest.mark.parametrize("reply", [
    "El plan Presencia es gratis.", "El banner sale en quinientos al mes.",
    "La terminal Inbursa no tiene comisión."])
def test_modelo_no_habla_de_temas_comerciales(world, reply):
    world.openai.content = json.dumps({"intent": "otro", "reply": reply, "needs_human": False})
    assert reply not in world.say("pregunta libre sobre cualquier cosa")


def test_baja_cancela_aviso_pendiente_y_no_se_sobrescribe(world):
    from vgr.whatsapp import SendOutcome
    original = world.wa.send
    world.wa.send = lambda p: (SendOutcome("retry", detail="429") if p["to"] == world.cfg.advisor_number
                               else original(p))
    world.make_lead()
    world.wa.send = original
    world.say("baja")
    world.advance(3600)
    texts = [world.advisor_text(p) for p in world.advisor_msgs()]
    assert len(texts) == 1 and "NO CONTACTAR" in texts[0]
    assert world.leads()[0]["consent"] == "revocado"


def test_reintento_manual_ignora_la_pausa_del_carril(world):
    world.sheet.fail = True
    world.make_lead()
    for _ in range(25):
        world.advance(3600)
    world.sheet.fail = False
    world.store.revive()
    world.svc.drain()
    assert len(world.sheet.rows) == 2


def test_limite_de_frecuencia():
    world = World(user_msgs_per_10min=3)
    replies = [world.say("hola") for _ in range(8)]
    assert sum(bool(r) for r in replies) == 4 and "muchos mensajes" in replies[3]
    assert world.say("baja")


def test_bot_apagado_no_procesa():
    world = World(enabled=False)
    assert world.post(world.payload("hola")).get_json()["status"] == "disabled"
    assert world.svc.drain() == 0


def test_hilos_reales_y_ready(world):
    world.store.clock = time.time
    world.svc.start_workers()
    try:
        world.post(world.payload("hola", ts=time.time()))
        deadline = time.time() + 5
        while not world.wa.sent and time.time() < deadline:
            time.sleep(0.05)
        assert len(world.wa.sent) == 1
        body = world.client.get("/ready")
        assert body.status_code == 200, body.get_json()
    finally:
        world.svc.stop.set()
        for thread in world.svc.threads:
            thread.join(3)
