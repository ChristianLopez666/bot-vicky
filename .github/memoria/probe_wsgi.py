"""App real bajo gunicorn con Google, Meta y Radar simulados en el transporte.

Usa las librerías reales (discovery, construcción de peticiones, lectura de
respuestas). Solo se sustituye el envío por red. Credenciales generadas al vuelo.
"""
import itertools
import json
import os
import re
import sys
import urllib.parse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.environ["APP_DIR"])

key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
os.environ.update({
    "GOOGLE_CREDENTIALS_JSON": json.dumps({
        "type": "service_account", "private_key": key, "client_email": "prueba@prueba.invalid",
        "token_uri": "https://oauth2.invalid/token", "project_id": "prueba"}),
    "SHEETS_ID_CONVERSACIONES": "hoja-ficticia", "META_TOKEN": "ficticio", "WABA_PHONE_ID": "111",
    "VERIFY_TOKEN": "ficticio", "META_APP_SECRET": "ficticio", "ADVISOR_NUMBER": "5215550000099",
    "OPENAI_API_KEY": "sk-ficticia", "ADVISOR_TEMPLATE_NAME": "plantilla_ficticia",
    "RADAR_RECORD_ENABLED": "true", "RADAR_EMIT_ENABLED": "true",
    "RADAR_EVENTS_URL": "https://radar.invalid/e", "RADAR_VICKY_TOKEN": "ficticio",
    "RADAR_VICKY_HMAC_SECRET": "ficticio",
})

import google_auth_httplib2  # noqa: E402
import httplib2  # noqa: E402
import requests  # noqa: E402

from radar_events import EVENTS_HEADER  # noqa: E402

TABS = ["CONVERSACIONES", "Conversaciones", "Reporte Leads", "EVENTOS_RADAR_REDES", "RADAR_CIRCUITO_REDES"]
REPORT = ["Telefono", "Nombre", "Ciudad", "Producto", "Pension", "Monto", "Cuota", "Plazo", "Estado",
          "Fecha inicio", "Ultima actualizacion"]
# Tamaños parecidos a la hoja real: unas 120 filas de eventos con su JSON.
EVENT_ROW = ["id", "message_sent", "2026-11-25T00:00:00Z", "vicky_redes", "111", "RS-x", "5550000001",
             "", "wamid.x", "", "", "sent", "", "FALSE", "ENVIADO", "2", "2026-11-25T00:00:00Z",
             json.dumps({"relleno": "x" * 1400})]
HEADERS = {"EVENTOS_RADAR_REDES": EVENTS_HEADER, "Reporte Leads": REPORT}


def fake_google(self, uri, method="GET", body=None, headers=None, **kwargs):
    url = urllib.parse.unquote(uri)
    payload = {}
    match = re.search(r"/values/([^?:]+)(?:!|%21)?([^?]*)", url)
    if method == "GET" and "/values/" in url:
        rng = url.split("/values/", 1)[1].split("?")[0]
        tab, _, cells = rng.partition("!")
        if re.fullmatch(r"A1:[A-Z]1", cells):
            payload = {"values": [HEADERS.get(tab, ["Phone", "Nombre", "Mensaje", "Fecha", "Tipo", "Origen",
                                  "Servicio", "Estado", "Resultado", "Error", "MsgID"])]}
        elif tab == "EVENTOS_RADAR_REDES":
            payload = {"values": [EVENT_ROW] * 120}
        elif tab == "Reporte Leads":
            payload = {"values": [REPORT] + [["52155500000%02d" % i] + ["dato"] * 10 for i in range(8)]}
        else:
            payload = {"values": []}
    elif method == "GET":
        payload = {"sheets": [{"properties": {"title": t}} for t in TABS]}
    elif ":append" in url:
        payload = {"updates": {"updatedRange": "EVENTOS_RADAR_REDES!A121:R121"}}
    assert match or True
    return httplib2.Response({"status": "200", "content-type": "application/json"}), json.dumps(payload).encode()


google_auth_httplib2.AuthorizedHttp.request = fake_google
ids = itertools.count(1)


class Reply:
    def __init__(self, status, body):
        self.status_code, self._body, self.text, self.headers = status, body, json.dumps(body), {}

    def json(self):
        return self._body


def fake_post(url, *args, **kwargs):
    if "graph.facebook.com" in url:
        print("META_POST", flush=True)
        return Reply(200, {"messages": [{"id": f"wamid.OUT{os.getpid()}.{next(ids)}"}]})
    return Reply(403, {"ok": False})          # como Radar en producción


requests.post = fake_post

import app as _core  # noqa: E402

app = _core.app
