import json
import unittest
import uuid
from unittest.mock import Mock

from radar_bridge import RadarBridge, lead_id_for
from radar_events import EventLog, SOURCE
from radar_outbox import correlated_event, correlation_index


class RadarBridgeTests(unittest.TestCase):
    def bridge(self):
        bridge = RadarBridge(phone_id="123", advisor="5216682478005", credentials="", sheet_id="", record_enabled=True)
        self.rows = []
        bridge.log = EventLog(lambda tab, row: (self.rows.append(row), len(self.rows)+1)[1])
        bridge.start = Mock()
        return bridge

    def test_lead_identity_is_stable_across_phone_formats_and_restart(self):
        expected = lead_id_for("6680000000")
        self.assertEqual(expected, lead_id_for("+52 668 000 0000"))
        self.assertEqual(expected, lead_id_for("5216680000000"))
        self.assertEqual(uuid.UUID(expected[3:]).version, 5)
        with self.assertRaises(ValueError):
            lead_id_for("123")

    def test_inbound_duplicate_records_once_and_never_uses_secom_identity(self):
        b = self.bridge()
        value = {"metadata": {"phone_number_id": "123"}, "messages": [{"from": "5216680000000", "id": "wamid.in", "timestamp": "1789084800", "text": {"body": "Hola"}}]}
        b.webhook(value); b.webhook(value)
        self.assertEqual(len(self.rows), 1)
        e = json.loads(self.rows[0][17])
        self.assertEqual(e["source"], "vicky_redes")
        self.assertEqual(e["lead"]["lead_id"], lead_id_for("6680000000"))
        self.assertEqual(e["event_type"], "message_inbound")

    def test_wrong_channel_and_advisor_are_excluded(self):
        b = self.bridge()
        b.webhook({"metadata": {"phone_number_id": "different"}, "messages": [{"from": "6680000000", "id": "wamid.in"}]})
        b.record("message_inbound", "6682478005", wamid="wamid.advisor", direction="inbound")
        self.assertEqual(self.rows, [])

    def test_request_sent_and_read_share_original_request_id(self):
        b = self.bridge(); request_id = str(uuid.uuid4()); payload = {"to": "6680000000", "type": "text"}
        b.requested(payload, request_id)
        response = Mock(status_code=200); response.json.return_value = {"messages": [{"id": "wamid.out"}]}
        b.response(payload, request_id, response)
        b.webhook({"metadata": {"phone_number_id": "123"}, "statuses": [{"recipient_id": "5216680000000", "id": "wamid.out", "status": "read", "timestamp": "1789084800"}]})
        self.assertEqual([r[1] for r in self.rows], ["message_requested", "message_sent", "message_read"])
        reading = correlated_event(json.loads(self.rows[2][17]), correlation_index(self.rows))
        self.assertEqual(reading["message"]["request_id"], request_id)

    def test_meta_rejection_is_separate_from_delivery_and_disabled_is_inert(self):
        b = self.bridge(); response = Mock(status_code=400)
        response.json.return_value = {"error": {"code": 131026, "message": "Undeliverable"}}
        b.response({"to": "6680000000"}, str(uuid.uuid4()), response)
        self.assertEqual(self.rows[0][1], "message_failed")
        b.record_enabled = False
        b.requested({"to": "6680000000"}, str(uuid.uuid4()))
        self.assertEqual(len(self.rows), 1)

    def test_storage_failure_does_not_interrupt_the_bot(self):
        b = self.bridge(); b.log.record = Mock(side_effect=OSError("Sheets unavailable"))
        b.requested({"to": "6680000000"}, str(uuid.uuid4()))
        b.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
