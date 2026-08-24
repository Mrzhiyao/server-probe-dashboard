#!/usr/bin/env python3
"""Feishu long-connection bot for read-only resource queries."""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from server_probe.auth import AuthStore
from server_probe.usage_reports import current_user_usage, format_bytes, format_percent


HELP_TEXT = """设备资源助手支持以下查询：
查人 崔涵帅
查机 gpu010
空闲GPU
帮助

当前仅提供只读查询，账号申请、审批和开通功能尚未启用。"""


def clean_command(text):
    value = str(text or "")
    value = re.sub(r"@_user_\d+\s*", "", value)
    value = value.replace("@设备资源助手", "")
    return " ".join(value.strip().split())


def parse_command(text):
    value = clean_command(text)
    compact = value.replace(" ", "").lower()
    if compact in ("", "帮助", "help", "?", "？"):
        return "help", ""
    if compact in ("空闲gpu", "空闲显卡", "空闲卡", "可用gpu"):
        return "idle_gpu", ""
    for prefix, command in (("查人", "person"), ("查询用户", "person"), ("查机", "machine"), ("查询机器", "machine")):
        if value.startswith(prefix):
            return command, value[len(prefix) :].strip(" ：:")
    if any(word in compact for word in ("开账号", "开通账号", "创建账号", "申请账号", "审批", "通过申请", "删除账号")):
        return "admin_disabled", ""
    return "help", ""


