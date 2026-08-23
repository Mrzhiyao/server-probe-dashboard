"""Hourly per-user resource summaries for Feishu."""

import math
import threading
import time
from datetime import datetime

from server_probe.notifications import BEIJING_TIMEZONE, clean_text, utc_iso


def as_number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_bytes(value):
    value = max(0.0, as_number(value))
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d %s" % (int(value), units[index])
    return "%.1f %s" % (value, units[index])


def format_percent(value):
    value = as_number(value)
    return ("%.1f" % value).rstrip("0").rstrip(".") + "%"


def runtime_user_matches_owner(container, owner, user_row):
    runtime_user = str(container.get("runtime_user") or "root").split(":", 1)[0].strip()
    if runtime_user == owner:
        return True
    uid = user_row.get("uid")
    return uid is not None and runtime_user == str(uid)


def current_user_usage(snapshot, excluded_users=None):
    excluded = {str(value).strip().lower() for value in (excluded_users or []) if str(value).strip()}
    machines = []
    for result in snapshot.get("results") or []:
        if result.get("status") != "online":
            continue
        metrics = result.get("metrics") or {}
        users = {}

        def user_entry(username):
            username = clean_text(username, 64)
            if not username or username.lower() in excluded:
                return None
            return users.setdefault(
                username,
                {
                    "user": username,
                    "uid": None,
                    "cpu_percent": 0.0,
                    "memory_bytes": 0,
                    "gpu_memory_bytes": 0,
                    "gpu_util_percent": 0.0,
                    "process_count": 0,
                    "running_process_count": 0,
                    "container_count": 0,
                    "gpu_process_count": 0,
                    "gpu_indices": set(),
                },
            )

        for row in metrics.get("user_resources") or []:
            entry = user_entry(row.get("user"))
            if entry is None:
                continue
            entry["uid"] = row.get("uid")
            entry["cpu_percent"] += as_number(row.get("cpu_percent_sum"))
            entry["memory_bytes"] += int(as_number(row.get("rss_bytes")))
            entry["process_count"] += int(as_number(row.get("process_count")))
            entry["running_process_count"] += int(as_number(row.get("running_process_count")))

        gpu_users = ((metrics.get("gpu") or {}).get("user_summary") or [])
        for row in gpu_users:
            username = clean_text(row.get("user"), 64)
            entry = users.get(username)
            if entry is None:
                continue
            entry["gpu_memory_bytes"] += int(as_number(row.get("used_memory_bytes")))
            entry["gpu_util_percent"] += as_number(row.get("gpu_sm_percent_sum"))
            entry["gpu_process_count"] += int(as_number(row.get("process_count")))
            entry["gpu_indices"].update(str(value) for value in (row.get("gpu_indices") or []))

        for container in ((metrics.get("docker") or {}).get("containers") or []):
            if not container.get("running"):
                continue
            owner = clean_text(container.get("owner_user"), 64)
            entry = user_entry(owner)
            if entry is None:
                continue
            entry["container_count"] += 1
            if runtime_user_matches_owner(container, owner, entry):
                continue
            entry["cpu_percent"] += as_number(container.get("cpu_percent"))
            entry["memory_bytes"] += int(as_number(container.get("memory_used_bytes")))
            entry["gpu_memory_bytes"] += int(as_number(container.get("gpu_memory_used_bytes")))
            entry["process_count"] += int(as_number(container.get("pids")))
            entry["gpu_process_count"] += int(as_number(container.get("gpu_process_count")))
            entry["gpu_indices"].update(str(value) for value in (container.get("gpu_indices") or []))

        rows = []
        for entry in users.values():
            entry["cpu_percent"] = round(entry["cpu_percent"], 1)
            entry["gpu_util_percent"] = round(entry["gpu_util_percent"], 1)
            entry["gpu_indices"] = sorted(
                entry["gpu_indices"], key=lambda value: int(value) if str(value).isdigit() else str(value)
            )
            rows.append(entry)
        rows.sort(
            key=lambda item: (
                item.get("gpu_memory_bytes") or 0,
                item.get("memory_bytes") or 0,
                item.get("cpu_percent") or 0,
            ),
            reverse=True,
        )
        if rows:
            machines.append(
                {
                    "server_id": result.get("id"),
                    "server_name": result.get("name") or result.get("id"),
                    "host": result.get("host"),
                    "group": result.get("group"),
                    "users": rows,
                }
            )
    return machines


