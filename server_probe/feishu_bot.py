#!/usr/bin/env python3
"""Feishu long-connection bot for read-only resource queries."""

import hashlib
import json
import os
import queue
import re
import secrets
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
申请账号
待审批（管理员）
开通账号（管理员）
帮助

查询支持折叠卡片；账号写操作必须经过表单、身份校验和确认。"""

ALLOWED_INTENTS = {
    "help",
    "idle_gpu",
    "person",
    "machine",
    "request_account",
    "pending_requests",
    "provision_account",
    "admin_disabled",
}


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
    if compact in ("申请账号", "账号申请", "申请机器", "申请机器账号") or any(
        word in compact for word in ("想申请账号", "帮我申请账号")
    ):
        return "request_account", ""
    if compact in ("待审批", "待处理申请", "申请列表", "审批列表"):
        return "pending_requests", ""
    if compact in ("开通账号", "开账号", "主动开账号", "创建机器账号") or any(
        word in compact for word in ("帮我开账号", "我要开账号")
    ):
        return "provision_account", ""
    for prefix, command in (("查人", "person"), ("查询用户", "person"), ("查机", "machine"), ("查询机器", "machine")):
        if value.startswith(prefix):
            return command, value[len(prefix) :].strip(" ：:")
    if any(word in compact for word in ("创建账号", "审批", "通过申请", "删除账号", "改密码")):
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
            "intent 只能是 help、idle_gpu、person、machine、request_account、pending_requests、provision_account、admin_disabled。"
            "查询某个人当前资源用 person，argument 填姓名或账号；查询某台机器用 machine，argument 填机器名或IP；"
            "查询空闲显卡用 idle_gpu；用户想提交申请用 request_account；管理员查看待审批用 pending_requests；"
            "管理员主动开通账号用 provision_account；删除、改密码、直接批准等其他写操作必须用 admin_disabled；其余用 help。"
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

    def request(self, path, method="GET", data=None):
        admin = self.store.get_user_by_username("admin")
        if not admin:
            raise RuntimeError("dashboard admin account is unavailable")
        token, _ = self.store.create_session(admin["id"], ip_address="127.0.0.1", user_agent="feishu-bot")
        try:
            headers = {"Cookie": "probe_session=" + token, "User-Agent": "server-probe-feishu-bot"}
            body = None
            if data is not None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                self.base_url + path,
                data=body,
                headers=headers,
                method=method,
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

    def get(self, path):
        return self.request(path)

    def post(self, path, data=None):
        return self.request(path, method="POST", data=data or {})

    def snapshot(self):
        return self.get("/api/snapshot") or {}

    def person(self, name):
        return self.get("/api/user-usage?person=" + urllib.parse.quote(name, safe=""))

    def machines(self):
        return (self.get("/api/request-machines") or {}).get("machines") or []


class FeishuWorkflowStore:
    def __init__(self, auth_store):
        self.auth_store = auth_store
        self.setup()

    def setup(self):
        with self.auth_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_feishu_requests (
                      request_id BIGINT PRIMARY KEY REFERENCES probe_model_requests(id) ON DELETE CASCADE,
                      requester_open_id TEXT NOT NULL,
                      requester_name TEXT,
                      chat_id TEXT,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS probe_feishu_requests_open_id_idx ON probe_feishu_requests(requester_open_id)"
                )

    def synthetic_username(self, open_id):
        return "feishu_%s" % hashlib.sha256(str(open_id).encode("utf-8")).hexdigest()[:16]

    def ensure_requester(self, open_id, display_name):
        username = self.synthetic_username(open_id)
        user = self.auth_store.get_user_by_username(username)
        if user:
            return user
        self.auth_store.set_password(
            username,
            secrets.token_urlsafe(32),
            role="user",
            display_name=str(display_name or "飞书用户")[:80],
        )
        return self.auth_store.get_user_by_username(username)

    def create_request(self, open_id, chat_id, data):
        requester = self.ensure_requester(open_id, data.get("owner_name"))
        recommendation = {
            "generated_by": "feishu-bot",
            "message": "Submitted through Feishu and awaiting administrator review",
            "candidates": [],
        }
        request_id = self.auth_store.create_model_request(requester["id"], data, recommendation)
        with self.auth_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_feishu_requests (request_id, requester_open_id, requester_name, chat_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (request_id) DO UPDATE SET
                      requester_open_id = EXCLUDED.requester_open_id,
                      requester_name = EXCLUDED.requester_name,
                      chat_id = EXCLUDED.chat_id
                    """,
                    (request_id, open_id, str(data.get("owner_name") or "")[:80], str(chat_id or "")[:160]),
                )
        return request_id

    def request_context(self, request_id):
        with self.auth_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT requester_open_id, requester_name, chat_id FROM probe_feishu_requests WHERE request_id = %s",
                    (int(request_id),),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"open_id": row[0], "name": row[1], "chat_id": row[2]}

    def pending_requests(self):
        admin = self.auth_store.get_user_by_username("admin")
        if not admin:
            return []
        return [item for item in self.auth_store.list_model_requests(admin) if item.get("status") == "pending"]


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


