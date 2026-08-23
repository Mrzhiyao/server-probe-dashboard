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


def usage_ratio(value, total):
    total = as_number(total)
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, as_number(value) * 100.0 / total))


def usage_bar(value, total, width=10):
    if as_number(total) <= 0:
        return "<font color='grey'>容量未知</font>"
    percent = usage_ratio(value, total)
    filled = int(round(percent * width / 100.0))
    if value and filled == 0:
        filled = 1
    color = "green" if percent < 50 else "orange" if percent < 80 else "red"
    return "<font color='%s'>%s</font><font color='grey'>%s</font> %s" % (
        color,
        "■" * filled,
        "□" * (width - filled),
        format_percent(percent),
    )


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
        gpu_devices = ((metrics.get("gpu") or {}).get("devices") or [])
        gpu_capacity_by_index = {
            str(device.get("index")): int(as_number(device.get("memory_total_bytes")))
            for device in gpu_devices
            if device.get("index") not in (None, "")
        }
        gpu_memory_total = sum(gpu_capacity_by_index.values())

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
            entry["gpu_memory_capacity_bytes"] = sum(
                gpu_capacity_by_index.get(str(index), 0) for index in entry["gpu_indices"]
            )
            if entry["gpu_memory_bytes"] and not entry["gpu_memory_capacity_bytes"]:
                entry["gpu_memory_capacity_bytes"] = gpu_memory_total
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
                    "memory_total_bytes": int(as_number((metrics.get("memory") or {}).get("total_bytes"))),
                    "gpu_memory_total_bytes": gpu_memory_total,
                    "users": rows,
                }
            )
    return machines


def group_usage_rows(rows, identities):
    people = {}
    for row in rows:
        username = clean_text(row.get("user"), 64)
        display_name = identities.get(username.lower())
        person_key = "name:%s" % display_name.casefold() if display_name else "account:%s" % username.lower()
        person = people.setdefault(
            person_key,
            {
                "person_key": person_key,
                "display_name": display_name,
                "identity_known": bool(display_name),
                "usernames": set(),
                "machine_rows": [],
                "machine_ids": set(),
                "gpu_memory_peak_bytes": 0,
                "memory_peak_bytes": 0,
            },
        )
        person["usernames"].add(username)
        person["machine_rows"].append(row)
        person["machine_ids"].add(row.get("server_id"))
        person["gpu_memory_peak_bytes"] += int(as_number(row.get("gpu_memory_peak_bytes")))
        person["memory_peak_bytes"] += int(as_number(row.get("memory_peak_bytes")))

    result = []
    for person in people.values():
        person["usernames"] = sorted(person["usernames"])
        person["machine_count"] = len(person.pop("machine_ids"))
        person["machine_rows"].sort(
            key=lambda row: (
                -(row.get("gpu_memory_peak_bytes") or 0),
                -(row.get("memory_peak_bytes") or 0),
                clean_text(row.get("server_name"), 120),
            )
        )
        result.append(person)
    return result


