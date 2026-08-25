import json
import tempfile
import unittest
from pathlib import Path

from server_probe.model_catalog import ModelCatalog, discover_model


class ModelCatalogTests(unittest.TestCase):
    def make_model(self, root, name, model_type="qwen2", architecture="Qwen2ForCausalLM", suffix=".safetensors"):
        path = Path(root) / name
        path.mkdir()
        (path / "config.json").write_text(
            json.dumps({"model_type": model_type, "architectures": [architecture]}),
            encoding="utf-8",
        )
        with (path / ("model" + suffix)).open("wb") as handle:
            handle.truncate(32 * 1024 * 1024)
        return path

    def test_discovers_candidate_and_applies_admin_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_model(root, "Qwen-Test")
            model = discover_model(
                path,
                "/mnt/bnu-model-nas/yaozhi/models",
                {
                    "enabled": True,
                    "verification_status": "verified",
                    "recommended_gpu_count": 2,
                    "served_model_name": "bnu/qwen-test",
                },
            )
        self.assertTrue(model["candidate"])
        self.assertTrue(model["enabled"])
        self.assertEqual(model["category"], "text")
        self.assertEqual(model["recommended_gpu_count"], 2)
        self.assertEqual(model["deployment_path"], "/mnt/bnu-model-nas/yaozhi/models/Qwen-Test")

    def test_non_chat_model_is_not_a_deployment_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_model(root, "Bert-Test", "bert", "BertForMaskedLM", ".bin")
            model = discover_model(path, "/models")
        self.assertEqual(model["category"], "embedding")
        self.assertFalse(model["candidate"])
        self.assertFalse(model["enabled"])

    def test_catalog_ignores_hidden_directories(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_model(root, "Visible")
            self.make_model(root, ".hidden")
            catalog = ModelCatalog(root, "/deploy", cache_seconds=30)
            models = catalog.scan(force=True)
        self.assertEqual([model["key"] for model in models], ["Visible"])


if __name__ == "__main__":
    unittest.main()
