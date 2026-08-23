import base64
import hashlib
import hmac
import json
import unittest

from server_probe.notifications import AlertNotificationManager, FeishuWebhookClient, alert_key


class RecordingClient:
    def __init__(self, error=None):
        self.error = error
        self.payloads = []

    def send(self, payload):
        if self.error:
            raise self.error
        self.payloads.append(payload)
        return {"code": 0}


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"code": 0, "msg": "success"}'


class FeishuWebhookClientTests(unittest.TestCase):
    def test_signed_request_contains_valid_signature(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        client = FeishuWebhookClient(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example",
            signing_secret="secret",
            timeout=3,
            opener=opener,
        )
        client.send({"msg_type": "text", "content": {"text": "test"}})
        timestamp = captured["body"]["timestamp"]
        expected = base64.b64encode(
            hmac.new(("%s\nsecret" % timestamp).encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("ascii")
        self.assertEqual(captured["body"]["sign"], expected)
        self.assertEqual(captured["timeout"], 3)


class AlertNotificationManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.client = RecordingClient()
        self.manager = AlertNotificationManager(
            self.client,
            critical_consecutive=2,
            warning_after_seconds=300,
            cooldown_seconds=1800,
            now_fn=lambda: self.now,
        )

    def alert(self, severity="critical", kind="offline", **details):
        alert = {
            "server_id": "gpu-1",
            "server_name": "GPU 1",
            "host": "192.0.2.10",
            "group": "GPU",
            "severity": severity,
            "kind": kind,
        }
        alert.update(details)
        return alert

    def test_critical_alert_requires_two_consecutive_samples(self):
        alert = self.alert()
        self.assertEqual(self.manager.process([alert], {"gpu-1": "offline"}), [])
        self.now += 60
        events = self.manager.process([alert], {"gpu-1": "offline"})
        self.assertEqual(events[0]["action"], "triggered")
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(self.client.payloads[0]["card"]["header"]["template"], "red")

    def test_warning_waits_five_minutes(self):
        alert = self.alert("warning", "cpu", metric="CPU", value=90, threshold=85)
        self.manager.process([alert], {"gpu-1": "online"})
        self.now += 299
        self.manager.process([alert], {"gpu-1": "online"})
        self.assertEqual(self.client.payloads, [])
        self.now += 1
        self.manager.process([alert], {"gpu-1": "online"})
        self.assertEqual(len(self.client.payloads), 1)

    def test_cooldown_and_recovery(self):
        alert = self.alert()
        self.manager.process([alert], {"gpu-1": "offline"})
        self.now += 60
        self.manager.process([alert], {"gpu-1": "offline"})
        self.now += 1799
        self.manager.process([alert], {"gpu-1": "offline"})
        self.assertEqual(len(self.client.payloads), 1)
        self.now += 1
        events = self.manager.process([alert], {"gpu-1": "offline"})
        self.assertEqual(events[0]["action"], "reminder")
        self.assertEqual(len(self.client.payloads), 2)
        self.now += 60
        events = self.manager.process([], {"gpu-1": "online"})
        self.assertEqual(events[0]["action"], "recovered")
        self.assertEqual(self.client.payloads[-1]["card"]["header"]["template"], "green")

    def test_offline_server_does_not_resolve_existing_resource_alert(self):
        alert = self.alert("warning", "memory", metric="Memory", value=90, threshold=88)
        self.manager.process([alert], {"gpu-1": "online"})
        self.now += 300
        self.manager.process([alert], {"gpu-1": "online"})
        self.assertTrue(self.manager.states[alert_key(alert)]["notified"])
        self.now += 60
        self.manager.process([self.alert()], {"gpu-1": "offline"})
        self.assertIn(alert_key(alert), self.manager.states)
        self.assertEqual(len(self.client.payloads), 1)

    def test_failed_delivery_is_retried_without_losing_state(self):
        self.client.error = RuntimeError("temporary failure")
        alert = self.alert()
        self.manager.process([alert], {"gpu-1": "offline"})
        self.now += 60
        self.manager.process([alert], {"gpu-1": "offline"})
        self.assertFalse(self.manager.states[alert_key(alert)]["notified"])
        self.client.error = None
        self.now += 60
        self.manager.process([alert], {"gpu-1": "offline"})
        self.assertEqual(len(self.client.payloads), 1)
        self.assertIsNone(self.manager.status()["last_error"])


if __name__ == "__main__":
    unittest.main()
