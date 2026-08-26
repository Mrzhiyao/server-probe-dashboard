"""Managed vLLM services and One API access allocations."""

import json
import math
import os
import re
import secrets
import string
import threading
from datetime import timedelta
from pathlib import Path, PurePosixPath

from server_probe.auth import utc_now


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


REMOTE_MODEL_SERVICE = r'''
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import string
import subprocess
import time
import urllib.request


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def run(command, check=True, timeout=120):
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed").strip())
    return completed


def require_text(value, name, limit=300):
    result = str(value or "").strip()
    if not result or len(result) > limit or "\n" in result or "\r" in result:
        raise RuntimeError("invalid %s" % name)
    return result


def external_token(stored, prefix):
    return stored if not prefix or stored.startswith(prefix) else prefix + stored


payload = json.loads(__PAYLOAD_JSON__)
action = str(payload.get("action") or "")
if os.geteuid() != 0:
    raise SystemExit("root permission is required")

if action == "deploy":
    container = require_text(payload.get("container_name"), "container name", 128)
    if not NAME_RE.match(container):
        raise RuntimeError("invalid container name")
    image = require_text(payload.get("image"), "image", 400)
    model_root = os.path.realpath(require_text(payload.get("model_root"), "model root", 800))
    model_path = os.path.realpath(require_text(payload.get("model_path"), "model path", 800))
    if os.path.commonpath([model_root, model_path]) != model_root or not os.path.isdir(model_path):
        raise RuntimeError("model path is unavailable")
    served_name = require_text(payload.get("internal_served_name"), "served model name", 200)
    upstream_key = require_text(payload.get("upstream_api_key"), "upstream key", 300)
    gpus = [str(value) for value in (payload.get("gpu_indices") or [])]
    if not gpus or any(not value.isdigit() for value in gpus):
        raise RuntimeError("invalid GPU selection")
    host_port = int(payload.get("host_port") or 0)
    if host_port < 1024 or host_port > 65535:
        raise RuntimeError("invalid service port")
    existing = run(["docker", "inspect", container], check=False, timeout=30)
    if existing.returncode == 0:
        raise RuntimeError("container already exists")
    run(["docker", "image", "inspect", image], timeout=60)
    available = run(
        ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
        timeout=30,
    ).stdout.splitlines()
    available = {value.strip() for value in available}
    if any(value not in available for value in gpus):
        raise RuntimeError("selected GPU is unavailable")
    bind_host = str(payload.get("bind_host") or "127.0.0.1")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((bind_host, host_port))
    finally:
        probe.close()

    command = [
        "docker", "run", "-d", "--name", container,
        "--restart", "unless-stopped",
        "--label", "probe.managed=true",
        "--label", "probe.model_key=%s" % require_text(payload.get("model_key"), "model key", 240),
        "--gpus", "device=%s" % ",".join(gpus),
        "-p", "%s:%d:8000" % (bind_host, host_port),
        "-v", "%s:/model:ro" % model_path,
        image,
        "python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", "/model",
        "--served-model-name", served_name,
        "--host", "0.0.0.0",
        "--port", "8000",
        "--api-key", upstream_key,
        "--gpu-memory-utilization", str(payload.get("gpu_memory_utilization") or "0.85"),
        "--max-model-len", str(int(payload.get("max_model_len") or 8192)),
        "--disable-log-requests",
    ]
    if len(gpus) > 1:
        command.extend(["--tensor-parallel-size", str(len(gpus))])
    if payload.get("trust_remote_code"):
        command.append("--trust-remote-code")
    try:
        container_id = run(command, timeout=180).stdout.strip()
        deadline = time.time() + max(30, min(int(payload.get("startup_timeout_seconds") or 600), 1200))
        health_url = "http://127.0.0.1:%d/health" % host_port
        last_state = "starting"
        while time.time() < deadline:
            state = run(["docker", "inspect", container, "--format", "{{.State.Status}}"], check=False, timeout=30)
            last_state = state.stdout.strip() or state.stderr.strip() or "unknown"
            if state.returncode != 0 or last_state in ("exited", "dead"):
                break
            try:
                with urllib.request.urlopen(health_url, timeout=3) as response:
                    if response.status == 200:
                        print(json.dumps({
                            "ok": True,
                            "container_id": container_id[:12],
                            "container_name": container,
                            "host_port": host_port,
                            "gpu_indices": gpus,
                            "health": "healthy",
                        }))
                        raise SystemExit(0)
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError("model service did not become healthy; state=%s" % last_state)
    except Exception:
        run(["docker", "rm", "-f", container], check=False, timeout=60)
        raise

elif action in ("oneapi_channel", "oneapi_token", "oneapi_token_get"):
    db_path = os.path.realpath(require_text(payload.get("database_path"), "One API database path", 800))
    if not os.path.isfile(db_path):
        raise RuntimeError("One API database is unavailable")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        if action == "oneapi_channel":
            backup_dir = os.path.join(os.path.dirname(db_path), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, "pre-model-channel-%d.db" % int(time.time()))
            backup = sqlite3.connect(backup_path)
            try:
                conn.backup(backup)
            finally:
                backup.close()
            channel_name = require_text(payload.get("channel_name"), "channel name", 200)
            public_model = require_text(payload.get("public_model"), "public model", 200)
            internal_model = require_text(payload.get("internal_model"), "internal model", 200)
            base_url = require_text(payload.get("base_url"), "channel base URL", 500)
            upstream_key = require_text(payload.get("upstream_api_key"), "upstream key", 300)
            mapping = json.dumps({public_model: internal_model}, ensure_ascii=False)
            existing = conn.execute("SELECT id FROM channels WHERE name = ?", (channel_name,)).fetchone()
            if existing:
                channel_id = int(existing[0])
                conn.execute(
                    "UPDATE channels SET status=1, key=?, base_url=?, models=?, model_mapping=?, test_time=? WHERE id=?",
                    (upstream_key, base_url, public_model, mapping, int(time.time()), channel_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO channels (
                      type, key, status, name, weight, created_time, test_time, response_time,
                      base_url, models, `group`, used_quota, model_mapping, priority
                    ) VALUES (1, ?, 1, ?, 0, ?, ?, 0, ?, ?, 'default', 0, ?, 0)
                    """,
                    (upstream_key, channel_name, int(time.time()), int(time.time()), base_url, public_model, mapping),
                )
                channel_id = int(cursor.lastrowid)
            conn.commit()
            print(json.dumps({"ok": True, "channel_id": channel_id}))
        elif action == "oneapi_token":
            user_id = int(payload.get("user_id") or 1)
            if not conn.execute("SELECT 1 FROM users WHERE id = ? AND status = 1", (user_id,)).fetchone():
                raise RuntimeError("One API user is unavailable")
            token_name = require_text(payload.get("token_name"), "token name", 200)
            public_model = require_text(payload.get("public_model"), "public model", 200)
            alphabet = string.ascii_letters + string.digits
            stored_key = "".join(secrets.choice(alphabet) for _ in range(48))
            now = int(time.time())
            expired_time = int(payload.get("expired_time") or -1)
            quota = max(1, int(payload.get("quota") or 5000000))
            cursor = conn.execute(
                """
                INSERT INTO tokens (
                  user_id, key, status, name, created_time, accessed_time, expired_time,
                  remain_quota, unlimited_quota, used_quota, models, subnet
                ) VALUES (?, ?, 1, ?, ?, 0, ?, ?, 0, 0, ?, '')
                """,
                (user_id, stored_key, token_name, now, expired_time, quota, public_model),
            )
            conn.commit()
            print(json.dumps({
                "ok": True,
                "token_id": int(cursor.lastrowid),
                "api_key": external_token(stored_key, str(payload.get("token_prefix") or "sk-")),
            }))
        else:
            token_id = int(payload.get("token_id") or 0)
            row = conn.execute("SELECT key,status FROM tokens WHERE id = ?", (token_id,)).fetchone()
            if not row or int(row[1]) != 1:
                raise RuntimeError("One API token is unavailable")
            print(json.dumps({
                "ok": True,
                "token_id": token_id,
                "api_key": external_token(str(row[0]), str(payload.get("token_prefix") or "sk-")),
            }))
    finally:
        conn.close()
else:
    raise RuntimeError("unsupported model service action")
'''


