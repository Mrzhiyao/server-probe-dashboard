import json
import unittest

from server_probe.feishu_bot import LLMIntentRouter, MessageDeduplicator, clean_command, compact_card, parse_command


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

    def test_admin_commands_are_disabled(self):
        self.assertEqual(parse_command("帮我开账号"), ("admin_disabled", ""))

    def test_duplicate_message_is_rejected(self):
        values = MessageDeduplicator()
        self.assertTrue(values.accept("message-1"))
        self.assertFalse(values.accept("message-1"))

    def test_compact_card_uses_collapsible_panels(self):
        card = compact_card("查询结果", "摘要", [{"title": "机器 1", "content": "详细内容"}])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["body"]["elements"][1]["tag"], "collapsible_panel")
        self.assertFalse(card["body"]["elements"][1]["expanded"])

    def test_llm_is_only_used_for_unknown_natural_language(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(
                {"choices": [{"message": {"content": '{"intent":"person","argument":"崔涵帅"}'}}]}
            )

        router = LLMIntentRouter("https://newapi.example", "secret", "small-model", opener=opener)
        self.assertEqual(router.route("帮我看看崔涵帅现在用了哪些卡"), ("person", "崔涵帅"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(router.route("查机 gpu010"), ("machine", "gpu010"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
