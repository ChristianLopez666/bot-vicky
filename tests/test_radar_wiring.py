import uuid
from unittest.mock import Mock, patch
import app as vicky


def test_wa_instrumentation_preserves_payload_response_and_request_identity():
    payload = {"to": "6680000000", "type": "text", "text": {"body": "Hola"}}
    response = Mock(status_code=200)
    order = []
    with patch.object(vicky._radar, "requested", side_effect=lambda *a: order.append("record")) as requested, \
         patch.object(vicky.requests, "post", side_effect=lambda *a, **k: (order.append("send"), response)[1]) as post, \
         patch.object(vicky._radar, "response") as observe:
        assert vicky._wa_post(payload) is response
    assert order == ["record", "send"]
    assert post.call_args.kwargs["json"] is payload
    request_id = requested.call_args.args[1]
    assert uuid.UUID(request_id).version == 4
    assert observe.call_args.args == (payload, request_id, response)


def test_radar_observes_only_webhooks_of_its_channel():
    def value(phone_id):
        return {"metadata": {"phone_number_id": phone_id}, "messages": [{"id": "wamid.test", "from": "6680000000"}]}
    body = {"entry": [{"changes": [{"value": value("123")}, {"value": value("other")}]}]}
    with patch.object(vicky, "WABA_ID", "123"), patch.object(vicky, "_verify_sig", return_value=True), \
         patch.object(vicky._radar, "webhook") as observe, patch.object(vicky._radar, "start"), \
         patch.object(vicky, "handle") as handle, patch.object(vicky, "_handle_statuses"):
        response = vicky.app.test_client().post("/webhook", json=body)
    assert response.status_code == 200
    assert observe.call_count == 1
    assert observe.call_args.args[0]["metadata"]["phone_number_id"] == "123"
    assert handle.call_count == 1
