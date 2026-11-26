"""Entorno simulado: Redis en memoria (fakeredis), Meta, OpenAI y Sheets falsos.
Todos los datos son ficticios. Ninguna prueba abre conexiones reales."""
import hashlib
import hmac
import itertools
import json
import os
import socket
import sys
from pathlib import Path

os.environ["VGR_NO_AUTOSTART"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fakeredis
import pytest

from app import create_app
from vgr.ai import AI
from vgr.config import Config
from vgr.service import Service
from vgr.sheets import Sheets
from vgr.store import Store
from vgr.whatsapp import SendOutcome

PHONE_ID = "100000000000001"
USER = "5215550000001"
ADVISOR = "5215550000099"
BOT = "5215550000077"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_a, **_k):
        raise AssertionError("una prueba intento usar la red")
    monkeypatch.setattr(socket.socket, "connect", blocked)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class FakeWA:
    def __init__(self):
        self.sent, self.script, self.ids = [], [], itertools.count(1)

    def send(self, payload):
        self.sent.append(payload)
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return SendOutcome("accepted", wamid=f"wamid.OUT{next(self.ids)}")

    def check_number(self):
        return {"token_ok": True, "detalle": "", "numero": "+" + BOT}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeOpenAI:
    def __init__(self):
        self.calls, self.fail, self.content = [], None, None

    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        if self.fail:
            raise self.fail
        content = self.content or json.dumps({"intent": "consumidor", "needs_human": False,
                                              "reply": "Busca en https://tugastroradar.com por municipio."})
        return Resp(200, {"choices": [{"message": {"content": content}}]})


class FakeSheetSession:
    """Imita la API REST de valores de Sheets."""

    def __init__(self):
        self.rows, self.fail, self.params = [], False, []

    def request(self, method, url, timeout=None, params=None, json=None):
        if self.fail:
            return Resp(503, {})
        self.params.append(params)
        rng = url.split("/values/")[1]
        if method == "GET" and rng.endswith("A1%3AL1"):
            return Resp(200, {"values": self.rows[:1]} if self.rows else {})
        if method == "GET":
            return Resp(200, {"values": [[r[0]] for r in self.rows]})
        row = json["values"][0]
        if rng.endswith(":append"):
            self.rows.append(row)
        else:
            index = int(rng.split("%21A")[1].split("%3A")[0]) - 1
            if index < len(self.rows):
                self.rows[index] = row
            else:
                self.rows.append(row)
        return Resp(200, {})


def make_config(**overrides):
    values = dict(enabled=True, meta_token="test-meta-token-SECRETO", phone_number_id=PHONE_ID,
                  verify_token="test-verify", app_secret="test-app-secret", graph_version="v0.0",
                  bot_number=BOT, kv_url="redis://fake", advisor_number=ADVISOR,
                  advisor_template="aviso_prospecto_prueba", admin_token="t" * 32,
                  sheet_id="hoja-prueba", ai_enabled=True, openai_key="sk-test-SECRETO")
    values.update(overrides)
    return Config(**values)


class World:
    def __init__(self, **overrides):
        self.cfg = make_config(**overrides)
        self.server = fakeredis.FakeServer()
        self.clock = Clock()
        self.wa, self.openai, self.sheet = FakeWA(), FakeOpenAI(), FakeSheetSession()
        self.ids = itertools.count(1)
        self.boot()

    def boot(self):
        """Arranca (o reinicia) el proceso sobre el mismo Redis."""
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.store = Store(self.redis, clock=self.clock)
        self.svc = Service(self.cfg, self.store, whatsapp=self.wa,
                           ai=AI(self.cfg, self.store, http=self.openai),
                           sheets=Sheets(self.cfg, session=self.sheet))
        self.app = create_app(self.cfg, self.svc, start_workers=False)
        self.client = self.app.test_client()

    def payload(self, text="hola", user=USER, mid=None, kind="text", phone_id=PHONE_ID, ts=None, **extra):
        msg = {"id": mid or f"wamid.IN{next(self.ids)}", "from": user, "type": kind,
               "timestamp": str(int(ts or self.clock.t)), **extra}
        if kind == "text":
            msg["text"] = {"body": text}
        return {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_id}, "messages": [msg]}}]}]}

    def status(self, wamid, status, errors=()):
        return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": PHONE_ID}, "statuses": [
            {"id": wamid, "status": status, "errors": [{"code": c} for c in errors]}]}}]}]}

    def post(self, payload, secret=None, raw=None):
        raw = raw if raw is not None else json.dumps(payload).encode()
        key = (secret or self.cfg.app_secret).encode()
        signature = "sha256=" + hmac.new(key, raw, hashlib.sha256).hexdigest()
        return self.client.post("/webhook", data=raw, headers={"X-Hub-Signature-256": signature,
                                                              "Content-Type": "application/json"})

    def say(self, text, user=USER, **kwargs):
        """Envia un mensaje, procesa la cola y devuelve las respuestas a ese usuario."""
        before = len(self.wa.sent)
        assert self.post(self.payload(text, user=user, **kwargs)).status_code == 200
        self.svc.drain()
        return "\n".join(p["text"]["body"] for p in self.wa.sent[before:] if p["to"] == user)

    def advance(self, seconds):
        self.clock.t += seconds
        self.svc.drain()

    def advisor_msgs(self):
        return [p for p in self.wa.sent if p["to"] == self.cfg.advisor_number]

    def advisor_text(self, payload):
        if payload["type"] == "template":
            return payload["template"]["components"][0]["parameters"][0]["text"]
        return payload["text"]["body"]

    def leads(self):
        return self.svc.list_leads()

    def make_lead(self, user=USER, service="banners"):
        self.say(f"Quiero {service}", user=user)
        self.say("asesor", user=user)
        self.say("sí", user=user)
        self.say("Ana Prueba", user=user)
        self.say("Tacos Ficticios", user=user)
        self.say("Culiacán", user=user)


@pytest.fixture
def world():
    return World()
