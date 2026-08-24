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

ALLOWED_INTENTS = {"help", "idle_gpu", "person", "machine", "admin_disabled"}


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
    return "unknown", ""


def as_number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class LLMIntentRouter:
    def __init__(self, base_url="", api_key="", model="", timeout=8, opener=None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout = max(2.0, float(timeout))
        self.opener = opener or urllib.request.urlopen
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.base_url and self.model)

    def endpoint(self):
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def route(self, text):
        deterministic = parse_command(text)
        if deterministic[0] != "unknown":
            return deterministic
        if not self.enabled:
            return "help", ""
        key = clean_command(text).lower()[:300]
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        try:
            result = self.classify(key)
        except Exception:
            return "help", ""
        with self.lock:
            self.cache[key] = result
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return result

    def classify(self, text):
        system_prompt = (
            "你是设备监控机器人的意图分类器。只输出一个 JSON 对象，不要解释。"
            "intent 只能是 help、idle_gpu、person、machine、admin_disabled。"
            "查询某个人当前资源用 person，argument 填姓名或账号；查询某台机器用 machine，argument 填机器名或IP；"
            "查询空闲显卡用 idle_gpu；任何申请、审批、创建、删除、改密码等写操作必须用 admin_disabled；其余用 help。"
            "格式为 {\"intent\":\"person\",\"argument\":\"崔涵帅\"}。"
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": str(text)[:300]},
            ],
            "temperature": 0,
            "max_tokens": 120,
        }
        headers = {"Content-Type": "application/json", "User-Agent": "server-probe-feishu-bot"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(
            self.endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self.opener(request, timeout=self.timeout) as response:
            payload = json.load(response)
        content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in content)
        match = re.search(r"\{.*\}", str(content), re.S)
        if not match:
            raise ValueError("LLM did not return JSON")
        parsed = json.loads(match.group(0))
        intent = str(parsed.get("intent") or "help").strip()
        argument = " ".join(str(parsed.get("argument") or "").strip().split())[:100]
        if intent not in ALLOWED_INTENTS:
            return "help", ""
        if intent in ("person", "machine") and not argument:
            return "help", ""
        return intent, argument


def compact_card(title, summary, sections=None, template="turquoise"):
    elements = []
    if summary:
        elements.append({"tag": "markdown", "content": str(summary)[:3000], "text_size": "normal"})
    for index, section in enumerate(sections or []):
        elements.append(
            {
                "tag": "collapsible_panel",
                "element_id": "section_%d" % index,
                "expanded": False,
                "header": {
                    "title": {"tag": "plain_text", "content": str(section.get("title") or "详情")[:120]},
                    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "border": {"color": "grey", "corner_radius": "5px"},
                "elements": [
                    {
                        "tag": "markdown",
                        "content": str(section.get("content") or "暂无详情")[:5000],
                        "text_size": "normal",
                    }
                ],
            }
        )
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": str(title)[:120]}},
        "body": {"direction": "vertical", "padding": "12px 12px 12px 12px", "elements": elements},
    }


def person_card(payload):
    person = (payload or {}).get("person") or {}
    if not person:
        return compact_card("未找到用户", "没有找到该用户的当前资源记录。", template="grey")
    display = person.get("display_name") or ", ".join(person.get("usernames") or []) or "未登记用户"
    rows = person.get("machine_rows") or []
    total_gpu = sum(as_number(row.get("gpu_memory_bytes")) for row in rows)
    total_memory = sum(as_number(row.get("memory_bytes")) for row in rows)
    summary = "**%s** · %d 台机器\n显存 **%s** · 内存 **%s**" % (
        display,
        len(rows),
        format_bytes(total_gpu),
        format_bytes(total_memory),
    )
    sections = []
    for row in rows[:12]:
        gpu = ",".join(row.get("gpu_indices") or [])
        title = "%s · %s" % (row.get("server_name") or row.get("server_id"), row.get("user") or "-")
        if row.get("gpu_memory_bytes"):
            title += " · GPU %s" % (gpu or "-")
        lines = [
            "CPU **%s** · 内存 **%s** · 显存 **%s**"
            % (
                format_percent(row.get("cpu_percent")),
                format_bytes(row.get("memory_bytes")),
                format_bytes(row.get("gpu_memory_bytes")),
            )
        ]
        processes = row.get("top_gpu_processes") or []
        if processes:
            lines.append("\n**显存进程（降序）**")
            for process in processes[:5]:
                label = process.get("container_name") or process.get("model") or process.get("process_name") or "unknown"
                kind = "容器" if process.get("container_name") else "进程"
                lines.append(
                    "- %s `%s` · PID %s · GPU %s · **%s**"
                    % (
                        kind,
                        label,
                        process.get("pid") or "-",
                        ",".join(process.get("gpu_indices") or []) or "-",
                        format_bytes(process.get("used_memory_bytes")),
                    )
                )
        else:
            lines.append("\n当前没有可识别的 GPU 进程。")
        sections.append({"title": title, "content": "\n".join(lines)})
    return compact_card("用户资源 · %s" % display, summary, sections)


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


def machine_card(snapshot, reference):
    query = str(reference or "").strip().lower()
    if not query:
        return compact_card("缺少机器", "请指定机器，例如：`查机 gpu010`", template="orange")
    matches = []
    for result in snapshot.get("results") or []:
        values = [result.get("id"), result.get("name"), result.get("host")]
        if any(query in str(value or "").lower() for value in values):
            matches.append(result)
    if not matches:
        return compact_card("未找到机器", "没有找到机器：`%s`" % reference, template="grey")
    sections = []
    online = 0
    for result in matches[:8]:
        title = result.get("name") or result.get("id")
        if result.get("status") != "online":
            sections.append({"title": title + " · 离线", "content": "设备离线或采集失败。"})
            continue
        online += 1
        metrics = result.get("metrics") or {}
        lines = [
            "CPU **%s** · 内存 **%s**"
            % (
                format_percent((metrics.get("cpu") or {}).get("percent")),
                format_percent((metrics.get("memory") or {}).get("percent")),
            )
        ]
        devices = ((metrics.get("gpu") or {}).get("devices") or [])
        for device in devices[:8]:
            lines.append(
                "- GPU %s · %s · 算力 %s · 显存 **%s / %s**"
                % (
                    device.get("index"),
                    device.get("name") or "GPU",
                    format_percent(device.get("utilization_percent")),
                    format_bytes(device.get("memory_used_bytes")),
                    format_bytes(device.get("memory_total_bytes")),
                )
            )
        sections.append({"title": "%s · %s" % (title, result.get("host") or "-"), "content": "\n".join(lines)})
    return compact_card(
        "机器查询 · %s" % reference,
        "匹配 **%d** 台 · 在线 **%d** 台，点击机器展开 GPU 详情。" % (len(matches), online),
        sections,
        template="blue",
    )


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


def idle_gpu_card(snapshot):
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
                    "index": device.get("index"),
                    "name": device.get("name") or "GPU",
                    "free": max(total - used, 0),
                    "total": total,
                    "util": as_number(device.get("utilization_percent")),
                }
            )
    rows.sort(key=lambda item: (item["free"], -item["util"]), reverse=True)
    if not rows:
        return compact_card("空闲 GPU", "当前没有可用的 GPU 数据。", template="grey")
    groups = OrderedDict()
    for row in rows[:30]:
        groups.setdefault(row["server"], []).append(row)
    sections = []
    for server, devices in groups.items():
        content = []
        for row in devices:
            content.append(
                "- GPU %s · %s\n  空闲 **%s / %s** · 算力 %s"
                % (
                    row["index"],
                    row["name"],
                    format_bytes(row["free"]),
                    format_bytes(row["total"]),
                    format_percent(row["util"]),
                )
            )
        sections.append({"title": "%s · %d 张" % (server, len(devices)), "content": "\n".join(content)})
    return compact_card(
        "空闲 GPU",
        "共 **%d** 张 GPU，已按可用显存排序；点击机器展开。" % len(rows),
        sections[:12],
        template="green",
    )


