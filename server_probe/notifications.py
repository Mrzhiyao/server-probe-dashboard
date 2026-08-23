"""Alert notification policy and Feishu webhook delivery."""

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def utc_iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def clean_text(value, limit=240):
    text = " ".join(str(value or "").split())
    return text[:limit]


def alert_key(alert):
    parts = [
        alert.get("server_id"),
        alert.get("kind"),
        alert.get("path"),
        alert.get("device"),
        alert.get("container"),
        alert.get("metric"),
    ]
    return "|".join(clean_text(part, 160) for part in parts)


def duration_text(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return "%d 秒" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%d 分钟" % minutes
    hours = minutes // 60
    if hours < 48:
        return "%d 小时 %d 分钟" % (hours, minutes % 60)
    days = hours // 24
    return "%d 天 %d 小时" % (days, hours % 24)


def number_text(value):
    try:
        return ("%.1f" % float(value)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def alert_detail(alert):
    kind = alert.get("kind")
    value = number_text(alert.get("value"))
    threshold = number_text(alert.get("threshold"))
    if kind == "offline":
        return "设备离线或 SSH 采集失败"
    if kind in ("cpu", "memory", "gpu", "disk", "storage", "inode"):
        labels = {
            "cpu": "CPU",
            "memory": "内存",
            "gpu": "GPU",
            "disk": "根分区",
            "storage": alert.get("path") or "存储",
            "inode": "%s inode" % (alert.get("path") or "存储"),
        }
        detail = "%s %s%%" % (labels[kind], value)
        if threshold:
            detail += "（阈值 %s%%）" % threshold
        return detail
    if kind in ("mount", "mount_read_only"):
        messages = {
            "automatic mount placeholder is active but the real filesystem is not mounted": "只有自动挂载占位，真实文件系统未挂载",
            "network storage connection is unavailable": "网络存储连接不可用",
            "configured filesystem is not mounted": "配置的文件系统未挂载",
            "filesystem is unavailable": "文件系统不可用",
            "active filesystem did not respond before the timeout": "文件系统探测超时",
            "filesystem is mounted read-only": "文件系统变为只读",
        }
        message = messages.get(alert.get("message"), alert.get("message") or "挂载异常")
        return "%s · %s" % (alert.get("path") or "挂载点", clean_text(message))
    if kind == "mount_latency":
        detail = "%s 延迟 %s ms" % (alert.get("path") or "网络存储", value)
        if threshold:
            detail += "（阈值 %s ms）" % threshold
        return detail
    if kind == "smart":
        return "%s · %s" % (alert.get("device") or "磁盘", clean_text(alert.get("message") or "SMART 异常"))
    if kind in ("container_expected", "container_restarting", "container_health", "vllm"):
        messages = {
            "container_expected": "预期容器未运行",
            "container_restarting": "容器正在反复重启",
            "container_health": "Docker 健康检查异常",
            "vllm": "vLLM 服务接口不可用",
        }
        detail = "%s · %s" % (alert.get("container") or "容器", messages[kind])
        if alert.get("model"):
            detail += " · %s" % clean_text(alert.get("model"), 100)
        return detail
    metric = clean_text(alert.get("metric") or kind or "资源")
    if value:
        metric += " %s" % value
    return metric


class FeishuWebhookClient:
    def __init__(self, webhook_url, signing_secret=None, timeout=5, opener=None):
        self.webhook_url = str(webhook_url or "").strip()
        if not self.webhook_url.startswith("https://open.feishu.cn/open-apis/bot/"):
            raise ValueError("invalid Feishu webhook URL")
        self.signing_secret = str(signing_secret or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.opener = opener or urllib.request.urlopen

    def signature(self, timestamp):
        string_to_sign = "%s\n%s" % (timestamp, self.signing_secret)
        digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def send(self, payload):
        body = dict(payload)
        if self.signing_secret:
            timestamp = int(time.time())
            body["timestamp"] = timestamp
            body["sign"] = self.signature(timestamp)
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Feishu webhook returned HTTP %s" % exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Feishu webhook connection failed") from None
        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("Feishu webhook returned an invalid response") from None
        code = result.get("code", result.get("StatusCode", 0))
        if code not in (0, None):
            message = clean_text(result.get("msg") or result.get("StatusMessage") or "request rejected", 160)
            raise RuntimeError("Feishu webhook rejected the message (%s): %s" % (code, message))
        return result


class AlertNotificationManager:
    def __init__(
        self,
        client,
        critical_consecutive=2,
        warning_after_seconds=300,
        cooldown_seconds=1800,
        recovery_enabled=True,
        max_items=20,
        dashboard_url=None,
        now_fn=None,
    ):
        self.client = client
        self.critical_consecutive = max(1, int(critical_consecutive))
        self.warning_after_seconds = max(0.0, float(warning_after_seconds))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.recovery_enabled = bool(recovery_enabled)
        self.max_items = max(1, min(int(max_items), 40))
        self.dashboard_url = str(dashboard_url or "").strip()
        self.now_fn = now_fn or time.time
        self.states = {}
        self.lock = threading.Lock()
        self.last_attempt_at = None
        self.last_success_at = None
        self.last_error = None
        self.sent_batches = 0
        self.sent_events = 0

    def process(self, alerts, server_statuses=None):
        now = float(self.now_fn())
        server_statuses = server_statuses or {}
        current = {}
        for alert in alerts or []:
            key = alert_key(alert)
            previous = current.get(key)
            if previous is None or (alert.get("severity") == "critical" and previous.get("severity") != "critical"):
                current[key] = dict(alert)

        with self.lock:
            events = []
            for key, alert in current.items():
                state = self.states.get(key)
                if state is None:
                    state = {
                        "server_id": alert.get("server_id"),
                        "kind": alert.get("kind"),
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "critical_consecutive": 0,
                        "notified": False,
                        "notified_severity": None,
                        "last_notified_at": None,
                        "alert": alert,
                    }
                    self.states[key] = state
                state["last_seen_at"] = now
                state["alert"] = alert
                severity = alert.get("severity")
                if severity == "critical":
                    state["critical_consecutive"] += 1
                else:
                    state["critical_consecutive"] = 0

                if not state["notified"]:
                    critical_ready = severity == "critical" and state["critical_consecutive"] >= self.critical_consecutive
                    warning_ready = severity != "critical" and now - state["first_seen_at"] >= self.warning_after_seconds
                    if critical_ready or warning_ready:
                        events.append({"action": "triggered", "key": key, "alert": alert, "state": state})
                    continue

                if severity == "critical" and state.get("notified_severity") != "critical":
                    if state["critical_consecutive"] >= self.critical_consecutive:
                        events.append({"action": "escalated", "key": key, "alert": alert, "state": state})
                    continue
                last_notified = state.get("last_notified_at") or now
                if now - last_notified >= self.cooldown_seconds:
                    events.append({"action": "reminder", "key": key, "alert": alert, "state": state})

            for key in list(self.states):
                if key in current:
                    continue
                state = self.states[key]
                server_status = server_statuses.get(state.get("server_id"))
                if state.get("kind") != "offline" and server_status and server_status != "online":
                    continue
                if state.get("notified") and self.recovery_enabled:
                    events.append(
                        {"action": "recovered", "key": key, "alert": state.get("alert") or {}, "state": state}
                    )
                else:
                    self.states.pop(key, None)

            if not events:
                return []

            self.last_attempt_at = utc_iso(now)
            try:
                payload = self.build_payload(events, now)
                self.client.send(payload)
            except Exception as exc:
                message = clean_text(exc, 300)
                webhook_url = getattr(self.client, "webhook_url", "")
                if webhook_url:
                    message = message.replace(webhook_url, "[redacted webhook]")
                self.last_error = message or "notification delivery failed"
                return []

            for event in events:
                state = event["state"]
                if event["action"] == "recovered":
                    self.states.pop(event["key"], None)
                    continue
                state["notified"] = True
                state["notified_severity"] = event["alert"].get("severity")
                state["last_notified_at"] = now
            self.last_success_at = utc_iso(now)
            self.last_error = None
            self.sent_batches += 1
            self.sent_events += len(events)
            return [{"action": event["action"], "key": event["key"]} for event in events]

    def record_runtime_error(self):
        with self.lock:
            self.last_attempt_at = utc_iso(float(self.now_fn()))
            self.last_error = "notification processing failed"

    def build_payload(self, events, now):
        active = [event for event in events if event["action"] != "recovered"]
        has_critical = any(event["alert"].get("severity") == "critical" for event in active)
        if has_critical:
            title = "设备监控严重告警"
            template = "red"
        elif active:
            title = "设备监控告警"
            template = "orange"
        else:
            title = "设备监控告警恢复"
            template = "green"

        elements = []
        visible_events = events[: self.max_items]
        for event in visible_events:
            alert = event["alert"]
            state = event["state"]
            action = event["action"]
            severity = alert.get("severity")
            labels = {
                "triggered": "严重告警" if severity == "critical" else "告警",
                "escalated": "升级为严重告警",
                "reminder": "持续严重告警" if severity == "critical" else "持续告警",
                "recovered": "告警已恢复",
            }
            server = clean_text(alert.get("server_name") or alert.get("server_id") or "未知设备", 120)
            host = clean_text(alert.get("host"), 120)
            group = clean_text(alert.get("group"), 80)
            location = " · ".join(value for value in (host, group) if value)
            duration = duration_text(now - state.get("first_seen_at", now))
            lines = ["**[%s] %s**" % (labels[action], server)]
            if location:
                lines.append(location)
            lines.append("%s · 持续 %s" % (alert_detail(alert), duration))
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
            elements.append({"tag": "hr"})

        if len(events) > len(visible_events):
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "另有 **%d** 项状态变化未在本卡片展开。" % (len(events) - len(visible_events)),
                    },
                }
            )
        timestamp = datetime.fromtimestamp(now, BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "北京时间 %s · 告警冷却 %s" % (timestamp, duration_text(self.cooldown_seconds))}],
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
                "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
                "elements": elements,
            },
        }

    def status(self):
        with self.lock:
            return {
                "enabled": True,
                "provider": "feishu",
                "critical_consecutive": self.critical_consecutive,
                "warning_after_seconds": self.warning_after_seconds,
                "cooldown_seconds": self.cooldown_seconds,
                "recovery_enabled": self.recovery_enabled,
                "tracked_alerts": len(self.states),
                "notified_alerts": sum(1 for state in self.states.values() if state.get("notified")),
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "sent_batches": self.sent_batches,
                "sent_events": self.sent_events,
            }
