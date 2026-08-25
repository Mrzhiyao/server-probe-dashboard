"""Read-only model catalog discovery for locally mounted model weights."""

import json
import math
import os
import threading
import time
from pathlib import Path, PurePosixPath


WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".gguf")
TEXT_MODEL_TYPES = {"llama", "qwen2", "qwen3", "qwen3_5", "qwen3_5_moe"}
VISION_MODEL_TYPES = {
    "deepseek_vl_v2",
    "gemma3",
    "gemma4",
    "gemma4_unified",
    "internvl",
    "mllama",
    "multi_modality",
    "qwen3_vl",
}


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def model_category(model_type, architectures):
    value = str(model_type or "").lower()
    names = " ".join(str(item) for item in (architectures or [])).lower()
    if value in TEXT_MODEL_TYPES or ("causallm" in names and "conditionalgeneration" not in names):
        return "text"
    if value in VISION_MODEL_TYPES or "conditionalgeneration" in names:
        return "vision"
    if value in ("bert", "clip"):
        return "embedding"
    if value in ("wav2vec2",):
        return "audio"
    if not value and "gguf" in names:
        return "gguf"
    return "other"


def weight_files(root):
    found = []

    def scan(path, depth):
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file(follow_symlinks=True) and entry.name.lower().endswith(WEIGHT_SUFFIXES):
                    found.append((entry.path, entry.stat(follow_symlinks=True).st_size))
                elif depth > 0 and entry.is_dir(follow_symlinks=False) and entry.name in ("model", "original", "weights"):
                    scan(entry.path, depth - 1)
            except OSError:
                continue

    scan(root, 1)
    return found


def default_gpu_count(weight_gib, category):
    if category not in ("text", "vision") or weight_gib <= 0:
        return 0
    estimated = weight_gib * 1.20 + 2.0
    return max(1, min(8, int(math.ceil(estimated / 20.0))))


def discover_model(path, deployment_root, override=None):
    override = dict(override or {})
    config = {}
    try:
        config_path = path / "config.json"
        if config_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("config.json is too large")
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        config = {}
    files = weight_files(path)
    weight_bytes = sum(size for _, size in files)
    weight_gib = round(weight_bytes / float(1024**3), 2)
    architectures = config.get("architectures") if isinstance(config.get("architectures"), list) else []
    model_type = str(config.get("model_type") or "") or None
    if not model_type and files and all(name.lower().endswith(".gguf") for name, _ in files):
        category = "gguf"
    else:
        category = model_category(model_type, architectures)
    quantization = config.get("quantization_config") or {}
    quant_method = quantization.get("quant_method") if isinstance(quantization, dict) else None
    suggested_gpus = default_gpu_count(weight_gib, category)
    recommended_gpus = max(0, min(16, as_int(override.get("recommended_gpu_count"), suggested_gpus)))
    verification_status = str(override.get("verification_status") or "untested")
    if verification_status not in ("untested", "verified", "blocked"):
        verification_status = "untested"
    enabled = bool(override.get("enabled")) and verification_status != "blocked"
    name = path.name
    return {
        "key": name,
        "name": name,
        "served_model_name": str(override.get("served_model_name") or ("bnu/" + name))[:200],
        "category": category,
        "model_type": model_type,
        "architectures": [str(item)[:160] for item in architectures[:8]],
        "quantization": str(quant_method or "") or None,
        "weight_files": len(files),
        "weight_bytes": weight_bytes,
        "weight_gib": weight_gib,
        "formats": sorted({Path(name).suffix.lower() for name, _ in files}),
        "config_ok": bool(config),
        "candidate": bool(config and files and category in ("text", "vision")),
        "suggested_gpu_count": suggested_gpus,
        "recommended_gpu_count": recommended_gpus,
        "verification_status": verification_status,
        "enabled": enabled,
        "notes": str(override.get("notes") or "")[:1000],
        "deployment_path": str(PurePosixPath(deployment_root) / name),
        "updated_at": override.get("updated_at"),
    }


class ModelCatalog:
    def __init__(self, source_root, deployment_root, cache_seconds=300):
        self.source_root = Path(source_root)
        self.deployment_root = str(deployment_root)
        self.cache_seconds = max(30.0, float(cache_seconds))
        self.lock = threading.Lock()
        self.cached_at = 0.0
        self.cached_models = []
        self.last_error = None

    def scan(self, overrides=None, force=False):
        now = time.time()
        with self.lock:
            if not force and self.cached_models and now - self.cached_at < self.cache_seconds:
                return [dict(item) for item in self.cached_models]
            try:
                entries = sorted(
                    [path for path in self.source_root.iterdir() if path.is_dir() and not path.name.startswith(".")],
                    key=lambda path: path.name.casefold(),
                )
                settings = overrides or {}
                models = [discover_model(path, self.deployment_root, settings.get(path.name)) for path in entries]
                self.cached_models = models
                self.cached_at = now
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)[:300]
                if not self.cached_models:
                    raise RuntimeError("model catalog unavailable") from None
            return [dict(item) for item in self.cached_models]

    def status(self):
        with self.lock:
            return {
                "source_available": self.source_root.is_dir(),
                "source_root": str(self.source_root),
                "deployment_root": self.deployment_root,
                "cached_at": self.cached_at or None,
                "last_error": self.last_error,
                "model_count": len(self.cached_models),
            }
