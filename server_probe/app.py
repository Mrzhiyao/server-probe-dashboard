#!/usr/bin/env python3
"""Small HTTP dashboard that collects Linux metrics over SSH."""

import argparse
import http.cookies
import json
import mimetypes
import os
import posixpath
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paramiko

from server_probe.auth import AuthStore, utc_now


APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
COLLECTOR_PATH = APP_DIR / "server_probe" / "collector.py"
SESSION_COOKIE = "probe_session"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def as_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def rounded(value):
    number = as_number(value)
    if number is None:
        return None
    return round(number, 1)


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def public_server(server):
    safe = {
        "id": server["id"],
        "name": server.get("name") or server["id"],
        "group": server.get("group") or "default",
        "host": server.get("host"),
        "port": server.get("port", 22),
        "user": server.get("user"),
        "connection": server.get("connection", "ssh"),
        "tags": server.get("tags", []),
    }
    if server.get("jump"):
        jump = server["jump"]
        safe["jump"] = {"host": jump.get("host"), "port": jump.get("port", 22), "user": jump.get("user")}
    return safe


def request_machine(server):
    return {
        "id": server["id"],
        "name": server.get("name") or server["id"],
        "group": server.get("group") or "default",
        "host": server.get("host"),
        "port": server.get("port", 22),
        "connection": server.get("connection", "ssh"),
        "tags": server.get("tags", []),
    }