class FeishuResourceBot:
    def __init__(self, app_id, app_secret, dashboard_api):
        import lark_oapi as lark

        self.lark = lark
        self.dashboard = dashboard_api
        self.deduplicator = MessageDeduplicator()
        self.router = LLMIntentRouter(
            base_url=os.getenv("FEISHU_BOT_LLM_BASE_URL"),
            api_key=os.getenv("FEISHU_BOT_LLM_API_KEY"),
            model=os.getenv("FEISHU_BOT_LLM_MODEL"),
            timeout=os.getenv("FEISHU_BOT_LLM_TIMEOUT_SECONDS", "8"),
        )
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

    def _reply_payload(self, message_id, msg_type, payload):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(json.dumps(payload, ensure_ascii=False))
            .build()
        )
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError("Feishu reply failed with code %s" % response.code)

    def reply(self, message_id, text, card=None):
        if card:
            try:
                self._reply_payload(message_id, "interactive", card)
                return
            except Exception as exc:
                print("card reply failed: %s" % type(exc).__name__, flush=True)
        self._reply_payload(message_id, "text", {"text": str(text)[:3900]})

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
        sender_id = getattr(getattr(data.event.sender, "sender_id", None), "open_id", "")
        print("message received sender_open_id=%s" % (sender_id or "unknown"), flush=True)
        try:
            command, argument = self.router.route(self.event_text(data))
            card = None
            if command == "person":
                if not argument:
                    answer = "请指定姓名或账号，例如：查人 崔涵帅"
                    card = compact_card("缺少用户", answer, template="orange")
                else:
                    payload = self.dashboard.person(argument)
                    answer = person_text(payload)
                    card = person_card(payload)
            elif command == "machine":
                snapshot = self.dashboard.snapshot()
                answer = machine_text(snapshot, argument)
                card = machine_card(snapshot, argument)
            elif command == "idle_gpu":
                snapshot = self.dashboard.snapshot()
                answer = idle_gpu_text(snapshot)
                card = idle_gpu_card(snapshot)
            elif command == "admin_disabled":
                answer = "账号管理命令尚未开放。当前机器人只提供只读资源查询。"
                card = compact_card("操作尚未开放", answer, template="orange")
            else:
                answer = HELP_TEXT
                card = compact_card("设备资源助手", HELP_TEXT, template="blue")
        except Exception as exc:
            print("query failed: %s" % type(exc).__name__, flush=True)
            answer = "查询暂时失败，请稍后重试。"
        try:
            self.reply(message_id, answer, card)
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
