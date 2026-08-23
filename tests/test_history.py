import threading
import unittest
from datetime import datetime, timezone

from server_probe.app import Monitor
from server_probe.history import HistoryStore, bucket_seconds, parse_time


class FakeHistoryStore:
    retention_days = 30

    def __init__(self, restored=None):
        self.restored = restored or {}
        self.appended = []
        self.cleanup_calls = 0

    def load_recent(self, server_ids, limit_per_server=240):
        self.loaded = (server_ids, limit_per_server)
        return self.restored

    def append_samples(self, samples):
        self.appended.extend(samples)
        return len(samples)

    def cleanup(self):
        self.cleanup_calls += 1
        return 0


class HistoryHelpersTests(unittest.TestCase):
    def test_bucket_seconds(self):
        self.assertEqual(bucket_seconds(24, 240), 360)
        self.assertEqual(bucket_seconds(168, 240), 2520)
        self.assertEqual(bucket_seconds(1, 720), 60)

    def test_parse_time_adds_timezone(self):
        parsed = parse_time("2026-08-23T12:30:00")
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_rows_are_grouped_by_server(self):
        store = HistoryStore("unused")
        rows = [
            (
                "server-a",
                datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
                "online",
                1.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                0.1,
                {"mount_issue_count": 0},
            )
        ]
        history = store.rows_by_server(rows)
        self.assertEqual(history["server-a"][0]["gpu_peak"], 5.0)
        self.assertEqual(history["server-a"][0]["storage"]["mount_issue_count"], 0)


class MonitorHistoryTests(unittest.TestCase):
    def monitor(self, store):
        monitor = Monitor.__new__(Monitor)
        monitor.servers = [{"id": "server-a"}]
        monitor.history_retention_points = 2
        monitor.history = {}
        monitor.history_lock = threading.Lock()
        monitor.history_store = store
        monitor.history_store_error = None
        monitor.history_cleanup_interval = 0
        monitor.last_history_cleanup_at = 0
        return monitor

    def result(self, cpu):
        return {
            "id": "server-a",
            "status": "online",
            "collected_at": "2026-08-23T12:00:00+00:00",
            "metrics": {
                "cpu": {"percent": cpu, "load1": 0.1},
                "memory": {"percent": 20},
                "disk": {"percent": 30},
                "gpu": {"devices": []},
                "storage": {
                    "summary": {"mount_issue_count": 1, "smart_issue_count": 0},
                    "mounts": [{"mount": "/nas", "status": "automount_only", "percent": None}],
                },
            },
        }

    def test_restore_and_persist_samples(self):
        restored = {"server-a": [{"time": "old", "status": "online", "cpu": 5}]}
        store = FakeHistoryStore(restored)
        monitor = self.monitor(store)
        monitor.restore_persistent_history()
        self.assertEqual(monitor.history, restored)
        monitor.record_history({"results": [self.result(10)]})
        self.assertEqual(len(store.appended), 1)
        self.assertEqual(store.appended[0][1]["storage"]["mount_issue_count"], 1)
        self.assertEqual(store.cleanup_calls, 1)

    def test_memory_history_keeps_configured_limit(self):
        store = FakeHistoryStore()
        monitor = self.monitor(store)
        for cpu in (1, 2, 3):
            monitor.record_history({"results": [self.result(cpu)]})
        self.assertEqual([sample["cpu"] for sample in monitor.history["server-a"]], [2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
