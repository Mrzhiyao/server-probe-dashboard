import unittest

from server_probe.model_services import ModelServiceManager, safe_slug


GIB = 1024**3


class FakeMonitor:
    def __init__(self, result):
        self.result = result

    def cached_snapshot(self, trigger=False):
        return {"results": [self.result]}

    def request_machine_label(self, server_id):
        return server_id


class FakeCatalog:
    def scan(self, settings):
        return [
            {
                "key": "Qwen-Test",
                "name": "Qwen Test",
                "served_model_name": "Qwen-Test",
                "enabled": True,
                "candidate": True,
                "recommended_gpu_count": 1,
                "weight_bytes": 4 * GIB,
            }
        ]


class FakeStore:
    def __init__(self, services=None):
        self.services = list(services or [])

    def model_catalog_settings(self):
        return {}

    def list_model_services(self, user=None):
        return list(self.services)


def worker_result():
    return {
        "id": "worker-1",
        "status": "online",
        "metrics": {
            "gpu": {
                "devices": [
                    {
                        "index": "0",
                        "memory_total_bytes": 80 * GIB,
                        "memory_used_bytes": 10 * 1024**2,
                        "memory_percent": 0.1,
                    },
                    {
                        "index": "1",
                        "memory_total_bytes": 80 * GIB,
                        "memory_used_bytes": 30 * GIB,
                        "memory_percent": 37.5,
                    },
                ]
            },
            "docker": {"containers": []},
        },
    }


class ModelServiceManagerTests(unittest.TestCase):
    def test_slug_is_safe_for_container_names(self):
        self.assertEqual(safe_slug("Qwen2.5 Coder / 3B"), "qwen2-5-coder-3b")

    def test_scheduler_uses_allowlisted_idle_gpu(self):
        manager = ModelServiceManager(
            FakeMonitor(worker_result()),
            FakeCatalog(),
            FakeStore(),
            {
                "enabled": True,
                "workers": [
                    {
                        "server_id": "worker-1",
                        "allowed_gpu_indices": [0, 1],
                        "max_gpu_memory_percent": 10,
                    }
                ],
            },
        )
        worker, indices = manager.select_worker(manager.model_by_key("Qwen-Test"), 1)
        self.assertEqual(worker["server_id"], "worker-1")
        self.assertEqual(indices, ["0"])

    def test_scheduler_excludes_gpu_owned_by_managed_service(self):
        store = FakeStore(
            [
                {
                    "worker_id": "worker-1",
                    "status": "running",
                    "gpu_indices": ["0"],
                }
            ]
        )
        manager = ModelServiceManager(
            FakeMonitor(worker_result()),
            FakeCatalog(),
            store,
            {
                "enabled": True,
                "workers": [
                    {
                        "server_id": "worker-1",
                        "allowed_gpu_indices": [0, 1],
                        "max_gpu_memory_percent": 50,
                    }
                ],
            },
        )
        _, indices = manager.select_worker(manager.model_by_key("Qwen-Test"), 1)
        self.assertEqual(indices, ["1"])


if __name__ == "__main__":
    unittest.main()