def as_number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class DashboardAPI:
    def __init__(self, dsn, base_url="http://127.0.0.1:8088"):
        self.store = AuthStore(dsn, session_hours=1)
        self.base_url = base_url.rstrip("/")

    def get(self, path):
        admin = self.store.get_user_by_username("admin")
        if not admin:
            raise RuntimeError("dashboard admin account is unavailable")
        token, _ = self.store.create_session(admin["id"], ip_address="127.0.0.1", user_agent="feishu-bot")
        try:
            request = urllib.request.Request(
                self.base_url + path,
                headers={"Cookie": "probe_session=" + token, "User-Agent": "server-probe-feishu-bot"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError("dashboard API returned HTTP %s" % exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("dashboard API is unavailable") from None
        finally:
            self.store.destroy_session(token)

    def snapshot(self):
        return self.get("/api/snapshot") or {}

    def person(self, name):
        return self.get("/api/user-usage?person=" + urllib.parse.quote(name, safe=""))


class MessageDeduplicator:
    def __init__(self, limit=1000, ttl_seconds=3600):
        self.limit = int(limit)
        self.ttl_seconds = float(ttl_seconds)
        self.values = OrderedDict()
        self.lock = threading.Lock()

    def accept(self, message_id):
        if not message_id:
            return True
        now = time.time()
        with self.lock:
            for key in list(self.values):
                if now - self.values[key] <= self.ttl_seconds:
                    break
                self.values.pop(key, None)
            if message_id in self.values:
                return False
            self.values[message_id] = now
            while len(self.values) > self.limit:
                self.values.popitem(last=False)
            return True


def person_text(payload):
    person = (payload or {}).get("person") or {}
    if not person:
        return "没有找到该用户的当前资源记录。"
    display = person.get("display_name") or ", ".join(person.get("usernames") or []) or "未登记用户"
    rows = person.get("machine_rows") or []
    lines = ["%s：当前使用 %d 台机器" % (display, len(rows))]
    for row in rows[:10]:
        gpu = ",".join(row.get("gpu_indices") or [])
        summary = "%s（%s）" % (row.get("server_name") or row.get("server_id"), row.get("user") or "-")
        resources = ["内存 %s" % format_bytes(row.get("memory_bytes"))]
        if row.get("gpu_memory_bytes"):
            resources.insert(0, "GPU %s" % (gpu or "-"))
            resources.insert(1, "显存 %s" % format_bytes(row.get("gpu_memory_bytes")))
        resources.append("CPU %s" % format_percent(row.get("cpu_percent")))
        lines.append("\n%s\n%s" % (summary, " · ".join(resources)))
        top = (row.get("top_gpu_processes") or [None])[0]
        if top:
            label = top.get("container_name") or top.get("model") or top.get("process_name") or "unknown"
            kind = "容器" if top.get("container_name") else "进程"
            lines.append(
                "最高显存%s：%s · PID %s · %s"
                % (kind, label, top.get("pid") or "-", format_bytes(top.get("used_memory_bytes")))
            )
    if len(rows) > 10:
        lines.append("\n另有 %d 台机器未展开。" % (len(rows) - 10))
    return "\n".join(lines)[:3900]


def machine_text(snapshot, reference):
    query = str(reference or "").strip().lower()
    if not query:
        return "请指定机器，例如：查机 gpu010"
    matches = []
    for result in snapshot.get("results") or []:
        values = [result.get("id"), result.get("name"), result.get("host")]
        if any(query in str(value or "").lower() for value in values):
            matches.append(result)
    if not matches:
        return "没有找到机器：%s" % reference
    lines = []
    for result in matches[:5]:
        title = result.get("name") or result.get("id")
        if result.get("status") != "online":
            lines.append("%s：离线或采集失败" % title)
            continue
        metrics = result.get("metrics") or {}
        lines.append(
            "%s（%s）\nCPU %s · 内存 %s"
            % (
                title,
                result.get("host") or "-",
                format_percent((metrics.get("cpu") or {}).get("percent")),
                format_percent((metrics.get("memory") or {}).get("percent")),
            )
        )
        for device in ((metrics.get("gpu") or {}).get("devices") or [])[:8]:
            total = as_number(device.get("memory_total_bytes"))
            used = as_number(device.get("memory_used_bytes"))
            lines.append(
                "GPU %s · %s · 算力 %s · 显存 %s / %s"
                % (
                    device.get("index"),
                    device.get("name") or "GPU",
                    format_percent(device.get("utilization_percent")),
                    format_bytes(used),
                    format_bytes(total),
                )
            )
    if len(matches) > 5:
        lines.append("另有 %d 个匹配结果。" % (len(matches) - 5))
    return "\n\n".join(lines)[:3900]


def idle_gpu_text(snapshot):
    rows = []
    for result in snapshot.get("results") or []:
        if result.get("status") != "online":
            continue
        for device in ((((result.get("metrics") or {}).get("gpu") or {}).get("devices")) or []):
            total = as_number(device.get("memory_total_bytes"))
            used = as_number(device.get("memory_used_bytes"))
            rows.append(
                {
                    "server": result.get("name") or result.get("id"),
                    "host": result.get("host"),
                    "index": device.get("index"),
                    "name": device.get("name") or "GPU",
                    "free": max(total - used, 0),
                    "total": total,
                    "util": as_number(device.get("utilization_percent")),
                }
            )
    rows.sort(key=lambda item: (item["free"], -item["util"]), reverse=True)
    if not rows:
        return "当前没有可用的 GPU 数据。"
    lines = ["空闲 GPU（按可用显存排序）："]
    for row in rows[:15]:
        lines.append(
            "%s · GPU %s · %s\n空闲 %s / %s · 算力 %s"
            % (
                row["server"],
                row["index"],
                row["name"],
                format_bytes(row["free"]),
                format_bytes(row["total"]),
                format_percent(row["util"]),
            )
        )
    return "\n\n".join(lines)[:3900]


class FeishuResourceBot:
    def __init__(self, app_id, app_secret, dashboard_api):
        import lark_oapi as lark

        self.lark = lark
        self.dashboard = dashboard_api
        self.deduplicator = MessageDeduplicator()
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        self.handler = (
            lark.EventDispatcherHandler.builder("", "", lark.LogLevel.WARNING)
            .register_p2_im_message_receive_v1(self.on_message)
            .build()
        )
        self.ws_client = lark.ws.Client(
            app_id,
            app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=self.handler,
            auto_reconnect=True,
            source="server-probe-dashboard",
        )

    def reply(self, message_id, text):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": str(text)[:3900]}, ensure_ascii=False))
            .build()
        )
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError("Feishu reply failed with code %s" % response.code)

    def event_text(self, data):
        message = data.event.message
        if getattr(message, "message_type", "") != "text":
            return ""
        try:
            payload = json.loads(message.content or "{}")
        except json.JSONDecodeError:
            return ""
        return payload.get("text") or ""

    def on_message(self, data):
        message = data.event.message
        message_id = getattr(message, "message_id", "")
        if not self.deduplicator.accept(message_id):
            return
        try:
            command, argument = parse_command(self.event_text(data))
            if command == "person":
                if not argument:
                    answer = "请指定姓名或账号，例如：查人 崔涵帅"
                else:
                    answer = person_text(self.dashboard.person(argument))
            elif command == "machine":
                answer = machine_text(self.dashboard.snapshot(), argument)
            elif command == "idle_gpu":
                answer = idle_gpu_text(self.dashboard.snapshot())
            elif command == "admin_disabled":
                answer = "账号管理命令尚未开放。当前机器人只提供只读资源查询。"
            else:
                answer = HELP_TEXT
        except Exception as exc:
            print("query failed: %s" % type(exc).__name__, flush=True)
            answer = "查询暂时失败，请稍后重试。"
        try:
            self.reply(message_id, answer)
        except Exception as exc:
            print("reply failed: %s" % type(exc).__name__, flush=True)

    def run(self):
        print("Feishu resource bot long connection starting", flush=True)
        self.ws_client.start()


def main():
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    dsn = os.getenv("PROBE_AUTH_DB_DSN", "").strip()
    if not app_id or not app_secret or not dsn:
        raise RuntimeError("FEISHU_APP_ID, FEISHU_APP_SECRET, and PROBE_AUTH_DB_DSN are required")
    FeishuResourceBot(app_id, app_secret, DashboardAPI(dsn)).run()


if __name__ == "__main__":
    main()
