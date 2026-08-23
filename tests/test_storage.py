import unittest
from unittest import mock

from server_probe import collector
from server_probe.app import Monitor


MOUNTINFO_ROOT = "24 1 8:2 / / rw,relatime - ext4 /dev/sda2 rw,errors=remount-ro"
MOUNTINFO_AUTOFS = "40 24 0:45 / /nas rw,relatime - autofs systemd-1 rw,fd=51,pgrp=1"
MOUNTINFO_CIFS = "41 40 0:46 / /nas rw,relatime - cifs //p8.example/share/Ray rw,vers=3.1.1"
FSTAB = "//p8.example/share/Ray /nas cifs credentials=/root/private,x-systemd.automount,_netdev 0 0\n"


class CollectorStorageTests(unittest.TestCase):
    def test_decode_mount_field(self):
        self.assertEqual(collector.decode_mount_field("/path\\040with\\040spaces"), "/path with spaces")

    def test_parse_diskstats_and_rate(self):
        before = collector.diskstats_snapshot("8 2 sda2 10 0 100 0 5 0 200 0 0 100 0 0 0 0")
        after = collector.diskstats_snapshot("8 2 sda2 20 0 300 0 8 0 500 0 0 600 0 0 0 0")
        rate = collector.disk_io_rate(before, after, 2.0, "8:2")
        self.assertEqual(rate["read_bytes_per_second"], 51200.0)
        self.assertEqual(rate["write_bytes_per_second"], 76800.0)
        self.assertEqual(rate["busy_percent"], 25.0)

    def test_parse_disconnected_cifs_session(self):
        data = """1) ConnectionId: 0x1 Hostname: p8.example\nTCP status: 5 Instance: 2\nDISCONNECTED\n"""
        servers = collector.parse_cifs_debug(data)
        self.assertFalse(servers["p8.example"]["connected"])
        self.assertEqual(servers["p8.example"]["tcp_status"], 5)

    def test_network_source_host(self):
        self.assertEqual(collector.network_source_host("//nas.example/share/team", "cifs"), ("nas.example", 445))
        self.assertEqual(collector.network_source_host("192.0.2.10:/exports/data", "nfs"), ("192.0.2.10", 2049))

    def storage(self, mountinfo):
        def fake_read(path, default=""):
            if path == "/proc/self/mountinfo":
                return mountinfo
            if path == "/etc/fstab":
                return FSTAB
            return default

        usage = {
            "total_bytes": 1000,
            "used_bytes": 500,
            "free_bytes": 500,
            "percent": 50.0,
            "inode_total": 100,
            "inode_used": 10,
            "inode_free": 90,
            "inode_percent": 10.0,
            "latency_ms": 4.0,
        }
        with mock.patch.object(collector, "read_first", side_effect=fake_read), mock.patch.object(
            collector, "filesystem_usage", return_value=usage
        ), mock.patch.object(collector, "network_mount_probe", return_value={"connection": "reachable", "latency_ms": 2.0}), mock.patch.object(
            collector, "sysfs_block_devices", return_value=[]
        ):
            return collector.storage_info()

    def test_real_cifs_wins_over_automount_placeholder(self):
        storage = self.storage("\n".join([MOUNTINFO_ROOT, MOUNTINFO_AUTOFS, MOUNTINFO_CIFS]))
        nas = next(item for item in storage["mounts"] if item["mount"] == "/nas")
        self.assertEqual(nas["status"], "mounted")
        self.assertEqual(nas["fstype"], "cifs")
        self.assertEqual(nas["kind"], "network")
        self.assertTrue(nas["automount"])
        self.assertEqual(storage["summary"]["mount_issue_count"], 0)

    def test_automount_without_real_filesystem_is_an_issue(self):
        storage = self.storage("\n".join([MOUNTINFO_ROOT, MOUNTINFO_AUTOFS]))
        nas = next(item for item in storage["mounts"] if item["mount"] == "/nas")
        self.assertEqual(nas["status"], "automount_only")
        self.assertEqual(nas["source"], "//p8.example/share/Ray")
        self.assertEqual(nas["fstype"], "cifs")
        self.assertEqual(storage["summary"]["mount_issue_count"], 1)

    def test_smart_attribute_uses_raw_value(self):
        data = {
            "ata_smart_attributes": {
                "table": [{"name": "Current_Pending_Sector", "raw": {"value": 3, "string": "3"}}]
            }
        }
        self.assertEqual(collector.smart_attribute(data, ["Current_Pending_Sector"]), 3.0)


class StorageAlertTests(unittest.TestCase):
    def monitor(self):
        monitor = Monitor.__new__(Monitor)
        monitor.alert_thresholds = {
            "cpu_warn_percent": 85,
            "cpu_critical_percent": 95,
            "memory_warn_percent": 88,
            "memory_critical_percent": 95,
            "gpu_warn_percent": 92,
            "gpu_critical_percent": 98,
            "disk_warn_percent": 90,
            "disk_critical_percent": 95,
            "inode_warn_percent": 90,
            "inode_critical_percent": 98,
            "mount_latency_warn_ms": 2000,
            "mount_latency_critical_ms": 5000,
        }
        return monitor

    def result(self, storage):
        return {
            "id": "edge-24",
            "name": "edge-24",
            "host": "192.0.2.24",
            "group": "edge",
            "status": "online",
            "metrics": {
                "cpu": {"percent": 1},
                "memory": {"percent": 2},
                "disk": {"percent": 3},
                "gpu": {"devices": []},
                "storage": storage,
            },
        }

    def test_missing_real_nas_mount_is_critical(self):
        result = self.result(
            {
                "mounts": [
                    {
                        "mount": "/nas",
                        "source": "//p8.example/share/Ray",
                        "status": "automount_only",
                        "expected": True,
                    }
                ],
                "devices": [],
            }
        )
        alerts = self.monitor().alerts_for_result(result)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "mount")
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["path"], "/nas")

    def test_inode_and_smart_warnings(self):
        result = self.result(
            {
                "mounts": [
                    {
                        "mount": "/data",
                        "status": "mounted",
                        "expected": True,
                        "read_only": False,
                        "percent": 40,
                        "inode_percent": 92,
                        "kind": "local",
                    }
                ],
                "devices": [
                    {
                        "name": "sda",
                        "smart": {"health": "warning", "messages": ["pending 2"]},
                    }
                ],
            }
        )
        alerts = self.monitor().alerts_for_result(result)
        self.assertEqual({item["kind"] for item in alerts}, {"inode", "smart"})

    def test_unresponsive_active_optional_mount_warns(self):
        result = self.result(
            {
                "mounts": [
                    {
                        "mount": "/home/alice/nas",
                        "status": "unresponsive",
                        "expected": False,
                        "kind": "network",
                    }
                ],
                "devices": [],
            }
        )
        alerts = self.monitor().alerts_for_result(result)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "warning")

    def test_server_config_can_require_an_unmounted_path(self):
        monitor = self.monitor()
        metrics = {"storage": {"mounts": [], "devices": [], "summary": {}}}
        monitor.apply_storage_expectations(
            {"expected_mounts": [{"mount": "/nas", "source": "//nas.example/share", "fstype": "cifs"}]},
            metrics,
        )
        mount = metrics["storage"]["mounts"][0]
        self.assertEqual(mount["mount"], "/nas")
        self.assertEqual(mount["status"], "missing")
        self.assertTrue(mount["expected"])
        self.assertEqual(metrics["storage"]["summary"]["mount_issue_count"], 1)


if __name__ == "__main__":
    unittest.main()