def machine_options(machines):
    options = []
    for machine in machines[:50]:
        value = str(machine.get("id") or "").strip()
        if not value:
            continue
        label = machine.get("name") or value
        host = machine.get("host")
        if host and host != label:
            label = "%s · %s" % (label, host)
        options.append({"text": {"tag": "plain_text", "content": str(label)[:100]}, "value": value})
    return options


def form_input(name, label, placeholder="", required=False, input_type="text", default_value=""):
    element = {
        "tag": "input",
        "name": name,
        "label": {"tag": "plain_text", "content": label},
        "label_position": "top",
        "placeholder": {"tag": "plain_text", "content": placeholder or label},
        "input_type": input_type,
        "required": bool(required),
        "width": "fill",
        "max_length": 300,
    }
    if default_value not in (None, ""):
        element["default_value"] = str(default_value)
    return element


def form_select(name, label, options, required=True, initial_option=None):
    element = {
        "tag": "select_static",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": "请选择%s" % label},
        "required": bool(required),
        "width": "fill",
        "options": options,
    }
    if initial_option:
        element["initial_option"] = initial_option
    return element


def account_form_card(machines, direct=False):
    options = machine_options(machines)
    elements = [
        {"tag": "markdown", "content": "**账号类型**"},
        form_select(
            "request_type",
            "账号类型",
            [
                {"text": {"tag": "plain_text", "content": "临时账号"}, "value": "temporary"},
                {"text": {"tag": "plain_text", "content": "长期账号"}, "value": "access"},
            ],
            initial_option="temporary",
        ),
        form_input("owner_name", "姓名", "真实姓名", required=True),
        {"tag": "markdown", "content": "**目标机器**"},
        form_select("target_machine", "目标机器", options, required=True),
        form_input("requested_account", "账号名", "留空则自动生成" if direct else "希望使用的账号名"),
        form_input("requested_password", "密码", "留空则自动生成", input_type="password"),
        form_input("duration_hours", "临时时长（小时）", "长期账号可填 24", required=True, default_value="24"),
    ]
    if not direct:
        elements.extend(
            [
                form_input("model_name", "模型或任务", "例如 Qwen3 / 训练任务", required=True),
                form_input("purpose", "用途", "简要说明用途", required=True),
                form_input("gpu_count", "GPU 数量", "例如 1", required=True, default_value="1"),
            ]
        )
    elements.append(
        {
            "tag": "button",
            "name": "submit",
            "form_action_type": "submit",
            "type": "primary",
            "width": "fill",
            "text": {"tag": "plain_text", "content": "确认开通" if direct else "提交申请"},
            "behaviors": [
                {"type": "callback", "value": {"action": "direct_provision" if direct else "submit_request"}}
            ],
        }
    )
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "orange" if direct else "blue",
            "title": {"tag": "plain_text", "content": "管理员主动开账号" if direct else "提交机器账号申请"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "密码仅用于机器账号创建；留空时由系统随机生成。长期账号不会设置自动删除时间。",
                    "text_size": "notation",
                },
                {"tag": "form", "name": "account_form", "elements": elements},
            ]
        },
    }


def callback_button(label, action, request_id, button_type):
    return {
        "tag": "button",
        "type": button_type,
        "width": "fill",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [
            {"type": "callback", "value": {"action": action, "request_id": str(request_id)}}
        ],
    }


def pending_requests_card(requests):
    if not requests:
        return compact_card("待审批申请", "当前没有待处理申请。", template="green")
    panels = []
    for item in requests[:15]:
        details = (
            "姓名 **%s** · 类型 **%s**\n机器 `%s` · 账号 `%s`\n模型/任务 %s · 时长 %s 小时\n%s"
            % (
                item.get("owner_name") or item.get("requester_display_name") or item.get("requester"),
                "长期" if item.get("request_type") == "access" else "临时",
                item.get("target_machine_label") or item.get("target_machine") or "待分配",
                item.get("requested_account") or "自动生成",
                item.get("model_name") or "-",
                item.get("duration_hours") or "-",
                item.get("purpose") or "",
            )
        )
        panels.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {"tag": "plain_text", "content": "#%s · %s" % (item.get("id"), item.get("owner_name") or item.get("requester"))},
                    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "border": {"color": "grey", "corner_radius": "5px"},
                "elements": [
                    {"tag": "markdown", "content": details},
                    {
                        "tag": "column_set",
                        "flex_mode": "bisect",
                        "columns": [
                            {"tag": "column", "width": "weighted", "weight": 1, "elements": [callback_button("通过并开通", "approve_request", item.get("id"), "primary")]},
                            {"tag": "column", "width": "weighted", "weight": 1, "elements": [callback_button("拒绝", "reject_request", item.get("id"), "danger")]},
                        ],
                    },
                ],
            }
        )
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "待审批申请"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "共 **%d** 条待处理；展开后确认机器和账号。" % len(requests)},
                *panels,
            ]
        },
    }


