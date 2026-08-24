import unittest

from server_probe.app import request_client_ip


class ProxyAddressTests(unittest.TestCase):
    def test_loopback_proxy_can_supply_valid_real_ip(self):
        self.assertEqual(request_client_ip("127.0.0.1", "203.0.113.8"), "203.0.113.8")

    def test_direct_client_cannot_spoof_forwarded_address(self):
        self.assertEqual(request_client_ip("192.0.2.10", "203.0.113.8"), "192.0.2.10")

    def test_invalid_proxy_header_falls_back_to_peer(self):
        self.assertEqual(request_client_ip("127.0.0.1", "spoofed, 203.0.113.8"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
