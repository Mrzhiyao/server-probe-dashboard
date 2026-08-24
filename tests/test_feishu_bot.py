import json
import unittest

from server_probe.feishu_bot import (
    LLMIntentRouter,
    MessageDeduplicator,
    account_form_card,
    clean_command,
    compact_card,
    idle_gpu_card,
    parse_command,
    pending_requests_card,
    snapshot_inventory,
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
        card = idle_gpu_card(snapshot, {"gpu_count": 4, "idle_only": True})
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("four-gpu", raw)
        self.assertNotIn("eight-gpu", raw)
        self.assertIn("四卡机", raw)

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


if __name__ == "__main__":
    unittest.main()
