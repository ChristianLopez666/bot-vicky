import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as vicky_app


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _setup_common(monkeypatch):
    monkeypatch.setattr(vicky_app, "INTERNAL_TOKEN", "test-token")
    monkeypatch.setattr(vicky_app, "notify_advisor", lambda msg: True)
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)

    captured = {}

    def fake_notify_boardroom(phone, product_code, data):
        captured["phone"] = phone
        captured["product_code"] = product_code
        captured["data"] = data

    monkeypatch.setattr(vicky_app, "_notify_boardroom_lead_qualified", fake_notify_boardroom)
    return captured


def test_ext_lead_imss_con_lead_id(monkeypatch):
    captured = _setup_common(monkeypatch)
    client = vicky_app.app.test_client()

    payload = {
        "lead_id": "lead-test-001",
        "nombre": "Cliente Prueba",
        "telefono": "6680000000",
        "interes": "prestamo_imss",
        "source": "cohifis.com.mx",
    }
    resp = client.post(
        "/ext/lead",
        json=payload,
        headers={"X-Internal-Token": "test-token"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["product_code"] == "prestamo_imss_ley73"
    assert captured["data"]["service_hint"] == "imss"


def test_ext_lead_imss_sin_lead_id(monkeypatch):
    captured = _setup_common(monkeypatch)
    monkeypatch.setattr(vicky_app.time, "time", lambda: 1780000000)
    client = vicky_app.app.test_client()

    payload = {
        "nombre": "Cliente Sin Lead ID",
        "telefono": "6680000001",
        "producto_interes": "prestamo_imss",
        "source": "cohifis.com.mx",
    }
    resp = client.post(
        "/ext/lead",
        json=payload,
        headers={"X-Internal-Token": "test-token"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lead_id"].startswith("cohifis-6680000001-")
    assert body["product_code"] == "prestamo_imss_ley73"
    assert captured["data"]["service_hint"] == "imss"
    assert captured["data"]["lead_id"].startswith("cohifis-6680000001-")
