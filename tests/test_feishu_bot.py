import unittest

from server_probe.feishu_bot import MessageDeduplicator, clean_command, parse_command


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


if __name__ == "__main__":
    unittest.main()