class FeishuResourceBot:
    def __init__(self, app_id, app_secret, dashboard_api):
        import lark_oapi as lark

        self.lark = lark
        self.dashboard = dashboard_api
        self.workflow = FeishuWorkflowStore(dashboard_api.store)
        self.deduplicator = MessageDeduplicator()
        self.admin_open_ids = {
            value.strip() for value in os.getenv("FEISHU_ADMIN_OPEN_IDS", "").split(",") if value.strip()
        }
        self.jobs = queue.Queue(maxsize=100)
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
            .register_p2_card_action_trigger(self.on_card_action)
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
        self.worker = threading.Thread(target=self.job_worker, name="feishu-account-worker", daemon=True)
        self.worker.start()

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

    def send_to_open_id(self, open_id, text, card=None):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        msg_type = "interactive" if card else "text"
        content = card if card else {"text": str(text)[:3900]}
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        request = CreateMessageRequest.builder().receive_id_type("open_id").request_body(body).build()
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError("Feishu send failed with code %s" % response.code)

    def is_admin(self, open_id):
        return bool(open_id and open_id in self.admin_open_ids)

    def callback_response(self, message, success=True):
        from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackToast, P2CardActionTriggerResponse

        response = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "success" if success else "error"
        toast.content = str(message)[:200]
        response.toast = toast
        return response

    def normalized_form(self, values):
        result = {}
        for key, value in (values or {}).items():
            if isinstance(value, dict):
                value = value.get("value") or value.get("option") or ""
            result[str(key)] = str(value or "").strip()
        return result

    def request_payload(self, form):
        request_type = form.get("request_type") or "temporary"
        if request_type not in ("temporary", "access"):
            raise ValueError("账号类型无效")
        owner_name = form.get("owner_name", "").strip()
        target_machine = form.get("target_machine", "").strip()
        account = form.get("requested_account", "").strip()
        password = form.get("requested_password", "").strip()
        model_name = form.get("model_name", "").strip()
        purpose = form.get("purpose", "").strip()
        if not owner_name or not target_machine:
            raise ValueError("姓名和目标机器不能为空")
        if account and not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", account):
            raise ValueError("账号名格式无效")
        if "\n" in password or "\r" in password:
            raise ValueError("密码格式无效")
        try:
            duration = int(form.get("duration_hours") or 0)
            gpu_count = int(form.get("gpu_count") or 1)
        except ValueError:
            raise ValueError("时长和 GPU 数量必须是整数") from None
        if request_type == "temporary" and duration <= 0:
            raise ValueError("临时账号必须填写有效时长")
        if request_type == "access" and (not account or not password):
            raise ValueError("长期账号必须填写账号名和密码")
        if not model_name:
            model_name = "长期机器接入" if request_type == "access" else "临时计算任务"
        if not purpose:
            purpose = "通过飞书提交的机器账号申请"
        machine_label = target_machine
        for machine in self.dashboard.machines():
            if machine.get("id") == target_machine:
                machine_label = machine.get("name") or target_machine
                break
        return {
            "request_type": request_type,
            "owner_name": owner_name[:80],
            "model_name": model_name[:160],
            "model_size": "",
            "purpose": purpose[:2000],
            "access_type": "ssh",
            "gpu_count": max(0, min(gpu_count, 16)),
            "gpu_memory_gb": None,
            "duration_hours": duration if request_type == "temporary" else None,
            "target_machine": target_machine[:160],
            "target_machine_label": str(machine_label)[:240],
            "requested_account": account[:120],
            "requested_password": password[:300],
            "notes": "Submitted through Feishu bot",
        }

    def on_card_action(self, data):
        event = data.event
        operator = getattr(event, "operator", None)
        open_id = getattr(operator, "open_id", "")
        action = getattr(event, "action", None)
        value = getattr(action, "value", None) or {}
        action_name = str(value.get("action") or "")
        form = self.normalized_form(getattr(action, "form_value", None) or {})
        chat_id = getattr(getattr(event, "context", None), "open_chat_id", "")
        try:
            if action_name == "submit_request":
                payload = self.request_payload(form)
                request_id = self.workflow.create_request(open_id, chat_id, payload)
                for admin_open_id in self.admin_open_ids:
                    try:
                        self.send_to_open_id(
                            admin_open_id,
                            "收到新的账号申请 #%s：%s · %s。发送“待审批”查看。"
                            % (request_id, payload["owner_name"], payload["target_machine_label"]),
                        )
                    except Exception:
                        pass
                return self.callback_response("申请 #%s 已提交" % request_id)
            if action_name in ("direct_provision", "approve_request", "reject_request"):
                if not self.is_admin(open_id):
                    return self.callback_response("仅管理员可以执行此操作", success=False)
                job = {"action": action_name, "operator_open_id": open_id, "form": form}
                if value.get("request_id"):
                    job["request_id"] = int(value["request_id"])
                self.jobs.put_nowait(job)
                return self.callback_response("任务已提交，完成后将私聊通知")
            return self.callback_response("未知操作", success=False)
        except (ValueError, queue.Full) as exc:
            return self.callback_response(str(exc) or "提交失败", success=False)
        except Exception:
            return self.callback_response("提交失败，请稍后重试", success=False)

    def result_text(self, result):
        account = result.get("account") or {}
        if account:
            return (
                "账号开通成功\n机器：%s\n账号：%s\n密码：%s\n到期：%s"
                % (
                    account.get("machine") or "-",
                    account.get("username") or "-",
                    account.get("password") or "-",
                    account.get("expires_at") or "长期有效",
                )
            )
        request = result.get("request") or {}
        allocation = {}
        for line in str(request.get("allocation_note") or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                allocation[key] = value
        return (
            "申请 #%s 已开通\n机器：%s\n账号：%s\n密码：%s\n到期：%s"
            % (
                request.get("id") or "-",
                allocation.get("machine") or request.get("target_machine_label") or "-",
                allocation.get("account") or request.get("requested_account") or "-",
                allocation.get("password") or "-",
                allocation.get("expires_at") or "长期有效",
            )
        )

    def job_worker(self):
        while True:
            job = self.jobs.get()
            try:
                action = job["action"]
                if action == "direct_provision":
                    payload = self.request_payload(job["form"])
                    result = self.dashboard.post(
                        "/api/provision-account",
                        {
                            "account_type": payload["request_type"],
                            "owner_name": payload["owner_name"],
                            "target_machine": payload["target_machine"],
                            "username": payload["requested_account"],
                            "password": payload["requested_password"],
                            "duration_hours": payload["duration_hours"],
                        },
                    )
                    self.send_to_open_id(job["operator_open_id"], self.result_text(result or {}))
                elif action == "approve_request":
                    request_id = int(job["request_id"])
                    result = self.dashboard.post("/api/resource-requests/%d/provision" % request_id, {})
                    message = self.result_text(result or {})
                    self.send_to_open_id(job["operator_open_id"], message)
                    context = self.workflow.request_context(request_id)
                    if context and context.get("open_id") != job["operator_open_id"]:
                        self.send_to_open_id(context["open_id"], message)
                elif action == "reject_request":
                    request_id = int(job["request_id"])
                    self.dashboard.post(
                        "/api/resource-requests/%d/status" % request_id,
                        {"status": "rejected", "admin_note": "Rejected in Feishu"},
                    )
                    message = "申请 #%d 已拒绝。" % request_id
                    self.send_to_open_id(job["operator_open_id"], message)
                    context = self.workflow.request_context(request_id)
                    if context and context.get("open_id") != job["operator_open_id"]:
                        self.send_to_open_id(context["open_id"], message)
            except Exception as exc:
                print("account job failed: %s" % type(exc).__name__, flush=True)
                try:
                    self.send_to_open_id(job.get("operator_open_id"), "账号操作失败，请到网页管理后台检查。")
                except Exception:
                    pass
            finally:
                self.jobs.task_done()

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
            elif command == "request_account":
                answer = "请在卡片中填写账号申请。"
                card = account_form_card(self.dashboard.machines(), direct=False)
            elif command == "pending_requests":
                if not self.is_admin(sender_id):
                    answer = "仅管理员可以查看待审批申请。"
                    card = compact_card("无管理员权限", answer, template="red")
                else:
                    requests = self.workflow.pending_requests()
                    answer = "当前有 %d 条待审批申请。" % len(requests)
                    card = pending_requests_card(requests)
            elif command == "provision_account":
                if not self.is_admin(sender_id):
                    answer = "仅管理员可以主动开通账号。"
                    card = compact_card("无管理员权限", answer, template="red")
                else:
                    answer = "请在卡片中填写开通信息。"
                    card = account_form_card(self.dashboard.machines(), direct=True)
            elif command == "admin_disabled":
                answer = "该写操作尚未开放，请使用“申请账号”“待审批”或“开通账号”。"
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
