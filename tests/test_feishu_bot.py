import json
import unittest

from server_probe.feishu_bot import (
    LLMIntentRouter,
    MessageDeduplicator,
    account_form_card,
    clean_command,
    combine_cards,
    compact_card,
    deployable_models_card,
    idle_gpu_card,
    machine_catalog_card,
    model_form_card,
    model_services_card,
    normalized_resource_query,
    parse_command,
    pending_requests_card,
    resource_query_card,
    snapshot_inventory,
    top_users_card,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return json.dumps(self.payload).encode()


class FeishuCommandTests(unittest.TestCase):
    def test_group_mention_is_removed(self):
        self.assertEqual(clean_command("@_user_1 查人  崔涵帅"), "查人 崔涵帅")

    def test_supported_commands(self):
        self.assertEqual(parse_command("查人 崔涵帅"), ("person", "崔涵帅"))
        self.assertEqual(parse_command("查机 gpu010"), ("machine", "gpu010"))
        self.assertEqual(parse_command("空闲 GPU"), ("idle_gpu", ""))

    def test_account_commands_open_guarded_workflows(self):
        self.assertEqual(parse_command("帮我开账号"), ("provision_account", ""))
        self.assertEqual(parse_command("申请账号"), ("request_account", ""))
        self.assertEqual(parse_command("删除账号"), ("admin_disabled", ""))

    def test_model_commands_open_managed_workflows(self):
        self.assertEqual(parse_command("申请模型"), ("request_model", ""))
        self.assertEqual(parse_command("模型状态"), ("model_status", ""))
        self.assertEqual(parse_command("创建模型"), ("deploy_model", ""))
        self.assertEqual(parse_command("你会部署模型吗"), ("model_catalog", ""))
        self.assertEqual(parse_command("你目前能部署哪些模型"), ("model_catalog", ""))
        self.assertEqual(parse_command("有哪些模型正在运行"), ("model_status", ""))

    def test_model_workflows_use_llm_as_primary_planner(self):
        calls = []

        def opener(request, timeout):
            prompt = json.loads(json.loads(request.data)["messages"][1]["content"])
            calls.append(prompt["text"])
            intent = "deploy_model" if prompt["text"] == "请部署模型" else "model_catalog"
            return FakeResponse(
                {"choices": [{"message": {"content": json.dumps({"intent": intent, "argument": "", "filters": {}})}}]}
            )

        router = LLMIntentRouter("https://newapi.example", "secret", "small-model", opener=opener)
        self.assertEqual(router.plan("你目前能部署哪些模型")["intent"], "model_catalog")
        self.assertEqual(router.plan("请部署模型")["intent"], "deploy_model")
        self.assertEqual(router.plan("现在哪些权重适合直接挂成推理接口")["intent"], "model_catalog")
        self.assertEqual(calls, ["你目前能部署哪些模型", "请部署模型", "现在哪些权重适合直接挂成推理接口"])

    def test_known_model_workflow_falls_back_when_llm_times_out(self):
        calls = []

        def opener(request, timeout):
            calls.append(timeout)
            raise TimeoutError("provider timeout")

        router = LLMIntentRouter("https://newapi.example", "secret", "small-model", timeout=3, opener=opener)
        self.assertEqual(router.plan("你目前能部署哪些模型")["intent"], "model_catalog")
        self.assertEqual(calls, [3.0])

    def test_duplicate_message_is_rejected(self):
        values = MessageDeduplicator()
        self.assertTrue(values.accept("message-1"))
        self.assertFalse(values.accept("message-1"))

    def test_compact_card_uses_collapsible_panels(self):
        card = compact_card("查询结果", "摘要", [{"title": "机器 1", "content": "详细内容"}])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["body"]["elements"][1]["tag"], "collapsible_panel")
        self.assertFalse(card["body"]["elements"][1]["expanded"])

    def test_llm_plans_queries_when_enabled(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            body = json.loads(request.data)
            prompt = json.loads(body["messages"][1]["content"])
            if "gpu010" in prompt["text"]:
                content = '{"intent":"machine","argument":"gpu010","filters":{},"clarification":""}'
            else:
                content = '{"intent":"person","argument":"崔涵帅","filters":{},"clarification":""}'
            return FakeResponse(
                {"choices": [{"message": {"content": content}}]}
            )

        router = LLMIntentRouter("https://newapi.example", "secret", "small-model", opener=opener)
        self.assertEqual(router.route("帮我看看崔涵帅现在用了哪些卡"), ("person", "崔涵帅"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(router.route("查机 gpu010"), ("machine", "gpu010"))
        self.assertEqual(len(calls), 2)

    def test_llm_extracts_resource_filters_and_receives_context(self):
        requests = []

        def opener(request, timeout):
            body = json.loads(request.data)
            requests.append(json.loads(body["messages"][1]["content"]))
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "idle_gpu",
                                        "argument": "",
                                        "filters": {
                                            "gpu_count": 4,
                                            "group": "C101设备",
                                            "idle_only": True,
                                            "min_free_gpu_memory_gb": 20,
                                            "gpu_model": None,
                                        },
                                        "clarification": "",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )

        router = LLMIntentRouter("https://newapi.example", "secret", "small-model", opener=opener)
        previous = {"user_text": "有哪些空闲机器", "plan": {"intent": "idle_gpu", "filters": {}}}
        plan = router.plan(
            "那四卡机呢",
            previous=previous,
            inventory={"machine_types": [{"gpu_count": 4, "online_machines": 5}]},
        )
        self.assertEqual(plan["intent"], "idle_gpu")
        self.assertEqual(plan["filters"]["gpu_count"], 4)
        self.assertEqual(plan["filters"]["min_free_gpu_memory_gb"], 20)
        self.assertEqual(requests[0]["previous"], previous)

    def test_llm_preserves_multiple_questions_in_one_message(self):
        def opener(request, timeout):
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "actions": [
                                            {"intent": "help", "argument": "", "filters": {}},
                                            {
                                                "intent": "top_users",
                                                "argument": "",
                                                "filters": {"limit": 8, "resource": "all"},
                                            },
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )

        router = LLMIntentRouter("https://newapi.example", "secret", "small-model", opener=opener)
        plans = router.plans("你还会啥？有哪些用户最近使用机器量很大")
        self.assertEqual([plan["intent"] for plan in plans], ["help", "top_users"])
        self.assertEqual(plans[1]["filters"], {"limit": 8, "resource": "all"})
        chat = router.normalize_action({"intent": "chat", "response": "我看到了上一条里的两个问题。"})
        self.assertEqual(chat["intent"], "chat")
        self.assertEqual(chat["clarification"], "我看到了上一条里的两个问题。")

    def test_resource_card_uses_model_filters_against_live_data_shape(self):
        def machine(name, count):
            return {
                "id": name,
                "name": name,
                "host": name,
                "group": "C101设备",
                "status": "online",
                "metrics": {
                    "cpu": {"percent": 2},
                    "memory": {"percent": 10},
                    "gpu": {
                        "devices": [
                            {
                                "index": index,
                                "name": "NVIDIA RTX",
                                "memory_total_bytes": 24 * 1024**3,
                                "memory_used_bytes": 1024**2,
                                "utilization_percent": 0,
                            }
                            for index in range(count)
                        ]
                    },
                },
            }

        snapshot = {"results": [machine("four-gpu", 4), machine("eight-gpu", 8)]}
        inventory = snapshot_inventory(snapshot)
        self.assertEqual([item["gpu_count"] for item in inventory["machine_types"]], [4, 8])
        self.assertEqual(inventory["machines"][1]["name"], "eight-gpu")
        card = idle_gpu_card(snapshot, {"gpu_count": 4, "idle_only": True})
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("four-gpu", raw)
        self.assertNotIn("eight-gpu", raw)
        self.assertIn("四卡机", raw)
        catalog = json.dumps(machine_catalog_card(snapshot, {"gpu_count": 8}), ensure_ascii=False)
        self.assertIn("八卡机", catalog)
        self.assertIn("eight-gpu", catalog)
        self.assertNotIn("four-gpu", catalog)

    def test_catalog_action_is_grounded_in_inventory_filters(self):
        router = LLMIntentRouter()
        plan = router.normalize_action(
            {
                "intent": "machine_catalog",
                "filters": {"gpu_count": 8, "idle_only": True, "min_free_gpu_memory_gb": 40},
            }
        )
        self.assertEqual(plan["filters"]["gpu_count"], 8)
        self.assertFalse(plan["filters"]["idle_only"])
        self.assertIsNone(plan["filters"]["min_free_gpu_memory_gb"])

    def test_general_resource_query_is_normalized_and_rendered(self):
        query = normalized_resource_query(
            {"entity": "process", "metric": "gpu_memory", "limit": 50, "scope": {"gpu_count": 8}}
        )
        self.assertEqual(query["limit"], 15)
        self.assertEqual(query["scope"]["gpu_count"], 8)
        card = resource_query_card(
            {
                "query": query,
                "matched_machines": [{"name": "gpu010"}],
                "rows": [
                    {
                        "server_name": "gpu010",
                        "pid": 9790,
                        "user": "root",
                        "process_name": "VLLM::Worker_PP",
                        "container_name": "deepseek-v4-flash-0731",
                        "model": "DeepSeek-V4-Flash-0731",
                        "gpu_indices": ["2"],
                        "gpu_memory_bytes": 70 * 1024**3,
                        "runtime_seconds": 3600,
                    }
                ],
            }
        )
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("八卡机", raw)
        self.assertIn("deepseek-v4-flash-0731", raw)
        self.assertIn("70.0 GB", raw)

    def test_account_form_and_approval_cards_use_callbacks(self):
        machines = [{"id": "gpu010", "name": "GPU10", "host": "192.0.2.10"}]
        form_card = account_form_card(machines)
        raw = json.dumps(form_card, ensure_ascii=False)
        self.assertIn("submit_request", raw)
        self.assertIn("target_machine", raw)
        approval = pending_requests_card(
            [
                {
                    "id": 7,
                    "owner_name": "Alice",
                    "request_type": "temporary",
                    "target_machine": "gpu010",
                    "model_name": "Qwen",
                    "duration_hours": 24,
                }
            ]
        )
        approval_raw = json.dumps(approval, ensure_ascii=False)
        self.assertIn("approve_request", approval_raw)
        self.assertIn("reject_request", approval_raw)

    def test_model_form_status_and_approval_cards(self):
        models = [
            {
                "key": "Qwen-Test",
                "name": "Qwen Test",
                "served_model_name": "Qwen-Test",
                "enabled": True,
                "candidate": True,
                "recommended_gpu_count": 1,
            }
        ]
        request_form = json.dumps(model_form_card(models), ensure_ascii=False)
        direct_form = json.dumps(model_form_card(models, direct=True), ensure_ascii=False)
        self.assertIn("submit_model_request", request_form)
        self.assertIn("direct_model_deploy", direct_form)
        status = json.dumps(
            model_services_card(
                {
                    "enabled": True,
                    "api_base_url": "https://api.example/v1",
                    "services": [
                        {
                            "served_name": "Qwen-Test",
                            "worker_name": "GPU worker",
                            "container_name": "probe-qwen",
                            "gpu_indices": ["0"],
                            "host_port": 18001,
                            "active_allocations": 2,
                            "runtime": {"status": "running", "health": "healthy"},
                        }
                    ],
                }
            ),
            ensure_ascii=False,
        )
        self.assertIn("Qwen-Test", status)
        self.assertIn("运行正常", status)
        catalog = json.dumps(
            deployable_models_card(
                models,
                {
                    "services": [
                        {
                            "model_key": "Qwen-Test",
                            "worker_name": "GPU worker",
                            "gpu_indices": ["0"],
                            "runtime": {"status": "running", "health": "healthy"},
                        }
                    ]
                },
            ),
            ensure_ascii=False,
        )
        self.assertIn("已运行，可直接复用", catalog)
        approval = json.dumps(
            pending_requests_card(
                [
                    {
                        "id": 8,
                        "owner_name": "Alice",
                        "request_type": "temporary",
                        "access_type": "api",
                        "model_key": "Qwen-Test",
                        "model_name": "Qwen-Test",
                        "gpu_count": 1,
                        "duration_hours": 24,
                    }
                ]
            ),
            ensure_ascii=False,
        )
        self.assertIn("approve_model_request", approval)
        self.assertIn("通过并部署", approval)

    def test_top_user_card_can_be_combined_with_help(self):
        ranking = top_users_card(
            {
                "window": "本小时至今",
                "sample_count": 12,
                "active_users": 3,
                "people": [
                    {
                        "display_name": "Alice",
                        "usernames": ["alice"],
                        "machine_count": 2,
                        "gpu_count": 3,
                        "gpu_memory_peak_bytes": 20 * 1024**3,
                        "memory_peak_bytes": 8 * 1024**3,
                        "cpu_average_sum": 40,
                        "resource_score": 72,
                        "machine_rows": [],
                    }
                ],
            }
        )
        card = combine_cards([compact_card("能力", "可以查询资源。"), ranking])
        raw = json.dumps(card, ensure_ascii=False)
        self.assertEqual(card["header"]["title"]["content"], "设备资源助手 · 2 项结果")
        self.assertIn("Alice", raw)
        self.assertIn("可以查询资源", raw)
        self.assertIn("<font color=", raw)


if __name__ == "__main__":
    unittest.main()