class Monitor:
    def __init__(self, config):
        self.config = config
        self.servers = config.get("servers", [])
        self.refresh_seconds = float(config.get("refresh_seconds", 30))
        self.cache_ttl = float(config.get("cache_ttl_seconds", max(self.refresh_seconds, 30)))
        self.connect_timeout = float(config.get("connect_timeout_seconds", 7))
        self.command_timeout = float(config.get("command_timeout_seconds", 12))
        self.executor = ThreadPoolExecutor(max_workers=int(config.get("concurrency", 8)))
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.snapshot_lock = threading.Lock()
        self.latest_snapshot = None
        self.history_retention_points = int(config.get("history_retention_points", 240))
        self.history = {}
        self.history_lock = threading.Lock()
        self.alert_thresholds = self.load_alert_thresholds()
        self.last_refresh_started_at = None
        self.last_refresh_finished_at = None
        self.refresh_lock = threading.Lock()
        self.collector_source = COLLECTOR_PATH.read_text(encoding="utf-8")
        self.secrets = self._load_known_secrets()
        self.background_thread = threading.Thread(target=self.background_refresh_loop, daemon=True)
        self.background_thread.start()

    def _load_known_secrets(self):
        values = []
        for server in self.servers:
            for item in (server, server.get("jump") or {}):
                password = item.get("password")
                if password:
                    values.append(str(password))
                env_name = item.get("password_env")
                if env_name and os.getenv(env_name):
                    values.append(os.getenv(env_name))
        return [value for value in values if value]

    def redact(self, text):
        if not text:
            return ""
        redacted = str(text)
        for secret in self.secrets:
            if secret:
                redacted = redacted.replace(secret, "******")
        return redacted

    def resolve_password(self, item):
        if item.get("password_env"):
            value = os.getenv(item["password_env"])
            if value is None:
                raise RuntimeError("missing password env %s" % item["password_env"])
            return value
        return item.get("password")

    def get_server(self, server_id):
        for server in self.servers:
            if server["id"] == server_id:
                return server
        return None

    def request_machines(self):
        return [request_machine(server) for server in self.servers]

    def request_machine_label(self, server_id):
        server = self.get_server(server_id)
        if not server:
            return server_id
        label = server.get("name") or server["id"]
        host = server.get("host")
        return "%s (%s)" % (label, host) if host and host != label else label

    def load_alert_thresholds(self):
        defaults = {
            "cpu_warn_percent": 85,
            "cpu_critical_percent": 95,
            "memory_warn_percent": 88,
            "memory_critical_percent": 95,
            "gpu_warn_percent": 92,
            "gpu_critical_percent": 98,
            "disk_warn_percent": 90,
            "disk_critical_percent": 95,
        }
        configured = self.config.get("alert_thresholds") or {}
        for key, value in configured.items():
            number = as_number(value)
            if key in defaults and number is not None:
                defaults[key] = number
        return defaults

    def collect_all(self, force=False):
        futures = {self.executor.submit(self.collect_server, server, force): server for server in self.servers}
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                server = futures[future]
                results.append(self.error_result(server, exc, 0))
        results.sort(key=lambda item: (item.get("group", ""), item.get("name", "")))
        snapshot = {
            "generated_at": utc_now_iso(),
            "refresh_seconds": self.refresh_seconds,
            "servers": [public_server(server) for server in self.servers],
            "results": results,
            "alerts": self.alerts_for_results(results),
        }
        self.record_history(snapshot)
        return snapshot

    def background_refresh_loop(self):
        while True:
            self.refresh_snapshot(force=True)
            time.sleep(self.refresh_seconds)

    def trigger_refresh(self, force=True):
        if self.refresh_lock.locked():
            return False
        thread = threading.Thread(target=self.refresh_snapshot, kwargs={"force": force}, daemon=True)
        thread.start()
        return True

    def refresh_snapshot(self, force=True):
        if not self.refresh_lock.acquire(blocking=False):
            return None
        self.last_refresh_started_at = utc_now_iso()
        try:
            snapshot = self.collect_all(force=force)
            self.last_refresh_finished_at = utc_now_iso()
            with self.snapshot_lock:
                self.latest_snapshot = snapshot
            return snapshot
        finally:
            self.refresh_lock.release()

    def empty_snapshot(self):
        return {
            "generated_at": None,
            "refresh_seconds": self.refresh_seconds,
            "servers": [public_server(server) for server in self.servers],
            "results": [],
        }

    def compact_snapshot(self, snapshot):
        compact = dict(snapshot)
        compact["results"] = [self.compact_result(result) for result in snapshot.get("results", [])]
        compact["history"] = self.history_payload()
        compact["history_retention_points"] = self.history_retention_points
        return compact

    def compact_result(self, result):
        compact = dict(result)
        metrics = result.get("metrics")
        if not metrics:
            return compact

        compact_metrics = dict(metrics)
        processes = metrics.get("processes") or {}
        compact_metrics["processes"] = {
            "top_cpu_count": len(processes.get("top_cpu") or []),
            "top_mem_count": len(processes.get("top_mem") or []),
        }

        gpu = metrics.get("gpu")
        if gpu:
            compact_gpu = dict(gpu)
            compact_gpu["process_count"] = len(gpu.get("processes") or [])
            compact_gpu.pop("processes", None)
            compact_metrics["gpu"] = compact_gpu

        compact["metrics"] = compact_metrics
        return compact

    def gpu_stats(self, metrics):
        devices = (((metrics or {}).get("gpu") or {}).get("devices") or [])
        util_values = []
        memory_values = []
        used_total = 0
        total_total = 0
        for device in devices:
            util = as_number(device.get("utilization_percent"))
            memory = as_number(device.get("memory_percent"))
            used = as_number(device.get("memory_used_bytes"))
            total = as_number(device.get("memory_total_bytes"))
            if util is not None:
                util_values.append(util)
            if memory is not None:
                memory_values.append(memory)
            if used is not None and total is not None and total > 0:
                used_total += used
                total_total += total

        average_util = sum(util_values) / len(util_values) if util_values else None
        aggregate_memory = (used_total / total_total) * 100.0 if total_total else None
        peaks = util_values + memory_values
        peak = max(peaks) if peaks else None
        return {
            "average_util": rounded(average_util),
            "aggregate_memory": rounded(aggregate_memory),
            "peak": rounded(peak),
        }

    def alerts_for_results(self, results):
        alerts = []
        for result in results:
            alerts.extend(result.get("alerts") or self.alerts_for_result(result))
        alerts.sort(key=lambda item: (0 if item.get("severity") == "critical" else 1, item.get("server_name", "")))
        return alerts

    def alert_item(self, result, severity, kind, metric=None, value=None, threshold=None):
        return {
            "server_id": result.get("id"),
            "server_name": result.get("name") or result.get("id"),
            "group": result.get("group"),
            "host": result.get("host"),
            "severity": severity,
            "kind": kind,
            "metric": metric,
            "value": rounded(value),
            "threshold": rounded(threshold),
            "collected_at": result.get("collected_at"),
        }

    def alerts_for_result(self, result):
        if result.get("status") != "online":
            return [self.alert_item(result, "critical", "offline")]

        metrics = result.get("metrics") or {}
        gpu = self.gpu_stats(metrics)
        checks = [
            ("cpu", "CPU", metrics.get("cpu", {}).get("percent"), "cpu"),
            ("memory", "Memory", metrics.get("memory", {}).get("percent"), "memory"),
            ("gpu", "GPU", gpu.get("peak"), "gpu"),
            ("disk", "Disk", metrics.get("disk", {}).get("percent"), "disk"),
        ]

        alerts = []
        for kind, metric, value, prefix in checks:
            number = as_number(value)
            warn = self.alert_thresholds.get("%s_warn_percent" % prefix)
            critical = self.alert_thresholds.get("%s_critical_percent" % prefix)
            if number is None or warn is None or number < warn:
                continue
            severity = "critical" if critical is not None and number >= critical else "warning"
            threshold = critical if severity == "critical" else warn
            alerts.append(self.alert_item(result, severity, kind, metric, number, threshold))
        return alerts

    def record_history(self, snapshot):
        with self.history_lock:
            for result in snapshot.get("results", []):
                server_id = result.get("id")
                if not server_id:
                    continue
                samples = self.history.setdefault(server_id, [])
                samples.append(self.history_sample(result))
                overflow = len(samples) - self.history_retention_points
                if overflow > 0:
                    del samples[:overflow]

    def history_sample(self, result):
        sample = {
            "time": result.get("collected_at") or utc_now_iso(),
            "status": result.get("status"),
        }
        metrics = result.get("metrics") or {}
        gpu = self.gpu_stats(metrics)
        if result.get("status") == "online":
            sample.update(
                {
                    "cpu": rounded(metrics.get("cpu", {}).get("percent")),
                    "mem": rounded(metrics.get("memory", {}).get("percent")),
                    "gpu": gpu.get("average_util"),
                    "gpu_mem": gpu.get("aggregate_memory"),
                    "gpu_peak": gpu.get("peak"),
                    "disk": rounded(metrics.get("disk", {}).get("percent")),
                    "load1": rounded(metrics.get("cpu", {}).get("load1")),
                }
            )
        return sample

    def history_payload(self):
        with self.history_lock:
            return {server_id: list(samples) for server_id, samples in self.history.items()}

    def resource_recommendation(self, requirements):
        gpu_count = max(0, int(as_number(requirements.get("gpu_count")) or 0))
        memory_gb = as_number(requirements.get("gpu_memory_gb")) or 0
        memory_bytes = memory_gb * 1024 * 1024 * 1024
        with self.snapshot_lock:
            snapshot = self.latest_snapshot or self.empty_snapshot()
            results = list(snapshot.get("results") or [])

        candidates = []
        for result in results:
            if result.get("status") != "online":
                continue
            metrics = result.get("metrics") or {}
            devices = ((metrics.get("gpu") or {}).get("devices") or [])
            cpu = as_number((metrics.get("cpu") or {}).get("percent")) or 0
            mem = as_number((metrics.get("memory") or {}).get("percent")) or 0
            if gpu_count <= 0:
                score = (100 - cpu) + (100 - mem)
                candidates.append(
                    {
                        "server_id": result.get("id"),
                        "server_name": result.get("name"),
                        "group": result.get("group"),
                        "gpu_indices": [],
                        "free_gpu_memory_gb": None,
                        "max_gpu_util_percent": None,
                        "cpu_percent": rounded(cpu),
                        "memory_percent": rounded(mem),
                        "score": rounded(score),
                        "reason": "CPU and memory are currently available",
                    }
                )
                continue

            usable = []
            for device in devices:
                total = as_number(device.get("memory_total_bytes")) or 0
                used = as_number(device.get("memory_used_bytes")) or 0
                free = max(total - used, 0)
                util = as_number(device.get("utilization_percent")) or 0
                if free >= memory_bytes:
                    usable.append(
                        {
                            "index": str(device.get("index")),
                            "free": free,
                            "util": util,
                        }
                    )
            usable.sort(key=lambda item: (item["util"], -item["free"]))
            if len(usable) < gpu_count:
                continue
            selected = usable[:gpu_count]
            total_free = sum(item["free"] for item in selected)
            max_util = max(item["util"] for item in selected) if selected else None
            score = (total_free / (1024 * 1024 * 1024)) - (max_util or 0) - cpu * 0.2 - mem * 0.1
            candidates.append(
                {
                    "server_id": result.get("id"),
                    "server_name": result.get("name"),
                    "group": result.get("group"),
                    "gpu_indices": [item["index"] for item in selected],
                    "free_gpu_memory_gb": round(total_free / (1024 * 1024 * 1024), 1),
                    "max_gpu_util_percent": rounded(max_util),
                    "cpu_percent": rounded(cpu),
                    "memory_percent": rounded(mem),
                    "score": rounded(score),
                    "reason": "Enough GPUs match the requested free memory",
                }
            )

        candidates.sort(key=lambda item: item.get("score") or 0, reverse=True)
        return {
            "generated_at": utc_now_iso(),
            "requested_gpu_count": gpu_count,
            "requested_gpu_memory_gb": rounded(memory_gb),
            "candidates": candidates[:5],
            "message": "Found matching machines" if candidates else "No matching online machines found",
        }

    def cached_snapshot(self, trigger=False):
        if trigger or self.latest_snapshot is None:
            self.trigger_refresh(force=True)

        with self.snapshot_lock:
            snapshot = self.latest_snapshot or self.empty_snapshot()
            payload = self.compact_snapshot(snapshot)

        payload["cache"] = {
            "refreshing": self.refresh_lock.locked(),
            "last_refresh_started_at": self.last_refresh_started_at,
            "last_refresh_finished_at": self.last_refresh_finished_at,
            "has_snapshot": self.latest_snapshot is not None,
        }
        return payload

    def collect_server(self, server, force=False):
        server_id = server["id"]
        with self.cache_lock:
            cached = self.cache.get(server_id)
            if cached and not force and cached["expires_at"] > time.time():
                return cached["result"]

        started = time.time()
        try:
            if server.get("connection") == "local":
                metrics = self.collect_local()
            else:
                metrics = self.collect_ssh(server)
            latency_ms = int((time.time() - started) * 1000)
            result = {
                **public_server(server),
                "status": "online",
                "latency_ms": latency_ms,
                "collected_at": utc_now_iso(),
                "metrics": metrics,
            }
            result["alerts"] = self.alerts_for_result(result)
        except Exception as exc:
            latency_ms = int((time.time() - started) * 1000)
            result = self.error_result(server, exc, latency_ms)

        with self.cache_lock:
            self.cache[server_id] = {"expires_at": time.time() + self.cache_ttl, "result": result}
        return result

    def error_result(self, server, exc, latency_ms):
        result = {
            **public_server(server),
            "status": "offline",
            "latency_ms": latency_ms,
            "collected_at": utc_now_iso(),
            "error": self.redact(str(exc))[:600],
        }
        result["alerts"] = self.alerts_for_result(result)
        return result

    def collect_local(self):
        completed = subprocess.run(
            [sys.executable, str(COLLECTOR_PATH)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=self.command_timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "local collector failed")
        return json.loads(completed.stdout)

    def collect_ssh(self, server):
        jump_client = None
        client = None
        try:
            sock = None
            if server.get("jump"):
                jump = server["jump"]
                jump_client = self.open_client(
                    host=jump["host"],
                    port=int(jump.get("port", 22)),
                    user=jump["user"],
                    password=self.resolve_password(jump),
                )
                transport = jump_client.get_transport()
                sock = transport.open_channel(
                    "direct-tcpip",
                    (server["host"], int(server.get("port", 22))),
                    ("127.0.0.1", 0),
                )

            client = self.open_client(
                host=server["host"],
                port=int(server.get("port", 22)),
                user=server["user"],
                password=self.resolve_password(server),
                sock=sock,
            )
            output = self.run_remote_collector(client)
            return json.loads(output)
        finally:
            if client is not None:
                client.close()
            if jump_client is not None:
                jump_client.close()

    def open_client(self, host, port, user, password, sock=None):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            sock=sock,
            timeout=self.connect_timeout,
            banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

    def run_remote_collector(self, client):
        command = "sh -lc 'if command -v python3 >/dev/null 2>&1; then exec python3 -; else exec python -; fi'"
        stdin, stdout, stderr = client.exec_command(command, timeout=self.command_timeout)
        stdin.write(self.collector_source)
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise RuntimeError(err.strip() or "remote collector exited with %s" % code)
        if not out.strip():
            raise RuntimeError("remote collector returned empty output")
        return out.strip()


def load_config(path):
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    ids = set()
    for server in config.get("servers", []):
        if "id" not in server:
            raise ValueError("server missing id")
        if server["id"] in ids:
            raise ValueError("duplicate server id %s" % server["id"])
        ids.add(server["id"])
        if server.get("connection") != "local":
            for key in ("host", "user"):
                if key not in server:
                    raise ValueError("server %s missing %s" % (server["id"], key))
    return config


class DashboardHandler(BaseHTTPRequestHandler):
    monitor = None
    auth_enabled = False
    auth_store = None
    cookie_secure = False
    login_lock = threading.Lock()
    login_failures = {}

    def log_message(self, format, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location):
        body = b""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, limit=16384):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        if length <= 0 or length > limit:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def cookie_token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        try:
            cookies = http.cookies.SimpleCookie(raw)
            morsel = cookies.get(SESSION_COOKIE)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def cookie_header(self, token, max_age):
        parts = [
            "%s=%s" % (SESSION_COOKIE, token),
            "Path=/",
            "Max-Age=%s" % int(max_age),
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def current_user(self):
        if not self.auth_enabled:
            return {"username": "anonymous", "role": "admin", "display_name": None}
        token = self.cookie_token()
        if not token:
            return None
        return self.auth_store.user_for_session(token)

    def is_admin(self, user):
        return bool(user and user.get("role") == "admin")

    def is_public_get(self, route):
        return route in ("/login", "/api/health", "/favicon.ico") or route.startswith("/static/")

    def require_user(self, route):
        if not self.auth_enabled or self.is_public_get(route):
            return {"username": "anonymous", "role": "admin", "display_name": None}
        user = self.current_user()
        if user:
            return user
        if route.startswith("/api/"):
            self.send_json({"error": "authentication required"}, status=401)
        else:
            next_path = urllib.parse.quote(self.path or "/", safe="")
            self.send_redirect("/login?next=%s" % next_path)
        return None

    def require_admin(self, user):
        if self.is_admin(user):
            return True
        self.send_json({"error": "admin permission required"}, status=403)
        return False

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded or self.client_address[0]

    def login_key(self, username):
        return "%s:%s" % (self.client_ip(), (username or "").strip().lower())

    def login_limited(self, username):
        key = self.login_key(username)
        now = time.time()
        with self.login_lock:
            values = [value for value in self.login_failures.get(key, []) if now - value < 600]
            self.login_failures[key] = values
            return len(values) >= 5

    def record_login_failure(self, username):
        key = self.login_key(username)
        now = time.time()
        with self.login_lock:
            values = [value for value in self.login_failures.get(key, []) if now - value < 600]
            values.append(now)
            self.login_failures[key] = values

    def clear_login_failures(self, username):
        with self.login_lock:
            self.login_failures.pop(self.login_key(username), None)

    def handle_login(self):
        if not self.auth_enabled:
            self.send_json({"ok": True, "auth_enabled": False})
            return
        data = self.read_json_body()
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not username or not password:
            self.send_json({"error": "username and password are required"}, status=400)
            return
        if self.login_limited(username):
            self.send_json({"error": "too many failed login attempts"}, status=429)
            return

        user = self.auth_store.verify_user(username, password)
        if not user:
            self.record_login_failure(username)
            self.send_json({"error": "invalid username or password"}, status=401)
            return

        self.clear_login_failures(username)
        token, expires_at = self.auth_store.create_session(
            user["id"],
            ip_address=self.client_ip(),
            user_agent=self.headers.get("User-Agent", ""),
        )
        max_age = max(1, int((expires_at - utc_now()).total_seconds()))
        self.send_json(
            {"ok": True, "user": self.auth_user_payload(user)},
            headers={"Set-Cookie": self.cookie_header(token, max_age)},
        )

    def handle_logout(self):
        if self.auth_enabled:
            self.auth_store.destroy_session(self.cookie_token())
        self.send_json(
            {"ok": True},
            headers={"Set-Cookie": self.cookie_header("", 0)},
        )

    def auth_user_payload(self, user):
        return {
            "username": user.get("username"),
            "role": user.get("role"),
            "display_name": user.get("display_name"),
        }

    def validated_request_payload(self):
        data = self.read_json_body(limit=32768)
        request_type = str(data.get("request_type") or "temporary").strip()
        if request_type not in ("temporary", "access"):
            raise ValueError("invalid request type")
        owner_name = str(data.get("owner_name") or "").strip()
        target_machine = str(data.get("target_machine") or "").strip()
        target_machine_label = self.monitor.request_machine_label(target_machine) if target_machine else ""
        requested_account = str(data.get("requested_account") or "").strip()
        requested_password = str(data.get("requested_password") or "")
        model_name = str(data.get("model_name") or "").strip()
        purpose = str(data.get("purpose") or "").strip()
        gpu_count = int(as_number(data.get("gpu_count")) or 0)
        if gpu_count < 0 or gpu_count > 16:
            raise ValueError("gpu count is out of range")
        duration_hours = int(as_number(data.get("duration_hours")) or 0)
        if duration_hours <= 0:
            duration_hours = None
        if request_type == "temporary":
            if not model_name or not purpose:
                raise ValueError("model name and purpose are required")
            if duration_hours is None:
                raise ValueError("duration is required for temporary requests")
        else:
            if not owner_name:
                raise ValueError("owner name is required")
            if not target_machine:
                raise ValueError("target machine is required")
            if not requested_account or not requested_password:
                raise ValueError("account name and password are required")
            if not model_name:
                model_name = "Long-term access for %s" % requested_account
            if not purpose:
                purpose = "Long-term machine access request"
        return {
            "request_type": request_type,
            "owner_name": owner_name[:80],
            "model_name": model_name[:160],
            "model_size": str(data.get("model_size") or "").strip()[:80],
            "purpose": purpose[:2000],
            "access_type": str(data.get("access_type") or "ssh").strip()[:20],
            "gpu_count": gpu_count,
            "gpu_memory_gb": rounded(data.get("gpu_memory_gb")),
            "duration_hours": duration_hours,
            "target_machine": target_machine[:160],
            "target_machine_label": target_machine_label[:240],
            "requested_account": requested_account[:120],
            "requested_password": requested_password[:300],
            "notes": str(data.get("notes") or "").strip()[:3000],
        }

    def create_model_request(self, user):
        try:
            payload = self.validated_request_payload()
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        recommendation = {}
        if payload["request_type"] == "temporary":
            recommendation = self.monitor.resource_recommendation(payload)
        else:
            existing = self.auth_store.find_existing_accounts(
                payload.get("owner_name") or user.get("display_name") or user.get("username"),
                payload.get("target_machine") or payload.get("target_machine_label"),
                payload.get("requested_account"),
            )
            if existing:
                self.send_json(
                    {
                        "error": "account already exists",
                        "existing_accounts": existing,
                    },
                    status=409,
                )
                return
            recommendation = {
                "generated_at": utc_now_iso(),
                "message": "Long-term access request requires admin approval",
                "candidates": [],
            }
        request_id = self.auth_store.create_model_request(user["id"], payload, recommendation)
        self.send_json({"ok": True, "id": request_id, "recommendation": recommendation}, status=201)

    def update_model_request(self, route, user):
        if not self.require_admin(user):
            return
        try:
            request_id = int(route.split("/")[-2])
        except Exception:
            self.send_json({"error": "invalid request id"}, status=400)
            return
        data = self.read_json_body(limit=32768)
        try:
            updated = self.auth_store.update_model_request(
                request_id,
                user["id"],
                str(data.get("status") or "").strip(),
                admin_note=str(data.get("admin_note") or "").strip()[:3000],
                allocation_note=str(data.get("allocation_note") or "").strip()[:3000],
            )
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        if not updated:
            self.send_json({"error": "request not found"}, status=404)
            return
        self.send_json({"ok": True, "request": updated})

    def create_user(self, user):
        if not self.require_admin(user):
            return
        data = self.read_json_body()
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        role = str(data.get("role") or "user").strip()
        display_name = str(data.get("display_name") or "").strip()
        if not username or not password:
            self.send_json({"error": "username and password are required"}, status=400)
            return
        if role not in ("admin", "user"):
            self.send_json({"error": "invalid role"}, status=400)
            return
        self.auth_store.set_password(username, password, role=role, display_name=display_name or None)
        self.send_json({"ok": True, "user": {"username": username, "role": role, "display_name": display_name}}, status=201)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/api/health":
            self.send_json({"ok": True, "time": utc_now_iso()})
            return

        if route == "/login":
            if self.auth_enabled and self.current_user():
                next_path = query.get("next", ["/"])[0] or "/"
                if not next_path.startswith("/"):
                    next_path = "/"
                self.send_redirect(next_path)
                return
            self.send_file(STATIC_DIR / "login.html")
            return

        user = self.require_user(route)
        if user is None:
            return

        if route == "/api/auth/me":
            self.send_json(
                {
                    "authenticated": bool(user),
                    "auth_enabled": self.auth_enabled,
                    "user": self.auth_user_payload(user),
                }
            )
            return

        if route == "/requests":
            if not self.auth_enabled:
                self.send_redirect("/")
                return
            self.send_file(STATIC_DIR / "requests.html")
            return

        if route == "/api/resource-requests":
            if not self.auth_enabled:
                self.send_json({"error": "authentication is disabled"}, status=404)
                return
            self.send_json({"requests": self.auth_store.list_model_requests(user)})
            return

        if route == "/api/request-machines":
            if not self.auth_enabled:
                self.send_json({"error": "authentication is disabled"}, status=404)
                return
            self.send_json({"machines": self.monitor.request_machines()})
            return

        if route == "/api/users":
            if not self.auth_enabled:
                self.send_json({"error": "authentication is disabled"}, status=404)
                return
            if not self.require_admin(user):
                return
            self.send_json({"users": self.auth_store.list_users()})
            return

        if route == "/api/servers":
            if not self.require_admin(user):
                return
            config = self.monitor.config
            groups = sorted({server.get("group", "default") for server in self.monitor.servers})
            self.send_json(
                {
                    "title": config.get("title", "Server Probe Dashboard"),
                    "refresh_seconds": self.monitor.refresh_seconds,
                    "history_retention_points": self.monitor.history_retention_points,
                    "alert_thresholds": self.monitor.alert_thresholds,
                    "groups": groups,
                    "servers": [public_server(server) for server in self.monitor.servers],
                }
            )
            return

        if route == "/api/snapshot":
            if not self.require_admin(user):
                return
            force = query.get("force", ["0"])[0] == "1"
            self.send_json(self.monitor.cached_snapshot(trigger=force))
            return

        if route == "/api/history":
            if not self.require_admin(user):
                return
            self.send_json(
                {
                    "generated_at": utc_now_iso(),
                    "retention_points": self.monitor.history_retention_points,
                    "refresh_seconds": self.monitor.refresh_seconds,
                    "history": self.monitor.history_payload(),
                }
            )
            return

        if route.startswith("/api/server/"):
            if not self.require_admin(user):
                return
            server_id = urllib.parse.unquote(route.rsplit("/", 1)[-1])
            server = self.monitor.get_server(server_id)
            if not server:
                self.send_json({"error": "server not found"}, status=404)
                return
            force = query.get("force", ["0"])[0] == "1"
            self.send_json(self.monitor.collect_server(server, force=force))
            return

        if route in ("", "/"):
            if not self.is_admin(user):
                self.send_redirect("/requests")
                return
            self.send_file(STATIC_DIR / "index.html")
            return

        if route.startswith("/static/"):
            rel = posixpath.normpath(urllib.parse.unquote(route[len("/static/") :]))
            if rel.startswith("../"):
                self.send_error(403)
                return
            self.send_file(STATIC_DIR / rel)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/api/auth/login":
            self.handle_login()
            return

        user = self.require_user(route)
        if user is None:
            return

        if route == "/api/auth/logout":
            self.handle_logout()
            return

        if route == "/api/resource-requests":
            if not self.auth_enabled:
                self.send_json({"error": "authentication is disabled"}, status=404)
                return
            self.create_model_request(user)
            return

        if route.startswith("/api/resource-requests/") and route.endswith("/status"):
            if not self.auth_enabled:
                self.send_json({"error": "authentication is disabled"}, status=404)
                return
            self.update_model_request(route, user)
            return

        if route == "/api/users":
            if not self.auth_enabled:
                self.send_json({"error": "authentication is disabled"}, status=404)
                return
            self.create_user(user)
            return

        self.send_error(404)


def main(argv=None):
    parser = argparse.ArgumentParser(description="SSH server resource dashboard")
    parser.add_argument("--config", default=os.getenv("PROBE_CONFIG", str(APP_DIR / "config" / "servers.json")))
    parser.add_argument("--host", default=os.getenv("PROBE_HOST", "0.0.0.0"))
    parser.add_argument("--port", default=int(os.getenv("PROBE_PORT", "8088")), type=int)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    DashboardHandler.monitor = Monitor(config)
    auth_config = config.get("auth") or {}
    auth_enabled = env_bool("PROBE_AUTH_ENABLED", bool(auth_config.get("enabled", False)))
    DashboardHandler.auth_enabled = auth_enabled
    DashboardHandler.cookie_secure = env_bool("PROBE_AUTH_COOKIE_SECURE", bool(auth_config.get("cookie_secure", False)))
    if auth_enabled:
        dsn = os.getenv("PROBE_AUTH_DB_DSN") or auth_config.get("postgres_dsn")
        if not dsn:
            raise RuntimeError("PROBE_AUTH_DB_DSN is required when authentication is enabled")
        session_hours = int(os.getenv("PROBE_AUTH_SESSION_HOURS", auth_config.get("session_hours", 12)))
        auth_store = AuthStore(dsn, session_hours=session_hours)
        auth_store.setup()
        bootstrap_user = os.getenv("PROBE_AUTH_BOOTSTRAP_USER")
        bootstrap_password = os.getenv("PROBE_AUTH_BOOTSTRAP_PASSWORD")
        if bootstrap_user and bootstrap_password:
            bootstrap_role = os.getenv("PROBE_AUTH_BOOTSTRAP_ROLE") or auth_config.get("bootstrap_role", "admin")
            if bootstrap_role not in ("admin", "user"):
                bootstrap_role = "admin"
            auth_store.set_password(bootstrap_user, bootstrap_password, role=bootstrap_role)
        DashboardHandler.auth_store = auth_store
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print("Server probe dashboard listening on http://%s:%s" % (args.host, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
