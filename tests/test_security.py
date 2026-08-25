import unittest

from server_probe.app import DashboardHandler, cookie_secure_mode, forwarded_request_is_https, request_client_ip


class ProxyAddressTests(unittest.TestCase):
    def test_loopback_proxy_can_supply_valid_real_ip(self):
        self.assertEqual(request_client_ip("127.0.0.1", "203.0.113.8"), "203.0.113.8")

    def test_direct_client_cannot_spoof_forwarded_address(self):
        self.assertEqual(request_client_ip("192.0.2.10", "203.0.113.8"), "192.0.2.10")

    def test_invalid_proxy_header_falls_back_to_peer(self):
        self.assertEqual(request_client_ip("127.0.0.1", "spoofed, 203.0.113.8"), "127.0.0.1")


class SessionCookieTests(unittest.TestCase):
    def test_auto_mode_is_preserved(self):
        self.assertEqual(cookie_secure_mode("auto"), "auto")

    def test_boolean_modes_remain_compatible(self):
        self.assertEqual(cookie_secure_mode(True), "always")
        self.assertEqual(cookie_secure_mode("0"), "never")

    def test_forwarded_https_is_detected(self):
        self.assertTrue(forwarded_request_is_https({"X-Forwarded-Proto": "https"}))
        self.assertTrue(forwarded_request_is_https({"Forwarded": "for=192.0.2.1;proto=https;host=example.test"}))

    def test_direct_http_has_no_https_signal(self):
        self.assertFalse(forwarded_request_is_https({}))
        self.assertFalse(forwarded_request_is_https({"X-Forwarded-Proto": "http"}))

    def test_auto_cookie_flag_follows_forwarded_scheme(self):
        handler = object.__new__(DashboardHandler)
        handler.cookie_secure_mode = "auto"
        handler.headers = {}
        self.assertNotIn("Secure", handler.cookie_header("token", 60))
        handler.headers = {"X-Forwarded-Proto": "https"}
        self.assertIn("Secure", handler.cookie_header("token", 60))


if __name__ == "__main__":
    unittest.main()
