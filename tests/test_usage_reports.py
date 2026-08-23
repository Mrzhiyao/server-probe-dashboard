import unittest
from unittest import mock

from server_probe import collector
from server_probe.usage_reports import HourlyUsageReporter, current_user_usage


GIB = 1024 * 1024 * 1024


class RecordingClient:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return {"code": 0}


class UserResourceCollectorTests(unittest.TestCase):
    def test_processes_are_aggregated_for_normal_login_uids(self):
        output = "\n".join(
            [
                "0 root 1 0.0 0.1 1000 100 S",
                "999 postgres 2 5.0 1.0 4096 100 R",
                "1000 alice 123 12.5 1.0 2048 50 R",
                "1000 alice 124 2.5 2.0 1024 100 S",
                "1001 bob 200 0.0 0.5 512 20 S",
                "65534 nobody 300 8.0 1.0 4096 10 R",
            ]
        )
        with mock.patch.object(collector, "run", return_value=output), mock.patch.object(
            collector, "login_uid_range", return_value=(1000, 60000)
        ), mock.patch.object(collector, "pwd", None):
            rows = collector.user_resource_summary()
        self.assertEqual([row["user"] for row in rows], ["alice", "bob"])
        alice = rows[0]
        self.assertEqual(alice["process_count"], 2)
        self.assertEqual(alice["running_process_count"], 1)
        self.assertEqual(alice["cpu_percent_sum"], 15.0)
        self.assertEqual(alice["rss_bytes"], 3 * 1024 * 1024)
        self.assertEqual(alice["longest_runtime_seconds"], 100)


class HourlyUsageReporterTests(unittest.TestCase):
    def snapshot(self):
        return {
            "results": [
                {
                    "id": "gpu-1",
                    "name": "GPU 1",
                    "host": "192.0.2.10",
                    "group": "GPU",
                    "status": "online",
                    "metrics": {
                        "user_resources": [
                            {
                                "user": "alice",
                                "uid": 1000,
                                "cpu_percent_sum": 20,
                                "rss_bytes": 1 * GIB,
                                "process_count": 4,
                                "running_process_count": 1,
                            },
                            {
                                "user": "root",
                                "uid": 0,
                                "cpu_percent_sum": 90,
                                "rss_bytes": 8 * GIB,
                                "process_count": 8,
                            },
                        ],
                        "gpu": {
                            "user_summary": [
                                {
                                    "user": "alice",
                                    "used_memory_bytes": 2 * GIB,
                                    "gpu_sm_percent_sum": 30,
                                    "process_count": 1,
                                    "gpu_indices": ["0"],
                                },
                                {
                                    "user": "root",
                                    "used_memory_bytes": 9 * GIB,
                                    "gpu_indices": ["1"],
                                },
                            ]
                        },
                        "docker": {
                            "containers": [
                                {
                                    "running": True,
                                    "owner_user": "bob",
                                    "runtime_user": "root",
                                    "cpu_percent": 50,
                                    "memory_used_bytes": 3 * GIB,
                                    "gpu_memory_used_bytes": 4 * GIB,
                                    "gpu_process_count": 1,
                                    "gpu_indices": ["1"],
                                },
                                {
                                    "running": True,
                                    "owner_user": "alice",
                                    "runtime_user": "alice",
                                    "cpu_percent": 99,
                                    "memory_used_bytes": 7 * GIB,
                                    "gpu_memory_used_bytes": 6 * GIB,
                                },
                            ]
                        },
                    },
                },
                {"id": "offline", "name": "Offline", "status": "offline"},
            ]
        }

    def test_host_gpu_and_container_usage_are_merged_without_root(self):
        machines = current_user_usage(self.snapshot(), ["root", "nobody"])
        self.assertEqual(len(machines), 1)
        rows = {row["user"]: row for row in machines[0]["users"]}
        self.assertEqual(set(rows), {"alice", "bob"})
        self.assertEqual(rows["alice"]["memory_bytes"], 1 * GIB)
        self.assertEqual(rows["alice"]["gpu_memory_bytes"], 2 * GIB)
        self.assertEqual(rows["alice"]["container_count"], 1)
        self.assertEqual(rows["bob"]["memory_bytes"], 3 * GIB)
        self.assertEqual(rows["bob"]["gpu_memory_bytes"], 4 * GIB)
        self.assertEqual(rows["bob"]["gpu_indices"], ["1"])

    def test_report_is_sent_on_the_next_hour_boundary(self):
        now = 1000.0
        client = RecordingClient()
        reporter = HourlyUsageReporter(client, interval_seconds=3600, now_fn=lambda: now)
        self.assertFalse(reporter.process(self.snapshot()))
        self.assertEqual(client.payloads, [])
        now = 3600.0
        self.assertTrue(reporter.process(self.snapshot()))
        self.assertEqual(len(client.payloads), 1)
        payload = client.payloads[0]
        self.assertEqual(payload["card"]["header"]["title"]["content"], "每小时用户资源报告")
        rendered = str(payload)
        self.assertIn("alice", rendered)
        self.assertIn("bob", rendered)
        self.assertNotIn("`root`", rendered)
        self.assertEqual(reporter.status()["sent_reports"], 1)
        self.assertEqual(reporter.status()["snapshot_samples"], 0)


if __name__ == "__main__":
    unittest.main()