class HourlyUsageReporter:
    def __init__(
        self,
        client,
        interval_seconds=3600,
        excluded_users=None,
        max_users=80,
        dashboard_url=None,
        send_on_start=False,
        now_fn=None,
    ):
        self.client = client
        self.interval_seconds = max(300.0, float(interval_seconds))
        self.excluded_users = list(excluded_users or ["root", "nobody"])
        self.max_users = max(1, min(int(max_users), 80))
        self.dashboard_url = str(dashboard_url or "").strip()
        self.now_fn = now_fn or time.time
        now = float(self.now_fn())
        self.next_due_at = now if send_on_start else self._next_boundary(now)
        self.period_started_at = now
        self.aggregate = {}
        self.snapshot_samples = 0
        self.latest_counts = {"total": 0, "online": 0, "offline": 0}
        self.lock = threading.Lock()
        self.last_attempt_at = None
        self.last_success_at = None
        self.last_error = None
        self.sent_reports = 0

    def _next_boundary(self, now):
        return (math.floor(now / self.interval_seconds) + 1) * self.interval_seconds

    def process(self, snapshot, force=False):
        now = float(self.now_fn())
        current = current_user_usage(snapshot, self.excluded_users)
        with self.lock:
            self._observe(current, snapshot, now)
            if not force and now < self.next_due_at:
                return False
            self.last_attempt_at = utc_iso(now)
            try:
                payload = self.build_payload(now)
                self.client.send(payload)
            except Exception as exc:
                message = clean_text(exc, 300)
                webhook_url = getattr(self.client, "webhook_url", "")
                if webhook_url:
                    message = message.replace(webhook_url, "[redacted webhook]")
                self.last_error = message or "usage report delivery failed"
                return False
            self.last_success_at = utc_iso(now)
            self.last_error = None
            self.sent_reports += 1
            self.aggregate = {}
            self.snapshot_samples = 0
            self.period_started_at = now
            self.next_due_at = self._next_boundary(now)
            return True

    def _observe(self, machines, snapshot, now):
        results = snapshot.get("results") or []
        online = sum(1 for result in results if result.get("status") == "online")
        self.latest_counts = {"total": len(results), "online": online, "offline": len(results) - online}
        self.snapshot_samples += 1
        for machine in machines:
            for row in machine.get("users") or []:
                key = (machine.get("server_id"), row.get("user"))
                entry = self.aggregate.get(key)
                if entry is None:
                    entry = {
                        "server_id": machine.get("server_id"),
                        "server_name": machine.get("server_name"),
                        "host": machine.get("host"),
                        "group": machine.get("group"),
                        "user": row.get("user"),
                        "samples": 0,
                        "cpu_sum": 0.0,
                        "cpu_peak": 0.0,
                        "memory_peak_bytes": 0,
                        "gpu_memory_peak_bytes": 0,
                        "gpu_util_peak": 0.0,
                        "process_peak": 0,
                        "container_peak": 0,
                        "gpu_indices": set(),
                        "first_seen_at": now,
                        "last_seen_at": now,
                    }
                    self.aggregate[key] = entry
                cpu = as_number(row.get("cpu_percent"))
                entry["samples"] += 1
                entry["cpu_sum"] += cpu
                entry["cpu_peak"] = max(entry["cpu_peak"], cpu)
                entry["memory_peak_bytes"] = max(entry["memory_peak_bytes"], int(as_number(row.get("memory_bytes"))))
                entry["gpu_memory_peak_bytes"] = max(
                    entry["gpu_memory_peak_bytes"], int(as_number(row.get("gpu_memory_bytes")))
                )
                entry["gpu_util_peak"] = max(entry["gpu_util_peak"], as_number(row.get("gpu_util_percent")))
                entry["process_peak"] = max(entry["process_peak"], int(as_number(row.get("process_count"))))
                entry["container_peak"] = max(entry["container_peak"], int(as_number(row.get("container_count"))))
                entry["gpu_indices"].update(str(value) for value in (row.get("gpu_indices") or []))
                entry["last_seen_at"] = now

    def report_rows(self):
        rows = []
        for entry in self.aggregate.values():
            row = dict(entry)
            row["cpu_average"] = round(entry["cpu_sum"] / max(entry["samples"], 1), 1)
            row["gpu_indices"] = sorted(
                entry["gpu_indices"], key=lambda value: int(value) if str(value).isdigit() else str(value)
            )
            rows.append(row)
        rows.sort(
            key=lambda item: (
                clean_text(item.get("group"), 80),
                clean_text(item.get("server_name"), 120),
                -(item.get("gpu_memory_peak_bytes") or 0),
                -(item.get("memory_peak_bytes") or 0),
            )
        )
        return rows

    def build_payload(self, now):
        rows = self.report_rows()
        visible = rows[: self.max_users]
        by_machine = {}
        for row in visible:
            key = row.get("server_id")
            by_machine.setdefault(key, []).append(row)
        active_machines = len({row.get("server_id") for row in rows})
        unique_users = len({row.get("user") for row in rows})
        total_gpu = sum(row.get("gpu_memory_peak_bytes") or 0 for row in rows)
        total_memory = sum(row.get("memory_peak_bytes") or 0 for row in rows)
        idle = max(self.latest_counts.get("online", 0) - active_machines, 0)
        started = datetime.fromtimestamp(self.period_started_at, BEIJING_TIMEZONE).strftime("%m-%d %H:%M")
        finished = datetime.fromtimestamp(now, BEIJING_TIMEZONE).strftime("%m-%d %H:%M")
        overview = (
            "**%s - %s**\n"
            "采样 %d 次 · 在线 %d · 离线 %d · 有用户活动 %d · 空闲 %d\n"
            "普通用户 %d 名 · 用户/机器 %d 项 · 显存峰值合计 %s · 内存峰值合计 %s"
            % (
                started,
                finished,
                self.snapshot_samples,
                self.latest_counts.get("online", 0),
                self.latest_counts.get("offline", 0),
                active_machines,
                idle,
                unique_users,
                len(rows),
                format_bytes(total_gpu),
                format_bytes(total_memory),
            )
        )
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": overview}}, {"tag": "hr"}]
        if not rows:
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "本时段未检测到普通用户活跃进程。"},
                }
            )
        for machine_rows in by_machine.values():
            first = machine_rows[0]
            location = " · ".join(
                value for value in (clean_text(first.get("host"), 120), clean_text(first.get("group"), 80)) if value
            )
            lines = ["**%s**%s" % (clean_text(first.get("server_name"), 120), " · " + location if location else "")]
            for row in machine_rows:
                details = []
                if row.get("gpu_indices"):
                    details.append("GPU %s" % ",".join(row["gpu_indices"]))
                if row.get("gpu_memory_peak_bytes"):
                    details.append("显存峰值 %s" % format_bytes(row["gpu_memory_peak_bytes"]))
                details.append("内存峰值 %s" % format_bytes(row.get("memory_peak_bytes")))
                details.append("CPU 均值 %s" % format_percent(row.get("cpu_average")))
                details.append("进程峰值 %d" % int(row.get("process_peak") or 0))
                if row.get("container_peak"):
                    details.append("容器 %d" % int(row["container_peak"]))
                lines.append("• `%s` · %s" % (clean_text(row.get("user"), 64), " · ".join(details)))
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
            elements.append({"tag": "hr"})
        if len(rows) > len(visible):
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "另有 **%d** 项用户/机器记录未展开。" % (len(rows) - len(visible)),
                    },
                }
            )
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "按每分钟采样汇总；不含 root 和系统账号；容器按可识别归属用户合并。",
                    }
                ],
            }
        )
        if self.dashboard_url:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开监控面板"},
                            "url": self.dashboard_url,
                            "type": "primary",
                        }
                    ],
                }
            )
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": "每小时用户资源报告"}},
                "elements": elements,
            },
        }

    def record_runtime_error(self):
        with self.lock:
            self.last_attempt_at = utc_iso(float(self.now_fn()))
            self.last_error = "usage report processing failed"

    def status(self):
        with self.lock:
            return {
                "enabled": True,
                "provider": "feishu",
                "interval_seconds": self.interval_seconds,
                "excluded_users": self.excluded_users,
                "next_due_at": utc_iso(self.next_due_at),
                "period_started_at": utc_iso(self.period_started_at),
                "tracked_user_machines": len(self.aggregate),
                "snapshot_samples": self.snapshot_samples,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "sent_reports": self.sent_reports,
            }
