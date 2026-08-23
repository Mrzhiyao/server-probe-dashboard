import unittest
from unittest import mock

from server_probe import collector
from server_probe.app import Monitor


class DockerParserTests(unittest.TestCase):
    def test_size_bytes_supports_decimal_and_binary_units(self):
        self.assertEqual(collector.size_bytes("19.5GB"), 19_500_000_000)
        self.assertEqual(collector.size_bytes("1.5GiB"), 1_610_612_736)
        self.assertEqual(collector.size_bytes("0B"), 0)

    def test_vllm_command_is_parsed_without_exposing_full_command(self):
        details = collector.vllm_details(
            "nvcr.io/nvidia/vllm:26.04-py3",
            '"/usr/local/bin/vllm serve /models/Qwen3-VL-8B-Instruct --served-model-name Qwen3-VL-8B-Instruct '
            '--port 18223 --max-model-len 32768 --gpu-memory-utilization 0.55"',
        )
        self.assertTrue(details["service"])
        self.assertEqual(details["model"], "Qwen3-VL-8B-Instruct")
        self.assertEqual(details["port"], 18223)
        self.assertEqual(details["max_model_len"], "32768")
        self.assertNotIn("command", details)

    def test_image_only_workload_is_not_a_service(self):
        details = collector.vllm_details("nvcr.io/nvidia/vllm:26.04-py3", "/bin/bash /work/quantize.py")
        self.assertEqual(details, {"service": False})

    def test_endpoint_candidates_prefer_published_local_port(self):
        container = {
            "vllm": {"port": 8000},
            "_inspect": {
                "network_mode": "bridge",
                "ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18223"}]},
                "networks": {"bridge": {"IPAddress": "172.17.0.2"}},
            },
        }
        self.assertEqual(collector.endpoint_candidates(container)[0], ("127.0.0.1", 18223))

    def test_probe_accepts_authenticated_models_endpoint(self):
        container = {
            "vllm": {"port": 8000},
            "_inspect": {"network_mode": "host", "ports": {}, "networks": {}},
        }
        responses = [(200, b"", 2.0), (401, b"", 3.0)]
        with mock.patch.object(collector, "local_http_request", side_effect=responses):
            probe = collector.probe_vllm(container)
        self.assertEqual(probe["status"], "healthy")
        self.assertEqual(probe["endpoint"], "127.0.0.1:8000")

    def test_gpu_process_is_mapped_to_container(self):
        container_id = "a" * 64
        gpu = {"processes": [{"pid": 123, "gpu_index": "2", "used_memory_bytes": 4096}]}
        containers = [{"_full_id": container_id, "name": "model-server"}]
        with mock.patch.object(collector, "read_first", return_value="0::/docker/%s" % container_id):
            collector.attach_gpu_containers(gpu, containers)
        self.assertEqual(gpu["processes"][0]["container_name"], "model-server")
        self.assertEqual(containers[0]["gpu_indices"], ["2"])
        self.assertEqual(containers[0]["gpu_memory_used_bytes"], 4096)

    def test_container_owner_prefers_explicit_label(self):
        owner = collector.infer_container_owner(
            {
                "labels": {"server-probe.owner": "alice", "com.docker.compose.project.working_dir": "/home/bob/app"},
                "mounts": [],
            }
        )
        self.assertEqual(owner["owner_user"], "alice")
        self.assertEqual(owner["owner_confidence"], "exact")

    def test_container_owner_can_be_inferred_from_home_mount(self):
        with mock.patch.object(collector, "existing_owner_name", side_effect=lambda value: value):
            owner = collector.infer_container_owner(
                {"labels": {}, "mounts": [{"Source": "/home/carol/models"}], "config_user": "1000"}
            )
        self.assertEqual(owner["owner_user"], "carol")
        self.assertEqual(owner["owner_source"], "home_mount")

    def test_unknown_creator_keeps_runtime_user_separate(self):
        owner = collector.infer_container_owner({"labels": {}, "mounts": [], "config_user": "1001"})
        self.assertIsNone(owner["owner_user"])
        self.assertEqual(owner["runtime_user"], "1001")


class DockerAlertTests(unittest.TestCase):
    def monitor(self):
        monitor = Monitor.__new__(Monitor)
        monitor.alert_thresholds = {
            "cpu_warn_percent": 85,
            "cpu_critical_percent": 95,
            "memory_warn_percent": 88,
            "memory_critical_percent": 95,
            "gpu_warn_percent": 92,
            "gpu_critical_percent": 98,
            "disk_warn_percent": 90,
            "disk_critical_percent": 95,
            "inode_warn_percent": 90,
            "inode_critical_percent": 98,
            "mount_latency_warn_ms": 2000,
            "mount_latency_critical_ms": 5000,
        }
        return monitor

    def result(self, containers):
        return {
            "id": "gpu-host",
            "name": "gpu-host",
            "host": "192.0.2.10",
            "status": "online",
            "metrics": {
                "cpu": {"percent": 1},
                "memory": {"percent": 2},
                "disk": {"percent": 3},
                "gpu": {"devices": []},
                "storage": {"mounts": [], "devices": []},
                "docker": {"available": True, "accessible": True, "containers": containers},
            },
        }

    def test_unhealthy_vllm_endpoint_is_critical(self):
        result = self.result(
            [
                {
                    "name": "model-api",
                    "running": True,
                    "state": "running",
                    "vllm": {"service": True, "model": "Qwen", "probe": {"status": "unhealthy"}},
                }
            ]
        )
        alerts = self.monitor().alerts_for_result(result)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "vllm")
        self.assertEqual(alerts[0]["severity"], "critical")

    def test_expected_container_is_synthesized_and_alerted(self):
        monitor = self.monitor()
        metrics = {"docker": {"available": True, "accessible": True, "containers": [], "summary": {}}}
        monitor.apply_container_expectations({"expected_containers": ["model-api"]}, metrics)
        result = self.result(metrics["docker"]["containers"])
        alerts = monitor.alerts_for_result(result)
        self.assertEqual(metrics["docker"]["summary"]["expected_issue_count"], 1)
        self.assertEqual(alerts[0]["kind"], "container_expected")


if __name__ == "__main__":
    unittest.main()