def load_model_service_config(path):
    if not path:
        return {"enabled": False, "workers": [], "seed_services": [], "oneapi": {}}
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    config["workers"] = config.get("workers") if isinstance(config.get("workers"), list) else []
    config["seed_services"] = config.get("seed_services") if isinstance(config.get("seed_services"), list) else []
    config["oneapi"] = config.get("oneapi") if isinstance(config.get("oneapi"), dict) else {}
    return config


def safe_slug(value, fallback="model"):
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (slug or fallback)[:42]


class ModelServiceManager:
    def __init__(self, monitor, catalog, auth_store, config=None):
        self.monitor = monitor
        self.catalog = catalog
        self.store = auth_store
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled") and monitor and catalog and auth_store)
        self.workers = {
            str(item.get("server_id") or "").strip(): dict(item)
            for item in self.config.get("workers", [])
            if isinstance(item, dict) and item.get("server_id")
        }
        self.oneapi = dict(self.config.get("oneapi") or {})
        self.lock = threading.Lock()

    def info(self):
        return {
            "enabled": self.enabled,
            "worker_count": len(self.workers),
            "api_base_url": self.oneapi.get("public_base_url") if self.enabled else None,
        }

    def catalog_models(self):
        settings = self.store.model_catalog_settings()
        return [model for model in self.catalog.scan(settings) if model.get("enabled") and model.get("candidate")]

    def model_by_key(self, model_key):
        key = str(model_key or "").strip()
        model = next((item for item in self.catalog_models() if item.get("key") == key), None)
        if not model:
            raise ValueError("model is not enabled for deployment")
        return model

    def seed_configured_services(self, created_by=None):
        if not self.enabled:
            return []
        seeded = []
        for item in self.config.get("seed_services", []):
            if not isinstance(item, dict):
                continue
            worker = self.workers.get(str(item.get("worker_id") or "")) or {}
            payload = dict(item)
            payload.setdefault("worker_name", worker.get("name") or item.get("worker_id"))
            payload.setdefault("created_by", created_by)
            seeded.append(self.store.seed_model_service(payload))
        return seeded

    def snapshot_result(self, worker_id):
        snapshot = self.monitor.cached_snapshot(trigger=False) or {}
        return next((item for item in snapshot.get("results", []) if item.get("id") == worker_id), None)

    def runtime_for_service(self, service):
        result = self.snapshot_result(service.get("worker_id"))
        if not result or result.get("status") != "online":
            if service.get("status") == "deploying":
                return {"status": "deploying", "health": "starting"}
            return {"status": "worker_offline", "health": "unknown"}
        containers = (((result.get("metrics") or {}).get("docker") or {}).get("containers") or [])
        container = next((item for item in containers if item.get("name") == service.get("container_name")), None)
        if not container:
            if service.get("status") == "deploying":
                return {"status": "deploying", "health": "starting"}
            return {"status": "missing", "health": "missing"}
        probe = ((container.get("vllm") or {}).get("probe") or {})
        return {
            "status": "running" if container.get("running") else container.get("state") or "stopped",
            "health": probe.get("status") or container.get("health") or ("running" if container.get("running") else "stopped"),
            "cpu_percent": container.get("cpu_percent"),
            "memory_used_bytes": container.get("memory_used_bytes"),
            "gpu_memory_used_bytes": container.get("gpu_memory_used_bytes"),
            "restart_count": container.get("restart_count"),
            "started_at": container.get("started_at"),
        }

    def list_services(self, user=None, model_ref=""):
        services = self.store.list_model_services(user)
        query = str(model_ref or "").strip().casefold()
        rows = []
        for service in services:
            if query and query not in " ".join(
                str(service.get(key) or "").casefold()
                for key in ("model_key", "model_name", "served_name", "worker_name", "container_name")
            ):
                continue
            row = dict(service)
            row["runtime"] = self.runtime_for_service(service)
            rows.append(row)
        return {
            "enabled": self.enabled,
            "services": rows,
            "api_base_url": self.oneapi.get("public_base_url"),
            "access_hint": self.oneapi.get("access_hint"),
            "generated_at": utc_now().isoformat(),
        }

    def service_is_running(self, service):
        runtime = self.runtime_for_service(service)
        return runtime.get("status") == "running" and runtime.get("health") in ("healthy", "running")

    def occupied_gpu_indices(self, worker_id):
        occupied = set()
        for service in self.store.list_model_services():
            if service.get("worker_id") == worker_id and service.get("status") in ("deploying", "running"):
                occupied.update(str(value) for value in service.get("gpu_indices") or [])
        return occupied

    def select_worker(self, model, gpu_count):
        required_per_gpu = int(math.ceil((float(model.get("weight_bytes") or 0) * 1.25 + 2 * 1024**3) / gpu_count))
        for worker_id, worker in self.workers.items():
            if worker.get("enabled") is False:
                continue
            result = self.snapshot_result(worker_id)
            if not result or result.get("status") != "online":
                continue
            devices = (((result.get("metrics") or {}).get("gpu") or {}).get("devices") or [])
            by_index = {str(item.get("index")): item for item in devices}
            allowed = [str(value) for value in worker.get("allowed_gpu_indices", sorted(by_index))]
            occupied = self.occupied_gpu_indices(worker_id)
            max_used = float(worker.get("max_gpu_memory_percent") or 10)
            candidates = []
            for index in allowed:
                device = by_index.get(index)
                if not device or index in occupied:
                    continue
                free_bytes = int(device.get("memory_total_bytes") or 0) - int(device.get("memory_used_bytes") or 0)
                if float(device.get("memory_percent") or 0) <= max_used and free_bytes >= required_per_gpu:
                    candidates.append((free_bytes, index))
            candidates.sort(reverse=True)
            if len(candidates) >= gpu_count:
                return worker, [index for _, index in candidates[:gpu_count]]
        raise RuntimeError("no configured worker has enough idle GPU capacity")

    def next_port(self, worker):
        start = int(worker.get("port_start") or 18001)
        end = int(worker.get("port_end") or start + 19)
        used = {
            int(item.get("host_port"))
            for item in self.store.list_model_services()
            if item.get("worker_id") == worker.get("server_id") and item.get("host_port")
        }
        for port in range(start, end + 1):
            if port not in used:
                return port
        raise RuntimeError("no model service port is available")

    def remote_action(self, server_id, payload, timeout=90):
        script = REMOTE_MODEL_SERVICE.replace("__PAYLOAD_JSON__", json.dumps(json.dumps(payload)))
        return self.monitor.run_remote_root_script(server_id, script, timeout=timeout)

    def create_service(self, model, gpu_count, created_by):
        worker, gpu_indices = self.select_worker(model, gpu_count)
        worker_id = str(worker.get("server_id"))
        port = self.next_port(worker)
        suffix = secrets.token_hex(2)
        container = "probe-model-%s-%s" % (safe_slug(model.get("key")), suffix)
        public_name = str(model.get("served_model_name") or model.get("name"))
        prefix = str(worker.get("internal_model_prefix") or "internal/")
        internal_name = public_name if "/" in public_name or not prefix else prefix + public_name
        model_root = str(worker.get("model_root") or "")
        model_path = str(PurePosixPath(model_root) / str(model.get("key")))
        upstream_key = "sk-upstream-" + secrets.token_urlsafe(32)
        service = self.store.create_model_service(
            {
                "model_key": model.get("key"),
                "model_name": model.get("name"),
                "served_name": public_name,
                "status": "deploying",
                "worker_id": worker_id,
                "worker_name": worker.get("name") or self.monitor.request_machine_label(worker_id),
                "container_name": container,
                "gpu_indices": gpu_indices,
                "host_port": port,
                "image": worker.get("image"),
                "source": "managed",
                "created_by": created_by,
                "progress_stage": "gpu_allocated",
                "progress_percent": 15,
            }
        )
        try:
            self.store.update_model_service(
                service["id"], progress_stage="starting_vllm", progress_percent=35
            )
            self.remote_action(
                worker_id,
                {
                    "action": "deploy",
                    "container_name": container,
                    "model_key": model.get("key"),
                    "image": worker.get("image"),
                    "model_root": model_root,
                    "model_path": model_path,
                    "internal_served_name": internal_name,
                    "upstream_api_key": upstream_key,
                    "gpu_indices": gpu_indices,
                    "host_port": port,
                    "bind_host": worker.get("bind_host") or "127.0.0.1",
                    "gpu_memory_utilization": worker.get("gpu_memory_utilization") or 0.85,
                    "max_model_len": worker.get("max_model_len") or 8192,
                    "trust_remote_code": bool(worker.get("trust_remote_code")),
                    "startup_timeout_seconds": worker.get("startup_timeout_seconds") or 600,
                },
                timeout=int(worker.get("startup_timeout_seconds") or 600) + 120,
            )
            self.store.update_model_service(
                service["id"], progress_stage="registering_gateway", progress_percent=80
            )
            channel_host = str(worker.get("oneapi_base_url") or "http://127.0.0.1:{port}").format(port=port)
            oneapi_result = self.remote_action(
                str(self.oneapi.get("server_id") or worker_id),
                {
                    "action": "oneapi_channel",
                    "database_path": self.oneapi.get("database_path"),
                    "channel_name": "probe-%s" % container,
                    "public_model": public_name,
                    "internal_model": internal_name,
                    "base_url": channel_host,
                    "upstream_api_key": upstream_key,
                },
                timeout=90,
            )
            return self.store.update_model_service(
                service["id"],
                status="running",
                oneapi_channel_id=oneapi_result.get("channel_id"),
                error_message=None,
                progress_stage="running",
                progress_percent=100,
            )
        except Exception as exc:
            self.store.update_model_service(
                service["id"],
                status="failed",
                error_message=str(exc)[:1000],
                progress_stage="failed",
                progress_percent=100,
            )
            raise

    def token_action(self, action, **values):
        server_id = str(self.oneapi.get("server_id") or "")
        if not server_id:
            raise RuntimeError("One API server is not configured")
        payload = {
            "action": action,
            "database_path": self.oneapi.get("database_path"),
            "token_prefix": self.oneapi.get("token_prefix") or "sk-",
        }
        payload.update(values)
        return self.remote_action(server_id, payload, timeout=90)

    def credential_payload(self, service, allocation, api_key, reused):
        return {
            "service": service,
            "allocation": allocation,
            "credential": {
                "base_url": self.oneapi.get("public_base_url"),
                "api_key": api_key,
                "model": service.get("served_name"),
                "expires_at": allocation.get("expires_at"),
                "access_hint": self.oneapi.get("access_hint"),
            },
            "reused": bool(reused),
        }

    def deploy(self, model_key, requester_id, owner_name, duration_hours, gpu_count=None, request_id=None, created_by=None):
        if not self.enabled:
            raise RuntimeError("managed model deployment is disabled")
        try:
            duration = max(1, min(int(duration_hours), 24 * 30))
        except (TypeError, ValueError):
            raise ValueError("duration must be between 1 and 720 hours") from None
        model = self.model_by_key(model_key)
        recommended = max(1, int(model.get("recommended_gpu_count") or 1))
        requested_gpus = recommended if gpu_count in (None, "") else max(recommended, int(gpu_count))
        if requested_gpus > 8:
            raise ValueError("managed deployment currently supports at most 8 GPUs")
        existing_allocation = self.store.get_model_allocation(request_id=request_id) if request_id is not None else None
        if existing_allocation and existing_allocation.get("oneapi_token_id"):
            service = self.store.get_model_service(existing_allocation["service_id"])
            token = self.token_action("oneapi_token_get", token_id=existing_allocation["oneapi_token_id"])
            return self.credential_payload(service, existing_allocation, token.get("api_key"), reused=True)

        with self.lock:
            service = self.store.find_active_model_service(model.get("key"))
            reused = bool(service and self.service_is_running(service))
            if service and not reused:
                self.store.update_model_service(service["id"], status="failed", error_message="runtime service is unavailable")
                service = None
            if not service:
                service = self.create_service(model, requested_gpus, created_by)

            expires_at = utc_now() + timedelta(hours=duration)
            token_name = "probe-%s-%s" % (service["id"], safe_slug(owner_name, "user"))
            allocation = self.store.create_model_allocation(
                {
                    "service_id": service["id"],
                    "request_id": request_id,
                    "requester_id": requester_id,
                    "owner_name": str(owner_name or "")[:80],
                    "token_name": token_name,
                    "quota": int(self.oneapi.get("default_quota") or 5000000),
                    "expires_at": expires_at,
                    "created_by": created_by,
                }
            )
            try:
                token = self.token_action(
                    "oneapi_token",
                    user_id=int(self.oneapi.get("user_id") or 1),
                    token_name="%s-%s" % (token_name, allocation["id"]),
                    public_model=service.get("served_name"),
                    expired_time=int(expires_at.timestamp()),
                    quota=allocation.get("quota"),
                )
                allocation = self.store.update_model_allocation(
                    allocation["id"], "active", token_id=token.get("token_id"), error_message=None
                )
                return self.credential_payload(service, allocation, token.get("api_key"), reused=reused)
            except Exception as exc:
                self.store.update_model_allocation(allocation["id"], "failed", error_message=str(exc)[:1000])
                raise