class HourlyUsageReporter:
    def __init__(
        self,
        client,
        interval_seconds=3600,
        excluded_users=None,
        max_users=80,
        detail_users=12,
        dashboard_url=None,
        send_on_start=False,
        identity_provider=None,
        now_fn=None,
    ):
        self.client = client
        self.interval_seconds = max(300.0, float(interval_seconds))
        self.excluded_users = list(excluded_users or ["root", "nobody"])
        self.max_users = max(1, min(int(max_users), 80))
        self.detail_users = max(4, min(int(detail_users), 30))
        self.dashboard_url = str(dashboard_url or "").strip()
        self.identity_provider = identity_provider
        self.now_fn = now_fn or time.time
        now = float(self.now_fn())
        self.next_due_at = now if send_on_start else self._next_boundary(now)
        self.period_started_at = now
        self.aggregate = {}
        self.snapshot_samples = 0
        self.latest_counts = {"total": 0, "online": 0, "offline": 0}
        self.latest_capacity = {"memory_bytes": 0, "gpu_memory_bytes": 0}
        self.period_peaks = {
            "memory_bytes": 0,
            "gpu_memory_bytes": 0,
            "active_machines": 0,
            "active_users": 0,
            "gpu_users": 0,
        }
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
            self.period_peaks = {
                "memory_bytes": 0,
                "gpu_memory_bytes": 0,
                "active_machines": 0,
                "active_users": 0,
                "gpu_users": 0,
            }
            self.next_due_at = self._next_boundary(now)
            return True

    def _observe(self, machines, snapshot, now):
        results = snapshot.get("results") or []
        online = sum(1 for result in results if result.get("status") == "online")
        self.latest_counts = {"total": len(results), "online": online, "offline": len(results) - online}
        self.latest_capacity = {
            "memory_bytes": sum(
                int(as_number(((result.get("metrics") or {}).get("memory") or {}).get("total_bytes")))
                for result in results
                if result.get("status") == "online"
            ),
            "gpu_memory_bytes": sum(
                int(as_number(device.get("memory_total_bytes")))
                for result in results
                if result.get("status") == "online"
                for device in ((((result.get("metrics") or {}).get("gpu") or {}).get("devices")) or [])
            ),
        }
        current_rows = [row for machine in machines for row in (machine.get("users") or [])]
        current_users = {row.get("user") for row in current_rows if row.get("user")}
        self.period_peaks["memory_bytes"] = max(
            self.period_peaks["memory_bytes"], sum(int(as_number(row.get("memory_bytes"))) for row in current_rows)
        )
        self.period_peaks["gpu_memory_bytes"] = max(
            self.period_peaks["gpu_memory_bytes"],
            sum(int(as_number(row.get("gpu_memory_bytes"))) for row in current_rows),
        )
        self.period_peaks["active_machines"] = max(self.period_peaks["active_machines"], len(machines))
        self.period_peaks["active_users"] = max(self.period_peaks["active_users"], len(current_users))
        self.period_peaks["gpu_users"] = max(
            self.period_peaks["gpu_users"], sum(1 for row in current_rows if row.get("gpu_memory_bytes"))
        )
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
                        "memory_total_bytes": machine.get("memory_total_bytes") or 0,
                        "gpu_memory_total_bytes": machine.get("gpu_memory_total_bytes") or 0,
                        "gpu_memory_capacity_bytes": row.get("gpu_memory_capacity_bytes") or 0,
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
                entry["gpu_memory_capacity_bytes"] = max(
                    entry["gpu_memory_capacity_bytes"], int(as_number(row.get("gpu_memory_capacity_bytes")))
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
        rows = self.report_rows()[: self.max_users]
        try:
            identities = self.identity_provider() if self.identity_provider else {}
        except Exception:
            identities = {}
        identities = {
            str(username).strip().lower(): clean_text(display_name, 80)
            for username, display_name in (identities or {}).items()
            if str(username).strip() and clean_text(display_name, 80)
        }
        people = group_usage_rows(rows, identities)
        gpu_people = sorted(
            [person for person in people if person.get("gpu_memory_peak_bytes")],
            key=lambda person: (
                person.get("gpu_memory_peak_bytes") or 0,
                person.get("memory_peak_bytes") or 0,
            ),
            reverse=True,
        )
        gpu_slots = max(1, int(round(self.detail_users * 2.0 / 3.0)))
        selected_gpu = gpu_people[:gpu_slots]
        selected_keys = {person.get("person_key") for person in selected_gpu}
        remaining_slots = max(self.detail_users - len(selected_gpu), 0)
        other_people = sorted(
            [
                person
                for person in people
                if not person.get("gpu_memory_peak_bytes") and person.get("person_key") not in selected_keys
            ],
            key=lambda person: person.get("memory_peak_bytes") or 0,
            reverse=True,
        )
        selected_other = other_people[:remaining_slots]
        selected = selected_gpu + selected_other
        known_names = {person.get("display_name") for person in people if person.get("identity_known")}
        started = datetime.fromtimestamp(self.period_started_at, BEIJING_TIMEZONE).strftime("%m-%d %H:%M")
        finished = datetime.fromtimestamp(now, BEIJING_TIMEZONE).strftime("%m-%d %H:%M")

        def metric_column(value, label, color="blue"):
            return {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "<font color='%s'>**%s**</font>\n%s" % (color, value, label),
                        },
                    }
                ],
            }

        def detail_element(person):
            display_name = person.get("display_name")
            if display_name:
                identity = "**%s**" % display_name
            else:
                identity = "**%s**\n<font color='grey'>姓名未登记</font>" % (person.get("usernames") or ["-"])[0]
            left_lines = [
                identity,
                "<font color='grey'>%d 个账号 · %d 台机器</font>"
                % (len(person.get("usernames") or []), int(person.get("machine_count") or 0)),
            ]
            if person.get("gpu_memory_peak_bytes"):
                left_lines.append("显存峰值合计 %s" % format_bytes(person.get("gpu_memory_peak_bytes")))
            left_lines.append("内存峰值合计 %s" % format_bytes(person.get("memory_peak_bytes")))
            machine_blocks = []
            for row in person.get("machine_rows") or []:
                username = clean_text(row.get("user"), 64)
                machine = clean_text(row.get("server_name"), 120)
                gpu_indices = ",".join(row.get("gpu_indices") or [])
                machine_lines = [
                    "**%s** · `%s`%s"
                    % (machine, username, " · GPU " + gpu_indices if gpu_indices else "")
                ]
                if row.get("gpu_memory_peak_bytes"):
                    machine_lines.extend(
                        [
                            "显存 %s / %s"
                            % (
                                format_bytes(row.get("gpu_memory_peak_bytes")),
                                format_bytes(row.get("gpu_memory_capacity_bytes")),
                            ),
                            usage_bar(row.get("gpu_memory_peak_bytes"), row.get("gpu_memory_capacity_bytes")),
                        ]
                    )
                machine_lines.extend(
                    [
                        "内存 %s / %s"
                        % (format_bytes(row.get("memory_peak_bytes")), format_bytes(row.get("memory_total_bytes"))),
                        usage_bar(row.get("memory_peak_bytes"), row.get("memory_total_bytes")),
                        "<font color='grey'>CPU 均值 %s · 进程峰值 %d%s</font>"
                        % (
                            format_percent(row.get("cpu_average")),
                            int(row.get("process_peak") or 0),
                            " · 容器 %d" % int(row.get("container_peak")) if row.get("container_peak") else "",
                        ),
                    ]
                )
                machine_blocks.append("\n".join(machine_lines))
            return {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "vertical_align": "top",
                        "elements": [
                            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(left_lines)}}
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 5,
                        "vertical_align": "top",
                        "elements": [
                            {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(machine_blocks)}}
                        ],
                    },
                ],
            }

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**%s - %s** · 每分钟采样 %d 次" % (started, finished, self.snapshot_samples),
                },
            },
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey",
                "columns": [
                    metric_column(self.period_peaks.get("active_machines", 0), "活跃机器", "blue"),
                    metric_column(len(people), "活跃人员", "turquoise"),
                    metric_column(len(gpu_people), "GPU 人员", "purple"),
                    metric_column(self.latest_counts.get("offline", 0), "离线设备", "red"),
                ],
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**普通用户资源峰值**\n"
                        "内存 %s / %s\n%s\n"
                        "显存 %s / %s\n%s"
                        % (
                            format_bytes(self.period_peaks.get("memory_bytes")),
                            format_bytes(self.latest_capacity.get("memory_bytes")),
                            usage_bar(
                                self.period_peaks.get("memory_bytes"), self.latest_capacity.get("memory_bytes"), 14
                            ),
                            format_bytes(self.period_peaks.get("gpu_memory_bytes")),
                            format_bytes(self.latest_capacity.get("gpu_memory_bytes")),
                            usage_bar(
                                self.period_peaks.get("gpu_memory_bytes"),
                                self.latest_capacity.get("gpu_memory_bytes"),
                                14,
                            ),
                        )
                    ),
                },
            },
            {"tag": "hr"},
        ]
        if not rows:
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "本时段未检测到普通用户活跃进程。"},
                }
            )
        if selected_gpu:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**GPU 使用重点** · 按姓名合并，展示 %d / %d 人" % (len(selected_gpu), len(gpu_people)),
                    },
                }
            )
            elements.extend(detail_element(person) for person in selected_gpu)
        if selected_other:
            elements.extend(
                [
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "**内存重点占用** · 未使用 GPU 的高占用账号"},
                    },
                ]
            )
            elements.extend(detail_element(person) for person in selected_other)
        if len(people) > len(selected):
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "<font color='grey'>其余 %d 人未展开；完整的 %d 条用户/机器记录仍保留在监控数据中。</font>"
                        % (len(people) - len(selected), len(rows)),
                    },
                }
            )
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "姓名已匹配 %d 人；账号名保留用于核对。不含 root 和系统账号，容器按可识别归属用户合并。"
                        % len(known_names),
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
                "header": {
                    "template": "turquoise",
                    "title": {"tag": "plain_text", "content": "每小时用户资源概览"},
                },
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
                "detail_users": self.detail_users,
                "identity_mapping": self.identity_provider is not None,
                "next_due_at": utc_iso(self.next_due_at),
                "period_started_at": utc_iso(self.period_started_at),
                "tracked_user_machines": len(self.aggregate),
                "snapshot_samples": self.snapshot_samples,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "sent_reports": self.sent_reports,
            }
