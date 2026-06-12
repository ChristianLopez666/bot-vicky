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


def test_handle_message_instruction(monkeypatch):
    monkeypatch.setattr(vicky_app, "INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("INTERNAL_TOKEN", "test-token")
    monkeypatch.setattr(vicky_app.threading, "Thread", ImmediateThread)

    fake_data = {}
    monkeypatch.setattr(vicky_app, "user_data", fake_data)

    captured = {}

    def fake_handle(msg_obj):
        captured["msg_obj"] = msg_obj

    monkeypatch.setattr(vicky_app, "handle", fake_handle)

    client = vicky_app.app.test_client()

    payload = {
        "phone": "6681234567",
        "instruction": "handle_message",
        "payload": {
            "text": "Hola quiero información del préstamo IMSS",
            "nombre": "Juan Prueba",
            "mtype": "text",
            "media_id": "",
        },
    }
    resp = client.post(
        "/ext/boardroom/instruct",
        json=payload,
        headers={"X-Internal-Token": "test-token"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["instruction"] == "handle_message"

    msg = captured["msg_obj"]
    assert msg["from"] == "6681234567"
    assert msg["type"] == "text"
    assert msg["text"]["body"] == "Hola quiero información del préstamo IMSS"

    assert fake_data["6681234567"]["nombre"] == "Juan Prueba"
