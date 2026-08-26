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
from server_probe.usage_reports import format_bytes, format_percent, usage_bar


HELP_TEXT = """可以直接用自然语言询问：
- 哪些四卡机现在空闲
- 找一张至少 40 GB 可用显存的卡
- 崔涵帅正在使用哪些资源
- 最近哪些用户资源占用较高
- gpu010 现在怎么样
- 申请账号
- 申请模型
- 模型状态

管理员还可以查询待审批申请、主动开通账号或部署模型。"""

ALLOWED_INTENTS = {
    "help",
    "idle_gpu",
    "person",
    "machine",
    "machine_catalog",
    "resource_query",
    "top_users",
    "request_account",
    "pending_requests",
    "provision_account",
    "request_model",
    "model_catalog",
    "model_status",
    "deploy_model",
    "admin_disabled",
    "clarify",
    "chat",
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
    if compact in ("用户排行", "资源排行", "高占用用户", "谁占用最多"):
        return "top_users", ""
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
    if "模型" in compact and any(word in compact for word in ("状态", "运行", "健康", "进度")):
        return "model_status", ""
    if "模型" in compact and any(
        phrase in compact
        for phrase in ("哪些模型", "什么模型", "模型列表", "可部署模型", "支持的模型", "会部署模型", "能部署模型")
    ):
        return "model_catalog", ""
    if compact in ("申请模型", "模型申请", "申请模型服务", "申请api") or (
        "申请" in compact and ("模型" in compact or "api" in compact)
    ):
        return "request_model", ""
    if compact == "模型服务":
        return "model_status", ""
    if compact in ("部署模型", "创建模型", "启动模型", "创建模型服务") or (
        "模型" in compact and any(word in compact for word in ("部署", "创建", "启动"))
    ):
        return "deploy_model", ""
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


def normalized_top_user_filters(value=None):
    source = value if isinstance(value, dict) else {}
    try:
        limit = max(1, min(int(source.get("limit") or 10), 15))
    except (TypeError, ValueError):
        limit = 10
    resource = str(source.get("resource") or "all").strip().lower()
    if resource not in ("all", "gpu", "memory", "machines"):
        resource = "all"
    return {"limit": limit, "resource": resource}


def normalized_resource_query(value=None):
    source = value if isinstance(value, dict) else {}
    scope = source.get("scope") if isinstance(source.get("scope"), dict) else {}
    entity = str(source.get("entity") or "process").strip().lower()
    if entity not in ("process",):
        entity = "process"
    metric = str(source.get("metric") or "gpu_memory").strip().lower()
    if metric not in ("gpu_memory", "memory", "cpu"):
        metric = "gpu_memory"
    try:
        limit = max(1, min(int(source.get("limit") or 5), 15))
    except (TypeError, ValueError):
        limit = 5
    return {
        "entity": entity,
        "metric": metric,
        "limit": limit,
        "scope": {
            "machine": " ".join(str(scope.get("machine") or "").strip().split())[:120] or None,
            "gpu_count": normalized_gpu_count(scope.get("gpu_count")),
            "group": " ".join(str(scope.get("group") or "").strip().split())[:80] or None,
            "gpu_model": " ".join(str(scope.get("gpu_model") or "").strip().split())[:80] or None,
            "user": " ".join(str(scope.get("user") or "").strip().split())[:80] or None,
        },
    }


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
        return self.as_legacy_route(self.plan(text))

    def route_with_context(self, text, previous=None):
        return self.as_legacy_route(self.plan(text, previous=previous))

    def fallback_plan(self, text):
        intent, argument = parse_command(text)
        if intent == "unknown":
            intent = "help"
        return {
            "intent": intent,
            "argument": argument,
            "filters": {},
            "clarification": "",
        }

    def as_legacy_route(self, plan):
        intent = plan.get("intent") or "help"
        if intent == "clarify":
            return "help", ""
        argument = plan.get("argument") or ""
        if intent == "idle_gpu" and plan.get("filters", {}).get("gpu_count"):
            argument = str(plan["filters"]["gpu_count"])
        return intent, argument

    def plan(self, text, previous=None, inventory=None):
        return self.plans(text, previous=previous, inventory=inventory)[0]

    def plans(self, text, previous=None, inventory=None):
        text = clean_command(text)[:500]
        safety_guard = parse_command(text)
        if safety_guard[0] == "admin_disabled":
            return [self.fallback_plan(text)]
        if not self.enabled:
            return [self.fallback_plan(text)]
        if isinstance(previous, (list, tuple)) and len(previous) >= 2:
            previous = {"intent": previous[0], "argument": previous[1], "filters": {}}
        previous = previous if isinstance(previous, dict) else {}
        inventory = inventory if isinstance(inventory, dict) else {}
        key = (
            text.lower(),
            json.dumps(previous, ensure_ascii=False, sort_keys=True)[:1000],
            json.dumps(inventory, ensure_ascii=False, sort_keys=True)[:1000],
        )
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        try:
            result = self.classify_many(text, previous=previous, inventory=inventory)
        except Exception as exc:
            print("llm planner failed error=%s" % type(exc).__name__, flush=True)
            fallback = self.fallback_plan(text)
            if parse_command(text)[0] == "unknown":
                fallback = {
                    "intent": "clarify",
                    "argument": "",
                    "filters": {},
                    "query": {},
                    "clarification": "刚才的自然语言理解服务响应失败，请重试这句话。",
                }
            return [fallback]
        with self.lock:
            self.cache[key] = result
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return result

    def classify(self, text, previous=None, inventory=None):
        return self.classify_many(text, previous=previous, inventory=inventory)[0]

    def classify_many(self, text, previous=None, inventory=None):
        system_prompt = (
            "你是设备资源助手的工具规划器，负责理解自然中文、连续追问和一句话中的多个独立问题。"
            "只输出一个 JSON 对象，不要解释；格式是 {\"actions\":[...]}，按用户提问顺序最多输出 3 个动作。"
            "可选 intent：idle_gpu、person、machine、request_account、pending_requests、provision_account、"
            "request_model、model_catalog、model_status、deploy_model、"
            "machine_catalog、resource_query、top_users、admin_disabled、help、clarify、chat。"
            "idle_gpu 用于查询机器清单、空闲机器、GPU 容量或按条件找机器。filters 可包含："
            "gpu_count（每台机器的 GPU 张数，整数或 null）、group（机器组或 null）、idle_only（布尔值）、"
            "min_free_gpu_memory_gb（要求单卡至少多少 GB 空闲显存或 null）、gpu_model（型号关键词或 null）。"
            "person 查询某个人或账号，argument 填姓名/账号；machine 查询一台明确机器，argument 填名称/IP。"
            "machine_catalog 用于解释单卡机/四卡机/八卡机等设备类别，或查询某类设备对应哪些实际机器；"
            "filters 可使用 gpu_count、group、gpu_model，但不要设置 idle_only 或显存阈值。"
            "resource_query 用于组合式资源分析，尤其是查询某类机器/用户上的进程排行。query 格式为："
            "{\"entity\":\"process\",\"metric\":\"gpu_memory|memory|cpu\",\"limit\":1到15,"
            "\"scope\":{\"machine\":名称或null,\"gpu_count\":整数或null,\"group\":分组或null,"
            "\"gpu_model\":型号或null,\"user\":用户或null}}。"
            "只要问题同时包含资源范围、对象、比较/最高/排行等分析要求，就优先使用 resource_query；"
            "不能因为没有旧的固定指令而退回 help。缺少执行所需条件时才用 clarify。"
            "top_users 查询本小时资源使用较高的用户；filters.limit 是 1 到 15，filters.resource 可为 all、gpu、memory、machines。"
            "request_account 是普通用户提交申请；pending_requests 是管理员看待审批；provision_account 是管理员主动开账号。"
            "request_model 是用户申请部署目录中已有模型的 API；model_status 查询已部署模型服务，可把模型关键词放 argument；"
            "model_catalog 查询当前目录中已验证且可以部署的模型清单；"
            "deploy_model 是管理员直接部署模型。"
            "询问系统是否会部署模型、具备哪些模型部署能力、有哪些权重可以提供推理 API，必须使用 model_catalog，不能用 chat。"
            "句子明确包含‘部署模型’‘创建模型’‘启动模型’时必须使用 deploy_model，不能因为不知道发送者角色而降级成 request_model；"
            "只有‘申请模型’‘申请模型 API’‘想使用某模型’才使用 request_model，权限由后端另行判断。"
            "删除账号、改密码、直接批准等未开放写操作用 admin_disabled。真正不明确时用 clarify，clarification 给一句具体追问。"
            "chat 只用于问候、反馈和解释上一轮对话，response 给不超过 100 字的简短回复；chat 不得声称任何实时资源事实。"
            "结合 previous 理解‘那四卡机呢’‘它呢’等追问，并继承仍然适用的条件。"
            "inventory 仅说明系统现有分组和 GPU 张数类型，不要臆造机器或资源数据。"
            "inventory.machines 是脱敏设备目录，包含真实名称、类别和 GPU 型号；涉及现有机器类别或类别对应设备时，"
            "必须使用 machine_catalog，不要退回 help/chat。"
            "每个动作格式为 {\"intent\":\"...\",\"argument\":\"\",\"filters\":{},\"clarification\":\"\"}。"
            "示例：‘查空闲机器吧，四卡机的呢’输出 "
            "{\"actions\":[{\"intent\":\"idle_gpu\",\"argument\":\"\",\"filters\":{\"gpu_count\":4,"
            "\"group\":null,\"idle_only\":true,\"min_free_gpu_memory_gb\":null,\"gpu_model\":null},"
            "\"clarification\":\"\"}]}。"
            "示例：‘你还会啥？有哪些用户最近使用机器量很大’必须拆成两个动作：help 和 top_users，不能忽略后一个问题。"
            "示例：‘你会部署模型吗’输出 model_catalog；‘目前有哪些权重能做成接口’输出 model_catalog；"
            "‘给我起一个 Qwen 推理服务’输出 deploy_model；‘刚才那个模型起到哪一步了’输出 model_status。"
            "示例：‘你知道八卡机是啥吗’输出 machine_catalog，filters.gpu_count=8；后端会回答当前对应机器。"
            "示例：‘八卡机什么进程占用最高’输出 resource_query，query.entity=process、query.metric=gpu_memory、"
            "query.limit=5、query.scope.gpu_count=8。"
            "如果 previous 显示上一句包含多个动作，而用户问‘为什么第二个没回复’，用 chat 并在 response 中说明看到的动作。"
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "text": str(text)[:500],
                            "previous": previous or {},
                            "inventory": inventory or {},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 640,
        }
        headers = {"Content-Type": "application/json", "User-Agent": "server-probe-feishu-bot"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        content = ""
        for attempt in range(2):
            body["max_tokens"] = 640 if attempt == 0 else 1000
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
                content = "".join(
                    str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in content
                )
            if content:
                break
        match = re.search(r"\{.*\}", str(content), re.S)
        if not match:
            raise ValueError("LLM did not return JSON")
        parsed = json.loads(match.group(0))
        actions = parsed.get("actions") if isinstance(parsed.get("actions"), list) else [parsed]
        normalized = [self.normalize_action(action) for action in actions[:3] if isinstance(action, dict)]
        return normalized or [self.fallback_plan(text)]

    def normalize_action(self, action):
        intent = str(action.get("intent") or "help").strip()
        argument = " ".join(str(action.get("argument") or "").strip().split())[:100]
        if intent not in ALLOWED_INTENTS:
            intent = "help"
        filters = action.get("filters") if isinstance(action.get("filters"), dict) else {}
        if intent == "idle_gpu":
            normalized_filters = normalized_resource_filters(filters)
        elif intent == "machine_catalog":
            normalized_filters = normalized_resource_filters(filters)
            normalized_filters["idle_only"] = False
            normalized_filters["min_free_gpu_memory_gb"] = None
        elif intent == "top_users":
            normalized_filters = normalized_top_user_filters(filters)
        else:
            normalized_filters = {}
        query = normalized_resource_query(action.get("query")) if intent == "resource_query" else {}
        if intent in ("person", "machine") and not argument:
            intent = "clarify"
            action["clarification"] = "你想查询哪位用户或哪台机器？"
        clarification = " ".join(str(action.get("clarification") or "").strip().split())[:240]
        if intent == "chat":
            clarification = " ".join(str(action.get("response") or clarification).strip().split())[:240]
            if not clarification:
                clarification = "我看到了你的消息，请继续告诉我想查询什么。"
        if intent == "clarify" and not clarification:
            clarification = "请再补充一下要查询的人、机器或资源条件。"
        return {
            "intent": intent,
            "argument": argument,
            "filters": normalized_filters,
            "query": query,
            "clarification": clarification,
        }


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


def prefix_card_element_ids(value, prefix):
    if isinstance(value, list):
        return [prefix_card_element_ids(item, prefix) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "element_id" and item:
            result[key] = "%s_%s" % (prefix, item)
        else:
            result[key] = prefix_card_element_ids(item, prefix)
    return result


def combine_cards(cards):
    cards = [card for card in cards if card]
    if not cards:
        return None
    if len(cards) == 1:
        return cards[0]
    elements = []
    for index, card in enumerate(cards, 1):
        title = (((card.get("header") or {}).get("title") or {}).get("content") or "查询结果")
        elements.append(
            {
                "tag": "markdown",
                "content": "<font color='grey'>**%d. %s**</font>" % (index, str(title)[:100]),
            }
        )
        elements.extend(prefix_card_element_ids((card.get("body") or {}).get("elements") or [], "result%d" % index))
        if index < len(cards):
            elements.append({"tag": "hr"})
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "设备资源助手 · %d 项结果" % len(cards)},
        },
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


def selected_top_users(payload, filters=None):
    filters = normalized_top_user_filters(filters)
    people = list((payload or {}).get("people") or [])
    resource = filters["resource"]
    if resource == "gpu":
        people = [person for person in people if person.get("gpu_memory_peak_bytes")]
        people.sort(key=lambda person: person.get("gpu_memory_peak_bytes") or 0, reverse=True)
    elif resource == "memory":
        people.sort(key=lambda person: person.get("memory_peak_bytes") or 0, reverse=True)
    elif resource == "machines":
        people.sort(
            key=lambda person: (
                person.get("machine_count") or 0,
                person.get("gpu_memory_peak_bytes") or 0,
                person.get("memory_peak_bytes") or 0,
            ),
            reverse=True,
        )
    return people[: filters["limit"]]


def top_users_text(payload, filters=None):
    people = selected_top_users(payload, filters)
    if not people:
        return "本时段没有检测到符合条件的普通用户资源记录。"
    lines = ["%s高资源用户：" % ((payload or {}).get("window") or "近期")]
    for index, person in enumerate(people, 1):
        display = person.get("display_name") or ", ".join(person.get("usernames") or []) or "未登记用户"
        lines.append(
            "%d. %s · %d 台机器 · GPU %d 张 · 显存 %s · 内存 %s"
            % (
                index,
                display,
                int(person.get("machine_count") or 0),
                int(person.get("gpu_count") or 0),
                format_bytes(person.get("gpu_memory_peak_bytes")),
                format_bytes(person.get("memory_peak_bytes")),
            )
        )
    return "\n".join(lines)[:3900]


def top_users_card(payload, filters=None):
    people = selected_top_users(payload, filters)
    window = (payload or {}).get("window") or "近期"
    if not people:
        return compact_card("高资源用户", "%s没有检测到符合条件的普通用户记录。" % window, template="grey")
    sections = []
    for index, person in enumerate(people, 1):
        display = person.get("display_name") or ", ".join(person.get("usernames") or []) or "未登记用户"
        usernames = "、".join(person.get("usernames") or []) or "-"
        lines = [
            "账号 `%s` · 机器 **%d** 台 · GPU **%d** 张"
            % (usernames, int(person.get("machine_count") or 0), int(person.get("gpu_count") or 0)),
            "显存峰值 **%s** · 内存峰值 **%s** · CPU 均值合计 **%s**"
            % (
                format_bytes(person.get("gpu_memory_peak_bytes")),
                format_bytes(person.get("memory_peak_bytes")),
                format_percent(person.get("cpu_average_sum")),
            ),
            "综合资源强度 %s" % usage_bar(person.get("resource_score"), 100, 8),
        ]
        for row in (person.get("machine_rows") or [])[:6]:
            gpu_indices = ",".join(row.get("gpu_indices") or [])
            machine_line = "- **%s** · `%s`" % (row.get("server_name") or row.get("server_id"), row.get("user") or "-")
            if gpu_indices:
                machine_line += " · GPU " + gpu_indices
            machine_line += "\n  显存 %s · 内存 %s · CPU %s" % (
                format_bytes(row.get("gpu_memory_peak_bytes")),
                format_bytes(row.get("memory_peak_bytes")),
                format_percent(row.get("cpu_average")),
            )
            lines.append(machine_line)
            process = row.get("top_gpu_process") or {}
            if process:
                label = process.get("container_name") or process.get("model") or process.get("process_name") or "unknown"
                lines.append("  最高显存进程 `%s` · PID %s · %s" % (
                    label,
                    process.get("pid") or "-",
                    format_bytes(process.get("used_memory_bytes")),
                ))
        sections.append(
            {
                "title": "%d. %s · %d 台 · 显存 %s"
                % (index, display, int(person.get("machine_count") or 0), format_bytes(person.get("gpu_memory_peak_bytes"))),
                "content": "\n".join(lines),
            }
        )
    summary = "%s · **%d** 次采样 · 活跃用户 **%d** 人\n按综合资源强度排序，点击姓名查看机器和进程。" % (
        window,
        int((payload or {}).get("sample_count") or 0),
        int((payload or {}).get("active_users") or len(people)),
    )
    return compact_card("近期高资源用户", summary, sections, template="orange")


def compact_duration(seconds):
    seconds = int(as_number(seconds) or 0)
    if seconds <= 0:
        return "-"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return "%d天%d小时" % (days, hours)
    if hours:
        return "%d小时%d分钟" % (hours, minutes)
    return "%d分钟" % minutes


def process_metric_label(metric):
    return {"gpu_memory": "显存占用", "memory": "内存占用", "cpu": "CPU 占用"}.get(metric, "资源占用")


def process_metric_value(row, metric):
    if metric == "gpu_memory":
        return format_bytes(row.get("gpu_memory_bytes"))
    if metric == "memory":
        return format_bytes(row.get("memory_bytes"))
    return format_percent(row.get("cpu_percent"))


def resource_query_scope_label(query, payload=None):
    scope = (query or {}).get("scope") or {}
    if scope.get("machine"):
        return scope["machine"]
    if scope.get("gpu_count"):
        return machine_type_label(scope["gpu_count"])
    if scope.get("group"):
        return scope["group"]
    machines = (payload or {}).get("matched_machines") or []
    if len(machines) == 1:
        return machines[0].get("name") or "目标机器"
    return "匹配机器"


def resource_query_text(payload):
    query = (payload or {}).get("query") or {}
    rows = (payload or {}).get("rows") or []
    metric = query.get("metric") or "gpu_memory"
    scope = resource_query_scope_label(query, payload)
    if not rows:
        return "%s中没有可识别的进程数据。" % scope
    lines = ["%s进程排行（按%s）：" % (scope, process_metric_label(metric))]
    for index, row in enumerate(rows, 1):
        label = row.get("container_name") or row.get("model") or row.get("process_name") or "unknown"
        lines.append(
            "%d. %s · %s · PID %s · %s"
            % (index, label, row.get("server_name") or "-", row.get("pid") or "-", process_metric_value(row, metric))
        )
    return "\n".join(lines)[:3900]


def resource_query_card(payload):
    query = (payload or {}).get("query") or {}
    rows = (payload or {}).get("rows") or []
    machines = (payload or {}).get("matched_machines") or []
    metric = query.get("metric") or "gpu_memory"
    scope = resource_query_scope_label(query, payload)
    if not rows:
        return compact_card(
            "进程排行 · %s" % scope,
            "匹配 **%d** 台机器，但当前没有可识别的进程数据。" % len(machines),
            template="grey",
        )
    sections = []
    for index, row in enumerate(rows, 1):
        label = row.get("container_name") or row.get("model") or row.get("process_name") or "unknown"
        user = row.get("display_name") or row.get("user") or "unknown"
        gpu = ",".join(row.get("gpu_indices") or [])
        lines = [
            "机器 **%s** · 用户 **%s**" % (row.get("server_name") or "-", user),
            "进程 `%s` · PID **%s** · 运行 **%s**"
            % (row.get("process_name") or "unknown", row.get("pid") or "-", compact_duration(row.get("runtime_seconds"))),
        ]
        if gpu:
            lines.append("GPU **%s** · 显存 **%s**" % (gpu, format_bytes(row.get("gpu_memory_bytes"))))
        if row.get("container_name"):
            lines.append("容器 `%s`" % row["container_name"])
        if row.get("model"):
            lines.append("模型 `%s`" % row["model"])
        sections.append(
            {
                "title": "%d. %s · %s" % (index, label, process_metric_value(row, metric)),
                "content": "\n".join(lines),
            }
        )
    first = rows[0]
    first_label = first.get("container_name") or first.get("model") or first.get("process_name") or "unknown"
    summary = (
        "范围 **%s** · 匹配 **%d** 台机器\n最高占用：**%s** · %s **%s**"
        % (scope, len(machines), first_label, process_metric_label(metric), process_metric_value(first, metric))
    )
    return compact_card("进程排行 · %s" % process_metric_label(metric), summary, sections, template="orange")


class DashboardAPI:
    def __init__(self, dsn, base_url="http://127.0.0.1:8088"):
        self.store = AuthStore(dsn, session_hours=1)
        self.base_url = base_url.rstrip("/")

    def request(self, path, method="GET", data=None, timeout=10):
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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                payload = {}
            raise RuntimeError(str(payload.get("error") or "dashboard API returned HTTP %s" % exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("dashboard API is unavailable") from None
        finally:
            self.store.destroy_session(token)

    def get(self, path):
        return self.request(path)

    def post(self, path, data=None, timeout=10):
        return self.request(path, method="POST", data=data or {}, timeout=timeout)

    def snapshot(self):
        return self.get("/api/snapshot") or {}

    def person(self, name):
        return self.get("/api/user-usage?person=" + urllib.parse.quote(name, safe=""))

    def top_users(self, limit=10):
        limit = max(1, min(int(limit), 30))
        return self.get("/api/user-usage-ranking?limit=%d" % limit) or {}

    def resource_query(self, query):
        return self.post("/api/resource-query", {"query": normalized_resource_query(query)}) or {}

    def machines(self):
        return (self.get("/api/request-machines") or {}).get("machines") or []

    def models(self):
        payload = self.get("/api/model-catalog") or {}
        return [item for item in payload.get("models", []) if item.get("enabled") and item.get("candidate")]

    def model_services(self, query=""):
        suffix = "?q=" + urllib.parse.quote(str(query or ""), safe="") if query else ""
        return self.get("/api/model-services" + suffix) or {}

    def deploy_model(self, data):
        return self.post("/api/model-services/deploy", data, timeout=900)

    def deploy_requested_model(self, request_id):
        return self.post("/api/resource-requests/%d/deploy-model" % int(request_id), {}, timeout=900)


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


class ConversationMemory:
    def __init__(self, limit=2000, ttl_seconds=900):
        self.limit = int(limit)
        self.ttl_seconds = float(ttl_seconds)
        self.values = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        now = time.time()
        with self.lock:
            item = self.values.get(key)
            if not item:
                return None
            result, timestamp = item
            if now - timestamp > self.ttl_seconds:
                self.values.pop(key, None)
                return None
            self.values.move_to_end(key)
            return result

    def remember(self, key, user_text, plans):
        if isinstance(plans, dict):
            plans = [plans]
        actions = []
        for plan in (plans or [])[:3]:
            intent = str(plan.get("intent") or "help")
            filters = plan.get("filters") or {}
            if intent in ("idle_gpu", "machine_catalog"):
                filters = normalized_resource_filters(filters)
                if intent == "machine_catalog":
                    filters["idle_only"] = False
                    filters["min_free_gpu_memory_gb"] = None
            elif intent == "top_users":
                filters = normalized_top_user_filters(filters)
            else:
                filters = {}
            actions.append(
                {
                    "intent": intent,
                    "argument": str(plan.get("argument") or "")[:100],
                    "filters": filters,
                    "query": normalized_resource_query(plan.get("query")) if intent == "resource_query" else {},
                }
            )
        context = {
            "user_text": clean_command(user_text)[:500],
            "actions": actions,
        }
        with self.lock:
            self.values[key] = (context, time.time())
            self.values.move_to_end(key)
            while len(self.values) > self.limit:
                self.values.popitem(last=False)


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


def normalized_gpu_count(value):
    if value in (None, ""):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 1 <= count <= 16 else None


def machine_type_label(count):
    return {1: "普通机器", 4: "四卡机", 8: "八卡机"}.get(int(count), "%d卡机" % int(count))


def normalized_resource_filters(value=None):
    source = value if isinstance(value, dict) else {"gpu_count": value}
    try:
        min_free = float(source.get("min_free_gpu_memory_gb"))
        min_free = min(max(min_free, 0), 640)
        if min_free <= 0:
            min_free = None
    except (TypeError, ValueError):
        min_free = None
    idle_value = source.get("idle_only")
    idle_only = idle_value is True or str(idle_value).strip().lower() in ("1", "true", "yes")
    return {
        "gpu_count": normalized_gpu_count(source.get("gpu_count")),
        "group": " ".join(str(source.get("group") or "").strip().split())[:80] or None,
        "idle_only": idle_only,
        "min_free_gpu_memory_gb": min_free,
        "gpu_model": " ".join(str(source.get("gpu_model") or "").strip().split())[:80] or None,
    }


def snapshot_inventory(snapshot):
    counts = OrderedDict()
    groups = OrderedDict()
    machines = []
    for result in snapshot.get("results") or []:
        if result.get("status") != "online":
            continue
        devices = ((((result.get("metrics") or {}).get("gpu") or {}).get("devices")) or [])
        if not devices:
            continue
        counts[len(devices)] = counts.get(len(devices), 0) + 1
        group = str(result.get("group") or "未分组")[:80]
        groups[group] = groups.get(group, 0) + 1
        models = sorted({str(device.get("name") or "GPU")[:100] for device in devices})
        machines.append(
            {
                "id": str(result.get("id") or "")[:100],
                "name": str(result.get("name") or result.get("id") or "")[:140],
                "group": group,
                "gpu_count": len(devices),
                "gpu_models": models,
            }
        )
    return {
        "machine_types": [
            {"gpu_count": count, "label": machine_type_label(count), "online_machines": amount}
            for count, amount in sorted(counts.items())
        ],
        "groups": [{"name": name, "online_gpu_machines": amount} for name, amount in groups.items()],
        "machines": machines[:60],
    }


def has_resource_filters(filters):
    return any(
        (
            filters.get("gpu_count"),
            filters.get("group"),
            filters.get("idle_only"),
            filters.get("min_free_gpu_memory_gb") is not None,
            filters.get("gpu_model"),
        )
    )


def gpu_machine_rows(snapshot, query=None):
    filters = normalized_resource_filters(query)
    expected_count = filters["gpu_count"]
    min_free_bytes = (filters["min_free_gpu_memory_gb"] or 0) * 1024**3
    group_query = str(filters["group"] or "").lower()
    model_query = str(filters["gpu_model"] or "").lower()
    rows = []
    for result in snapshot.get("results") or []:
        if result.get("status") != "online":
            continue
        metrics = result.get("metrics") or {}
        devices = (((metrics.get("gpu") or {}).get("devices")) or [])
        if not devices or (expected_count and len(devices) != expected_count):
            continue
        if group_query and group_query not in str(result.get("group") or "").lower():
            continue
        device_rows = []
        for device in devices:
            total = as_number(device.get("memory_total_bytes"))
            used = as_number(device.get("memory_used_bytes"))
            util = as_number(device.get("utilization_percent"))
            free = max(total - used, 0)
            idle = util <= 10 and (total <= 0 or used / total <= 0.10)
            model_matches = not model_query or model_query in str(device.get("name") or "").lower()
            capacity_matches = not min_free_bytes or free >= min_free_bytes
            idle_matches = not filters["idle_only"] or idle
            device_rows.append(
                {
                    "index": device.get("index"),
                    "name": device.get("name") or "GPU",
                    "free": free,
                    "total": total,
                    "util": util,
                    "idle": idle,
                    "matches": model_matches and capacity_matches and idle_matches,
                }
            )
        matching_count = sum(1 for device in device_rows if device["matches"])
        if has_resource_filters(filters) and not matching_count:
            continue
        rows.append(
            {
                "server": result.get("name") or result.get("id"),
                "host": result.get("host") or "-",
                "group": result.get("group") or "未分组",
                "gpu_count": len(device_rows),
                "idle_count": sum(1 for device in device_rows if device["idle"]),
                "matching_count": matching_count,
                "free": sum(device["free"] for device in device_rows),
                "total": sum(device["total"] for device in device_rows),
                "cpu": (metrics.get("cpu") or {}).get("percent"),
                "memory": (metrics.get("memory") or {}).get("percent"),
                "devices": device_rows,
            }
        )
    rows.sort(key=lambda item: (-item["matching_count"], -item["idle_count"], -item["free"], item["server"]))
    return rows


def machine_catalog_text(snapshot, query=None):
    filters = normalized_resource_filters(query)
    filters["idle_only"] = False
    filters["min_free_gpu_memory_gb"] = None
    rows = gpu_machine_rows(snapshot, filters)
    if not rows:
        return "当前设备目录中没有符合条件的在线机器。"
    count = filters.get("gpu_count")
    if count:
        lines = ["%s是指单台机器配置 %d 张 GPU；当前有 %d 台：" % (machine_type_label(count), count, len(rows))]
    else:
        lines = ["当前在线 GPU 设备目录："]
    for row in rows[:20]:
        models = "、".join(sorted({device.get("name") or "GPU" for device in row.get("devices") or []}))
        lines.append("%s · %s · %d 张 %s" % (row["server"], row["group"], row["gpu_count"], models))
    return "\n".join(lines)[:3900]


def machine_catalog_card(snapshot, query=None):
    filters = normalized_resource_filters(query)
    filters["idle_only"] = False
    filters["min_free_gpu_memory_gb"] = None
    rows = gpu_machine_rows(snapshot, filters)
    count = filters.get("gpu_count")
    label = machine_type_label(count) if count else "GPU 设备"
    if not rows:
        return compact_card("设备目录 · %s" % label, "当前没有符合条件的在线机器。", template="grey")
    if not has_resource_filters(filters):
        groups = OrderedDict()
        for row in sorted(rows, key=lambda item: (item["gpu_count"], item["server"])):
            groups.setdefault(row["gpu_count"], []).append(row)
        sections = []
        for gpu_count, machines in groups.items():
            sections.append(
                {
                    "title": "%s · %d 台" % (machine_type_label(gpu_count), len(machines)),
                    "content": "\n".join("- **%s** · %s" % (row["server"], row["group"]) for row in machines),
                }
            )
        return compact_card(
            "GPU 设备目录",
            "当前在线 **%d** 台 GPU 机器，按单机 GPU 张数分类。" % len(rows),
            sections,
            template="blue",
        )
    sections = []
    for row in rows:
        models = "、".join(sorted({device.get("name") or "GPU" for device in row.get("devices") or []}))
        content = (
            "%s · `%s`\n配置 **%d 张 %s**\n当前空闲 **%d/%d** 张 · 总可用显存 **%s**"
            % (
                row["group"],
                row["host"],
                row["gpu_count"],
                models,
                row["idle_count"],
                row["gpu_count"],
                format_bytes(row["free"]),
            )
        )
        sections.append({"title": row["server"], "content": content})
    definition = (
        "**%s**是指单台机器配置 **%d 张 GPU**。当前设备目录中对应 **%d 台**机器。"
        % (label, count, len(rows))
        if count
        else "当前设备目录中找到 **%d 台**符合条件的机器。" % len(rows)
    )
    return compact_card("设备目录 · %s" % label, definition, sections, template="blue")


def resource_filter_description(filters):
    parts = []
    if filters.get("gpu_count"):
        parts.append(machine_type_label(filters["gpu_count"]))
    if filters.get("group"):
        parts.append(filters["group"])
    if filters.get("gpu_model"):
        parts.append("型号含 %s" % filters["gpu_model"])
    if filters.get("idle_only"):
        parts.append("当前空闲")
    if filters.get("min_free_gpu_memory_gb") is not None:
        parts.append("单卡可用显存至少 %g GB" % filters["min_free_gpu_memory_gb"])
    return " · ".join(parts) or "全部在线 GPU 机器"


def idle_gpu_text(snapshot, query=None):
    filters = normalized_resource_filters(query)
    rows = gpu_machine_rows(snapshot, filters)
    if not rows:
        return "当前没有符合以下条件的机器：%s。" % resource_filter_description(filters)
    if not has_resource_filters(filters):
        groups = OrderedDict()
        for row in sorted(rows, key=lambda item: (item["gpu_count"] not in (1, 4, 8), item["gpu_count"], item["server"])):
            groups.setdefault(row["gpu_count"], []).append(row)
        lines = ["空闲机器概览："]
        for count, machines in groups.items():
            idle = sum(item["idle_count"] for item in machines)
            total = sum(item["gpu_count"] for item in machines)
            lines.append("%s：%d 台，空闲 %d/%d 张卡" % (machine_type_label(count), len(machines), idle, total))
        lines.append("回复“那四卡机呢”可查看具体机器和每张卡。")
        return "\n".join(lines)[:3900]
    lines = ["资源匹配：%s" % resource_filter_description(filters)]
    for row in rows:
        lines.append(
            "%s\n符合 %d/%d 张卡 · 空闲 %d/%d · 可用显存 %s · CPU %s · 内存 %s"
            % (
                row["server"],
                row["matching_count"],
                row["gpu_count"],
                row["idle_count"],
                row["gpu_count"],
                format_bytes(row["free"]),
                format_percent(row["cpu"]),
                format_percent(row["memory"]),
            )
        )
    return "\n\n".join(lines)[:3900]


def idle_gpu_card(snapshot, query=None):
    filters = normalized_resource_filters(query)
    rows = gpu_machine_rows(snapshot, filters)
    if not rows:
        return compact_card(
            "机器资源匹配",
            "当前没有符合以下条件的机器：**%s**。" % resource_filter_description(filters),
            template="grey",
        )

    if not has_resource_filters(filters):
        groups = OrderedDict()
        ordered = sorted(rows, key=lambda item: (item["gpu_count"] not in (1, 4, 8), item["gpu_count"], item["server"]))
        for row in ordered:
            groups.setdefault(row["gpu_count"], []).append(row)
        sections = []
        for count, machines in groups.items():
            idle = sum(item["idle_count"] for item in machines)
            total = sum(item["gpu_count"] for item in machines)
            content = []
            for row in machines:
                content.append(
                    "- **%s** · 空闲 **%d/%d** · 可用显存 **%s**"
                    % (row["server"], row["idle_count"], row["gpu_count"], format_bytes(row["free"]))
                )
            sections.append(
                {
                    "title": "%s · %d 台 · 空闲 %d/%d" % (machine_type_label(count), len(machines), idle, total),
                    "content": "\n".join(content),
                }
            )
        return compact_card(
            "空闲机器概览",
            "共 **%d** 台在线 GPU 机器。展开类型查看机器；回复“那四卡机呢”可看逐卡详情。" % len(rows),
            sections,
            template="green",
        )

    matching_devices = sum(row["matching_count"] for row in rows)
    sections = []
    for row in rows:
        content = [
            "%s · `%s`\nCPU **%s** · 内存 **%s**"
            % (row["group"], row["host"], format_percent(row["cpu"]), format_percent(row["memory"]))
        ]
        for device in sorted(row["devices"], key=lambda item: (not item["matches"], item["index"])):
            content.append(
                "- GPU %s · **%s** · %s\n  可用 **%s / %s** · 算力 %s"
                % (
                    device["index"],
                    "符合" if device["matches"] else ("空闲" if device["idle"] else "占用"),
                    device["name"],
                    format_bytes(device["free"]),
                    format_bytes(device["total"]),
                    format_percent(device["util"]),
                )
            )
        sections.append(
            {
                "title": "%s · 符合 %d/%d" % (row["server"], row["matching_count"], row["gpu_count"]),
                "content": "\n".join(content),
            }
        )
    return compact_card(
        "机器资源匹配",
        "条件：**%s**\n找到 **%d** 台机器、**%d** 张符合条件的 GPU；点击机器查看每张卡。"
        % (resource_filter_description(filters), len(rows), matching_devices),
        sections,
        template="green" if matching_devices else "orange",
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


def model_form_card(models, direct=False):
    available = [item for item in models if item.get("enabled") and item.get("candidate")]
    if not available:
        return compact_card("模型服务", "当前没有已验证并开放的目录模型。", template="orange")
    options = []
    for model in available[:50]:
        label = "%s · %s GPU" % (model.get("name"), model.get("recommended_gpu_count") or 1)
        options.append(
            {
                "text": {"tag": "plain_text", "content": label[:100]},
                "value": str(model.get("key"))[:240],
            }
        )
    elements = [
        {"tag": "markdown", "content": "**目录模型**"},
        form_select("model_key", "模型", options, required=True, initial_option=options[0]["value"]),
        form_input("owner_name", "姓名", "真实姓名", required=True),
        form_input("duration_hours", "使用时长（小时）", "1-720", required=True, default_value="24"),
        form_select(
            "gpu_count",
            "GPU 数量",
            [
                {"text": {"tag": "plain_text", "content": "%d 张" % count}, "value": str(count)}
                for count in (1, 2, 4, 8)
            ],
            required=True,
            initial_option="1",
        ),
        form_input("purpose", "用途", "例如代码生成 API", required=True),
        {
            "tag": "button",
            "name": "submit",
            "form_action_type": "submit",
            "type": "primary",
            "width": "fill",
            "text": {"tag": "plain_text", "content": "立即部署" if direct else "提交申请"},
            "behaviors": [
                {"type": "callback", "value": {"action": "direct_model_deploy" if direct else "submit_model_request"}}
            ],
        },
    ]
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "orange" if direct else "blue",
            "title": {"tag": "plain_text", "content": "管理员部署模型" if direct else "申请模型 API"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "选择已开放的模型；已运行的模型会直接复用现有服务。",
                    "text_size": "notation",
                },
                {"tag": "form", "name": "model_form", "elements": elements},
            ]
        },
    }


def deployable_models_text(models):
    available = [item for item in models if item.get("enabled") and item.get("candidate")]
    if not available:
        return "当前没有已验证并开放的目录模型。"
    return "当前有 %d 个模型可以部署。" % len(available)


def model_category_label(category):
    return {
        "text": "文本",
        "vision": "多模态",
        "embedding": "嵌入",
        "audio": "语音",
        "gguf": "GGUF",
    }.get(str(category or ""), "其他")


def deployable_models_card(models, services_payload=None):
    available = [item for item in models if item.get("enabled") and item.get("candidate")]
    if not available:
        return compact_card("可部署模型", "当前没有已验证并开放的目录模型。", template="orange")
    services = (services_payload or {}).get("services") or []
    running = {
        item.get("model_key"): item
        for item in services
        if (item.get("runtime") or {}).get("status") == "running"
    }
    sections = []
    for model in available[:30]:
        service = running.get(model.get("key"))
        lines = [
            "类型 **%s** · 权重 **%s GB**" % (model_category_label(model.get("category")), model.get("weight_gib") or 0),
            "建议 **%s GPU** · API 名称 `%s`" % (
                model.get("recommended_gpu_count") or 1,
                model.get("served_model_name") or model.get("name"),
            ),
            "当前 **%s**" % ("已运行，可直接复用" if service else "未运行"),
        ]
        if service:
            lines.append(
                "机器 `%s` · GPU %s"
                % (
                    service.get("worker_name") or service.get("worker_id") or "-",
                    ", ".join(service.get("gpu_indices") or []) or "-",
                )
            )
        sections.append({"title": str(model.get("name") or model.get("key")), "content": "\n".join(lines)})
    return compact_card(
        "可部署模型",
        "共 **%d** 个已开放模型 · **%d** 个已有运行实例" % (len(available), len(running)),
        sections,
        template="blue",
    )


def model_runtime_label(runtime):
    status = str((runtime or {}).get("status") or "unknown")
    health = str((runtime or {}).get("health") or "unknown")
    if status == "running" and health == "healthy":
        return "运行正常"
    labels = {
        "worker_offline": "机器离线",
        "missing": "容器缺失",
        "stopped": "已停止",
        "exited": "已退出",
        "deploying": "部署中",
        "failed": "部署失败",
    }
    return labels.get(status, status)


def model_progress_stage_label(stage):
    return {
        "gpu_allocated": "GPU 已分配",
        "starting_vllm": "正在启动 vLLM",
        "registering_gateway": "正在注册 OneAPI",
        "running": "运行正常",
        "failed": "部署失败",
    }.get(str(stage or ""), str(stage or "等待调度"))


def model_services_text(payload):
    services = payload.get("services") or []
    if not payload.get("enabled"):
        return "模型部署功能当前未启用。"
    if not services:
        return "当前没有匹配的模型服务。"
    running = sum(1 for item in services if (item.get("runtime") or {}).get("status") == "running")
    return "共 %d 个模型服务，%d 个正在运行。" % (len(services), running)


def model_services_card(payload):
    services = payload.get("services") or []
    if not payload.get("enabled"):
        return compact_card("模型服务", "模型部署功能当前未启用。", template="orange")
    if not services:
        return compact_card("模型服务", "当前没有匹配的模型服务。", template="grey")
    panels = []
    healthy = 0
    for service in services[:20]:
        runtime = service.get("runtime") or {}
        if runtime.get("status") == "running" and runtime.get("health") == "healthy":
            healthy += 1
        gpu_text = ", ".join("GPU %s" % value for value in service.get("gpu_indices") or []) or "-"
        details = [
            "状态 **%s** · 授权 **%d** 人" % (model_runtime_label(runtime), service.get("active_allocations") or 0),
            "机器 `%s` · %s" % (service.get("worker_name") or service.get("worker_id") or "-", gpu_text),
            "容器 `%s` · 端口 `%s`" % (service.get("container_name") or "-", service.get("host_port") or "-"),
        ]
        if service.get("status") == "deploying":
            details.insert(
                1,
                "进度 **%d%%** · `%s`"
                % (service.get("progress_percent") or 0, model_progress_stage_label(service.get("progress_stage"))),
            )
        resources = []
        if runtime.get("gpu_memory_used_bytes") is not None:
            resources.append("显存 %s" % format_bytes(runtime.get("gpu_memory_used_bytes")))
        if runtime.get("memory_used_bytes") is not None:
            resources.append("内存 %s" % format_bytes(runtime.get("memory_used_bytes")))
        if runtime.get("cpu_percent") is not None:
            resources.append("CPU %s" % format_percent(runtime.get("cpu_percent")))
        if resources:
            details.append(" · ".join(resources))
        panels.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {"tag": "plain_text", "content": str(service.get("served_name") or service.get("model_name"))[:100]},
                    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "border": {"color": "grey", "corner_radius": "5px"},
                "elements": [{"tag": "markdown", "content": "\n".join(details)}],
            }
        )
    summary = "共 **%d** 个服务 · **%d** 个运行正常" % (len(services), healthy)
    if payload.get("api_base_url"):
        summary += "\nAPI 地址 `%s`" % payload.get("api_base_url")
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {"template": "green" if healthy == len(services) else "orange", "title": {"tag": "plain_text", "content": "模型服务状态"}},
        "body": {"elements": [{"tag": "markdown", "content": summary}, *panels]},
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
        is_model = item.get("access_type") == "api" and bool(item.get("model_key"))
        owner = item.get("owner_name") or item.get("requester_display_name") or item.get("requester")
        if is_model:
            details = (
                "姓名 **%s** · 类型 **模型 API**\n模型 `%s` · GPU %s 张\n时长 %s 小时\n%s"
                % (
                    owner,
                    item.get("model_name") or item.get("model_key") or "-",
                    item.get("gpu_count") or "自动",
                    item.get("duration_hours") or "-",
                    item.get("purpose") or "",
                )
            )
            approve_label = "通过并部署"
            approve_action = "approve_model_request"
        else:
            details = (
                "姓名 **%s** · 类型 **%s**\n机器 `%s` · 账号 `%s`\n模型/任务 %s · 时长 %s 小时\n%s"
                % (
                    owner,
                    "长期" if item.get("request_type") == "access" else "临时",
                    item.get("target_machine_label") or item.get("target_machine") or "待分配",
                    item.get("requested_account") or "自动生成",
                    item.get("model_name") or "-",
                    item.get("duration_hours") or "-",
                    item.get("purpose") or "",
                )
            )
            approve_label = "通过并开通"
            approve_action = "approve_request"
        panels.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {"tag": "plain_text", "content": "#%s · %s" % (item.get("id"), owner)},
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
                            {"tag": "column", "width": "weighted", "weight": 1, "elements": [callback_button(approve_label, approve_action, item.get("id"), "primary")]},
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
                {"tag": "markdown", "content": "共 **%d** 条待处理；展开后确认资源和用途。" % len(requests)},
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
        self.context = ConversationMemory()
        self.admin_open_ids = {
            value.strip() for value in os.getenv("FEISHU_ADMIN_OPEN_IDS", "").split(",") if value.strip()
        }
        self.jobs = queue.Queue(maxsize=100)
        self.router = LLMIntentRouter(
            base_url=os.getenv("FEISHU_BOT_LLM_BASE_URL"),
            api_key=os.getenv("FEISHU_BOT_LLM_API_KEY"),
            model=os.getenv("FEISHU_BOT_LLM_MODEL"),
            timeout=os.getenv("FEISHU_BOT_LLM_TIMEOUT_SECONDS", "20"),
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

    def model_request_payload(self, form):
        model_key = form.get("model_key", "").strip()
        owner_name = form.get("owner_name", "").strip()
        purpose = form.get("purpose", "").strip()
        if not model_key or not owner_name or not purpose:
            raise ValueError("模型、姓名和用途不能为空")
        try:
            duration = int(form.get("duration_hours") or 0)
            gpu_count = int(form.get("gpu_count") or 0)
        except ValueError:
            raise ValueError("时长和 GPU 数量必须是整数") from None
        if duration < 1 or duration > 720:
            raise ValueError("使用时长必须在 1 到 720 小时之间")
        if gpu_count not in (1, 2, 4, 8):
            raise ValueError("GPU 数量无效")
        model = next((item for item in self.dashboard.models() if item.get("key") == model_key), None)
        if not model:
            raise ValueError("该模型当前未开放部署")
        return {
            "request_type": "temporary",
            "owner_name": owner_name[:80],
            "model_key": model_key[:240],
            "model_name": str(model.get("served_model_name") or model.get("name"))[:160],
            "model_size": "%s GB weights" % (model.get("weight_gib") or 0),
            "purpose": purpose[:2000],
            "access_type": "api",
            "gpu_count": max(gpu_count, int(model.get("recommended_gpu_count") or 1)),
            "gpu_memory_gb": None,
            "duration_hours": duration,
            "target_machine": "",
            "target_machine_label": "自动调度",
            "requested_account": "",
            "requested_password": "",
            "notes": "Submitted through Feishu model service form",
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
            if action_name == "submit_model_request":
                payload = self.model_request_payload(form)
                request_id = self.workflow.create_request(open_id, chat_id, payload)
                for admin_open_id in self.admin_open_ids:
                    try:
                        self.send_to_open_id(
                            admin_open_id,
                            "收到新的模型 API 申请 #%s：%s · %s。发送“待审批”查看。"
                            % (request_id, payload["owner_name"], payload["model_name"]),
                        )
                    except Exception:
                        pass
                return self.callback_response("模型申请 #%s 已提交" % request_id)
            if action_name in (
                "direct_provision",
                "approve_request",
                "approve_model_request",
                "direct_model_deploy",
                "reject_request",
            ):
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
        credential = result.get("credential") or {}
        service = result.get("service") or {}
        allocation = result.get("allocation") or {}
        if credential:
            lines = [
                "模型 API 已开通%s" % ("（复用现有服务）" if result.get("reused") else ""),
                "模型：%s" % (credential.get("model") or service.get("served_name") or "-"),
                "机器：%s" % (service.get("worker_name") or service.get("worker_id") or "-"),
                "GPU：%s" % (", ".join("GPU %s" % value for value in service.get("gpu_indices") or []) or "-"),
                "Base URL：%s" % (credential.get("base_url") or "-"),
                "API Key：%s" % (credential.get("api_key") or "-"),
                "到期：%s" % (credential.get("expires_at") or allocation.get("expires_at") or "长期有效"),
            ]
            if credential.get("access_hint"):
                lines.append("连接：%s" % credential.get("access_hint"))
            return "\n".join(lines)
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
                elif action == "direct_model_deploy":
                    payload = self.model_request_payload(job["form"])
                    result = self.dashboard.deploy_model(
                        {
                            "model_key": payload["model_key"],
                            "owner_name": payload["owner_name"],
                            "duration_hours": payload["duration_hours"],
                            "gpu_count": payload["gpu_count"],
                        }
                    )
                    self.send_to_open_id(job["operator_open_id"], self.result_text(result or {}))
                elif action == "approve_model_request":
                    request_id = int(job["request_id"])
                    result = self.dashboard.deploy_requested_model(request_id)
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
                    operation = "模型操作" if "model" in str(job.get("action") or "") else "账号操作"
                    self.send_to_open_id(job.get("operator_open_id"), "%s失败：%s" % (operation, str(exc)[:300]))
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

    def execute_plan(self, plan, sender_id, snapshot):
        command = plan.get("intent") or "help"
        argument = plan.get("argument") or ""
        if command == "person":
            if not argument:
                answer = "请指定姓名或账号，例如：查人 崔涵帅"
                return answer, compact_card("缺少用户", answer, template="orange")
            payload = self.dashboard.person(argument)
            return person_text(payload), person_card(payload)
        if command == "machine":
            snapshot = snapshot or self.dashboard.snapshot()
            return machine_text(snapshot, argument), machine_card(snapshot, argument)
        if command == "machine_catalog":
            snapshot = snapshot or self.dashboard.snapshot()
            filters = plan.get("filters") or {}
            return machine_catalog_text(snapshot, filters), machine_catalog_card(snapshot, filters)
        if command == "idle_gpu":
            snapshot = snapshot or self.dashboard.snapshot()
            filters = plan.get("filters") or {}
            return idle_gpu_text(snapshot, filters), idle_gpu_card(snapshot, filters)
        if command == "resource_query":
            payload = self.dashboard.resource_query(plan.get("query") or {})
            return resource_query_text(payload), resource_query_card(payload)
        if command == "top_users":
            filters = plan.get("filters") or {}
            payload = self.dashboard.top_users(30)
            return top_users_text(payload, filters), top_users_card(payload, filters)
        if command == "request_account":
            answer = "请在卡片中填写账号申请。"
            return answer, account_form_card(self.dashboard.machines(), direct=False)
        if command == "pending_requests":
            if not self.is_admin(sender_id):
                answer = "仅管理员可以查看待审批申请。"
                return answer, compact_card("无管理员权限", answer, template="red")
            requests = self.workflow.pending_requests()
            answer = "当前有 %d 条待审批申请。" % len(requests)
            return answer, pending_requests_card(requests)
        if command == "provision_account":
            if not self.is_admin(sender_id):
                answer = "仅管理员可以主动开通账号。"
                return answer, compact_card("无管理员权限", answer, template="red")
            answer = "请在卡片中填写开通信息。"
            return answer, account_form_card(self.dashboard.machines(), direct=True)
        if command == "request_model":
            models = self.dashboard.models()
            answer = "请在卡片中选择模型并填写用途。"
            return answer, model_form_card(models, direct=False)
        if command == "model_catalog":
            models = self.dashboard.models()
            services = self.dashboard.model_services()
            cards = [deployable_models_card(models, services)]
            if models:
                cards.append(model_form_card(models, direct=self.is_admin(sender_id)))
            return deployable_models_text(models), combine_cards(cards)
        if command == "model_status":
            payload = self.dashboard.model_services(plan.get("argument") or "")
            return model_services_text(payload), model_services_card(payload)
        if command == "deploy_model":
            if not self.is_admin(sender_id):
                answer = "仅管理员可以直接部署模型；你可以发送“申请模型”。"
                return answer, compact_card("无管理员权限", answer, template="red")
            models = self.dashboard.models()
            answer = "请在卡片中选择要部署的目录模型。"
            return answer, model_form_card(models, direct=True)
        if command == "admin_disabled":
            answer = "该写操作尚未开放，请使用“申请账号”“待审批”或“开通账号”。"
            return answer, compact_card("操作尚未开放", answer, template="orange")
        if command == "clarify":
            answer = plan.get("clarification") or "请再补充一下要查询的人、机器或资源条件。"
            return answer, compact_card("需要确认", answer, template="orange")
        if command == "chat":
            answer = plan.get("clarification") or "我看到了你的消息。"
            return answer, compact_card("设备资源助手", answer, template="blue")
        return HELP_TEXT, compact_card("设备资源助手", HELP_TEXT, template="blue")

    def on_message(self, data):
        message = data.event.message
        message_id = getattr(message, "message_id", "")
        if not self.deduplicator.accept(message_id):
            return
        sender_id = getattr(getattr(data.event.sender, "sender_id", None), "open_id", "")
        chat_id = getattr(message, "chat_id", "")
        print("message received sender_open_id=%s" % (sender_id or "unknown"), flush=True)
        try:
            context_key = (sender_id, chat_id)
            user_text = self.event_text(data)
            try:
                snapshot = self.dashboard.snapshot()
            except Exception:
                snapshot = {}
            plans = self.router.plans(
                user_text,
                previous=self.context.get(context_key),
                inventory=snapshot_inventory(snapshot),
            )
            self.context.remember(context_key, user_text, plans)
            answers = []
            cards = []
            for plan in plans:
                try:
                    current_answer, current_card = self.execute_plan(plan, sender_id, snapshot)
                except Exception as exc:
                    print("action failed intent=%s error=%s" % (plan.get("intent"), type(exc).__name__), flush=True)
                    current_answer = "其中一项查询暂时失败，请稍后重试。"
                    current_card = compact_card("查询失败", current_answer, template="red")
                answers.append(current_answer)
                cards.append(current_card)
            answer = "\n\n".join(answers)[:3900]
            card = combine_cards(cards)
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
