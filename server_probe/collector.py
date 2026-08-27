#!/usr/bin/env python3
"""Collect one-shot Linux host metrics and print JSON.

This file is intentionally dependency-free because it is copied over SSH and
executed on the target machine through stdin.
"""

import csv
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

try:
    import pwd
except Exception:
    pwd = None


def run(command, timeout=3):
    try:
        completed = subprocess.run(
            command,
            shell=isinstance(command, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip()
    except Exception:
        return ""


def read_first(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except Exception:
        return default


def numeric(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or "not supported" in text.lower() or text == "[N/A]":
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def bytes_from_kib(kib):
    try:
        return int(kib) * 1024
    except Exception:
        return 0


def duration_text(seconds):
    try:
        seconds = int(float(seconds))
    except Exception:
        return ""
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return "%dd %02dh" % (days, hours)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, seconds)
    return "%ds" % seconds


def cpu_snapshot():
    fields = read_first("/proc/stat").splitlines()[0].split()[1:]
    values = [int(x) for x in fields[:10]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def cpu_percent():
    first_total, first_idle = cpu_snapshot()
    time.sleep(0.35)
    second_total, second_idle = cpu_snapshot()
    total_delta = max(second_total - first_total, 1)
    idle_delta = max(second_idle - first_idle, 0)
    return round((1.0 - idle_delta / float(total_delta)) * 100.0, 1)


def load_average():
    try:
        one, five, fifteen = os.getloadavg()
        return round(one, 2), round(five, 2), round(fifteen, 2)
    except Exception:
        parts = read_first("/proc/loadavg").split()
        if len(parts) >= 3:
            return numeric(parts[0]), numeric(parts[1]), numeric(parts[2])
    return None, None, None


def memory_info():
    values = {}
    for line in read_first("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if parts:
            values[key] = int(parts[0])

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(total - available, 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    swap_used = max(swap_total - swap_free, 0)
    return {
        "total_bytes": bytes_from_kib(total),
        "available_bytes": bytes_from_kib(available),
        "used_bytes": bytes_from_kib(used),
        "percent": round((used / float(total)) * 100.0, 1) if total else None,
        "swap_total_bytes": bytes_from_kib(swap_total),
        "swap_used_bytes": bytes_from_kib(swap_used),
        "swap_percent": round((swap_used / float(swap_total)) * 100.0, 1) if swap_total else 0,
    }


NETWORK_FILESYSTEMS = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "sshfs"}
PSEUDO_FILESYSTEMS = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "rpc_pipefs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}
IGNORED_MOUNT_PREFIXES = (
    "/snap/",
    "/var/snap/",
    "/var/lib/docker/",
    "/var/lib/containers/",
    "/var/lib/kubelet/",
    "/var/lib/calico/",
    "/var/lib/cni/",
    "/var/lib/containerd/",
    "/var/lib/containerd-nydus/",
)


def decode_mount_field(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value or "")


def parse_mountinfo(text):
    mounts = []
    for line in (text or "").splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 2:
            continue
        mount_point = decode_mount_field(left_fields[4])
        mounts.append(
            {
                "major_minor": left_fields[2],
                "root": decode_mount_field(left_fields[3]),
                "mount": mount_point,
                "fstype": right_fields[0],
                "source": decode_mount_field(right_fields[1]),
                "options": sorted(set(left_fields[5].split(","))),
                "super_options": sorted(set(right_fields[2].split(","))) if len(right_fields) > 2 else [],
            }
        )
    return mounts


def parse_fstab(text):
    mounts = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 4 or fields[2] == "swap":
            continue
        mount_point = decode_mount_field(fields[1])
        options = set(fields[3].split(","))
        mounts[mount_point] = {
            "mount": mount_point,
            "source": decode_mount_field(fields[0]),
            "fstype": fields[2],
            "required": "noauto" not in options,
            "read_only_expected": "ro" in options,
            "automount_configured": "x-systemd.automount" in options,
        }
    return mounts


def interesting_mount(mount):
    mount_point = mount.get("mount") or ""
    fstype = mount.get("fstype") or ""
    source = mount.get("source") or ""
    if mount_point == "/":
        return True
    if any(mount_point == prefix.rstrip("/") or mount_point.startswith(prefix) for prefix in IGNORED_MOUNT_PREFIXES):
        return False
    if fstype in PSEUDO_FILESYSTEMS or "/overlay2/" in mount_point or "/kubelet/pods/" in mount_point:
        return False
    if fstype in NETWORK_FILESYSTEMS:
        return True
    if source.startswith("/dev/") and fstype not in PSEUDO_FILESYSTEMS:
        return True
    return mount_point == "/nas" or mount_point.startswith(("/disk_", "/mnt/", "/media/", "/data"))


def filesystem_usage(path, timeout=2.5):
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["stat", "-f", "-c", "%S|%b|%f|%a|%c|%d", "--", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
        latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        if completed.returncode != 0:
            return {"latency_ms": latency_ms, "error": "stat failed"}
        fields = completed.stdout.strip().split("|")
        if len(fields) != 6:
            return {"latency_ms": latency_ms, "error": "invalid stat output"}
        block_size, blocks, free_blocks, available_blocks, inodes, free_inodes = [int(value) for value in fields]
        used_blocks = max(blocks - free_blocks, 0)
        usable_blocks = used_blocks + max(available_blocks, 0)
        used_inodes = max(inodes - free_inodes, 0)
        return {
            "total_bytes": block_size * blocks,
            "used_bytes": block_size * used_blocks,
            "free_bytes": block_size * max(available_blocks, 0),
            "percent": round((used_blocks / float(usable_blocks)) * 100.0, 1) if usable_blocks else None,
            "inode_total": inodes or None,
            "inode_used": used_inodes if inodes else None,
            "inode_free": free_inodes if inodes else None,
            "inode_percent": round((used_inodes / float(inodes)) * 100.0, 1) if inodes else None,
            "latency_ms": latency_ms,
        }
    except subprocess.TimeoutExpired:
        return {"latency_ms": round((time.monotonic() - started) * 1000.0, 1), "error": "timeout"}
    except Exception:
        return {"latency_ms": round((time.monotonic() - started) * 1000.0, 1), "error": "stat failed"}


def parse_cifs_debug(text):
    servers = {}
    sections = re.split(r"(?m)(?=^\d+\) ConnectionId:)", text or "")
    for section in sections:
        host_match = re.search(r"Hostname:\s*(\S+)", section)
        if not host_match:
            continue
        status_match = re.search(r"TCP status:\s*(\d+)", section)
        status = int(status_match.group(1)) if status_match else None
        host = host_match.group(1).lower()
        connected = status == 1 and "DISCONNECTED" not in section
        current = servers.setdefault(host, {"connected": False, "tcp_status": status, "session_count": 0})
        current["session_count"] += 1
        if connected:
            current["connected"] = True
            current["tcp_status"] = 1
        elif not current["connected"]:
            current["tcp_status"] = status
    return servers


def network_source_host(source, fstype):
    text = str(source or "")
    if fstype in ("cifs", "smb3") and text.startswith("//"):
        return text[2:].split("/", 1)[0], 445
    if fstype in ("nfs", "nfs4") and ":" in text:
        return text.split(":", 1)[0].strip("[]"), 2049
    if fstype in ("sshfs", "fuse.sshfs"):
        host = text.split(":", 1)[0].split("@")[-1]
        return host, 22
    return "", None


def network_mount_probe(mount, cifs_servers=None, timeout=3.0):
    fstype = mount.get("fstype") or ""
    host, port = network_source_host(mount.get("source"), fstype)
    if not host or not port:
        return {"connection": "unknown"}
    if fstype in ("cifs", "smb3"):
        debug = (cifs_servers or {}).get(host.lower())
        if debug and not debug.get("connected"):
            return {"connection": "disconnected", "error": "kernel CIFS session is disconnected"}
        if debug and debug.get("connected"):
            return {"connection": "connected"}
    started = time.monotonic()
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
        connection.close()
        return {
            "connection": "reachable",
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    except Exception:
        return {
            "connection": "unreachable",
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "error": "network service is unreachable",
        }


def mount_is_available(mount):
    status = mount.get("status")
    if status == "mounted":
        return True
    return bool(
        status == "automount_only"
        and mount.get("automount")
        and mount.get("connection") in ("connected", "reachable")
    )


def diskstats_snapshot(text=None):
    rows = {}
    for line in (read_first("/proc/diskstats") if text is None else text).splitlines():
        fields = line.split()
        if len(fields) < 14:
            continue
        try:
            rows["%s:%s" % (fields[0], fields[1])] = {
                "name": fields[2],
                "read_bytes_total": int(fields[5]) * 512,
                "write_bytes_total": int(fields[9]) * 512,
                "io_time_ms_total": int(fields[12]),
            }
        except Exception:
            continue
    return rows


def disk_io_rate(before, after, elapsed_seconds, major_minor):
    first = (before or {}).get(major_minor)
    second = (after or {}).get(major_minor)
    if not first or not second or not elapsed_seconds or elapsed_seconds <= 0:
        return None
    read_delta = second["read_bytes_total"] - first["read_bytes_total"]
    write_delta = second["write_bytes_total"] - first["write_bytes_total"]
    busy_delta = second["io_time_ms_total"] - first["io_time_ms_total"]
    if min(read_delta, write_delta, busy_delta) < 0:
        return None
    return {
        "device": second.get("name"),
        "read_bytes_per_second": round(read_delta / float(elapsed_seconds), 1),
        "write_bytes_per_second": round(write_delta / float(elapsed_seconds), 1),
        "busy_percent": round(min(100.0, busy_delta / (elapsed_seconds * 10.0)), 1),
        "read_bytes_total": second["read_bytes_total"],
        "write_bytes_total": second["write_bytes_total"],
    }


def sysfs_block_devices():
    devices = []
    root = "/sys/class/block"
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return devices
    for name in names:
        path = os.path.join(root, name)
        if name.startswith(("loop", "ram", "zram", "dm-")) or name.startswith("sr"):
            continue
        if name.startswith("mmcblk") and ("boot" in name or "rpmb" in name):
            continue
        if os.path.exists(os.path.join(path, "partition")):
            continue
        size = numeric(read_first(os.path.join(path, "size")))
        if not size:
            continue
        model = read_first(os.path.join(path, "device", "model")).replace("\x00", "").strip()
        rotational = read_first(os.path.join(path, "queue", "rotational"))
        syspath = os.path.realpath(path).lower()
        if "nvme" in name or "nvme" in syspath:
            transport = "nvme"
        elif "mmc" in name or "mmc" in syspath:
            transport = "mmc"
        elif "usb" in syspath:
            transport = "usb"
        elif "ata" in syspath:
            transport = "sata"
        elif name.startswith("md"):
            transport = "raid"
        else:
            transport = "block"
        devices.append(
            {
                "name": name,
                "path": "/dev/%s" % name,
                "model": model or name,
                "size_bytes": int(size * 512),
                "transport": transport,
                "rotational": rotational == "1",
                "major_minor": read_first(os.path.join(path, "dev")),
            }
        )
    return devices


def smart_attribute(data, names):
    table = (((data or {}).get("ata_smart_attributes") or {}).get("table") or [])
    wanted = {name.lower() for name in names}
    for item in table:
        if str(item.get("name") or "").lower() not in wanted:
            continue
        raw = item.get("raw") or {}
        value = raw.get("value")
        return numeric(value if value is not None else raw.get("string"))
    return None


def smart_info(device):
    base = {"available": False, "health": "unavailable"}
    if not shutil.which("smartctl") or not os.path.exists(device.get("path") or ""):
        return base
    try:
        completed = subprocess.run(
            ["smartctl", "-n", "standby,3", "-H", "-A", "-j", device["path"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=3,
        )
        data = json.loads(completed.stdout or "{}")
    except Exception:
        return base

    messages = []
    passed = ((data.get("smart_status") or {}).get("passed"))
    temperature = numeric(((data.get("temperature") or {}).get("current")))
    nvme = data.get("nvme_smart_health_information_log") or {}
    critical_warning = numeric(nvme.get("critical_warning")) or 0
    media_errors = numeric(nvme.get("media_errors")) or 0
    percentage_used = numeric(nvme.get("percentage_used"))
    power_on_hours = numeric(((data.get("power_on_time") or {}).get("hours")))
    if power_on_hours is None:
        power_on_hours = smart_attribute(data, ["Power_On_Hours", "Power_On_Hours_and_Msec"])
    reallocated = smart_attribute(data, ["Reallocated_Sector_Ct", "Reallocated_Event_Count"]) or 0
    pending = smart_attribute(data, ["Current_Pending_Sector"]) or 0
    uncorrectable = smart_attribute(data, ["Offline_Uncorrectable", "Reported_Uncorrect"]) or 0

    health = "passed" if passed is not False else "failed"
    if passed is False:
        messages.append("SMART self-assessment failed")
    if critical_warning:
        health = "failed"
        messages.append("NVMe critical warning %s" % int(critical_warning))
    for label, value in (("reallocated", reallocated), ("pending", pending), ("uncorrectable", uncorrectable), ("media errors", media_errors)):
        if value:
            if health != "failed":
                health = "warning"
            messages.append("%s %s" % (label, int(value)))
    if percentage_used is not None and percentage_used >= 90:
        if health != "failed":
            health = "warning"
        messages.append("wear %s%%" % int(percentage_used))
    if temperature is not None and temperature >= 60:
        if health != "failed":
            health = "warning"
        messages.append("temperature %sC" % int(temperature))
    if health == "passed" and passed is None and not nvme and not (data.get("ata_smart_attributes") or {}).get("table"):
        health = "unknown"
    return {
        "available": True,
        "health": health,
        "passed": passed,
        "temperature_c": temperature,
        "power_on_hours": power_on_hours,
        "percentage_used": percentage_used,
        "reallocated_sectors": reallocated,
        "pending_sectors": pending,
        "uncorrectable_errors": uncorrectable,
        "media_errors": media_errors,
        "messages": messages,
    }


def storage_info(disk_before=None, disk_after=None, elapsed_seconds=None):
    active_mounts = parse_mountinfo(read_first("/proc/self/mountinfo"))
    configured = parse_fstab(read_first("/etc/fstab"))
    by_path = {}
    for mount in active_mounts:
        by_path.setdefault(mount["mount"], []).append(mount)

    paths = {"/"}
    for mount in active_mounts:
        if interesting_mount(mount):
            paths.add(mount["mount"])
    for mount_point, item in configured.items():
        candidate = dict(item)
        if item.get("required") and interesting_mount(candidate):
            paths.add(mount_point)

    mounts = []
    for mount_point in sorted(paths, key=lambda value: (value != "/", value)):
        entries = by_path.get(mount_point, [])
        real_entries = [entry for entry in entries if entry.get("fstype") != "autofs"]
        selected = real_entries[-1] if real_entries else (entries[-1] if entries else {})
        expected = configured.get(mount_point) or {}
        has_automount = any(entry.get("fstype") == "autofs" for entry in entries) or expected.get("automount_configured", False)
        if real_entries:
            status = "mounted"
        elif entries:
            status = "automount_only"
        else:
            status = "missing"
        if real_entries:
            fstype = selected.get("fstype") or expected.get("fstype") or ""
            source = selected.get("source") or expected.get("source") or ""
        else:
            fstype = expected.get("fstype") or selected.get("fstype") or ""
            source = expected.get("source") or selected.get("source") or ""
        options = set(selected.get("options") or []) | set(selected.get("super_options") or [])
        mounts.append(
            {
                "mount": mount_point,
                "source": source,
                "fstype": fstype,
                "kind": "network" if fstype in NETWORK_FILESYSTEMS or str(source).startswith("//") else ("automount" if fstype == "autofs" else "local"),
                "status": status,
                "expected": bool(expected.get("required", mount_point == "/")),
                "read_only_expected": bool(expected.get("read_only_expected", False)),
                "automount": has_automount,
                "read_only": "ro" in options,
                "major_minor": selected.get("major_minor"),
            }
        )

    mounted_local = [item for item in mounts if item["status"] == "mounted" and item.get("kind") != "network"]
    if mounted_local:
        workers = min(6, len(mounted_local))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            usages = list(executor.map(lambda item: filesystem_usage(item["mount"], timeout=2.5), mounted_local))
        for item, usage in zip(mounted_local, usages):
            item.update(usage)
            if usage.get("error"):
                item["status"] = "unresponsive"
            io = disk_io_rate(disk_before, disk_after, elapsed_seconds, item.get("major_minor"))
            if io:
                item["io"] = io

    mounted_network = [
        item
        for item in mounts
        if item["status"] in ("mounted", "automount_only") and item.get("kind") == "network"
    ]
    if mounted_network:
        cifs_servers = parse_cifs_debug(read_first("/proc/fs/cifs/DebugData"))
        workers = min(6, len(mounted_network))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            probes = list(executor.map(lambda item: network_mount_probe(item, cifs_servers), mounted_network))
        for item, probe in zip(mounted_network, probes):
            item.update(probe)
            if probe.get("error") and item.get("status") == "mounted":
                item["status"] = "unresponsive"

    devices = sysfs_block_devices()
    if devices:
        workers = min(4, len(devices))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            smart_rows = list(executor.map(smart_info, devices))
        for device, smart in zip(devices, smart_rows):
            device["smart"] = smart
            io = disk_io_rate(disk_before, disk_after, elapsed_seconds, device.get("major_minor"))
            if io:
                device["io"] = io

    mount_issues = sum(
        1
        for item in mounts
        if item.get("status") == "unresponsive" or (item.get("expected") and not mount_is_available(item))
    )
    smart_issues = sum(1 for item in devices if (item.get("smart") or {}).get("health") in ("warning", "failed"))
    return {
        "mounts": mounts,
        "devices": devices,
        "smartctl_available": bool(shutil.which("smartctl")),
        "summary": {
            "mount_count": len(mounts),
            "mounted_count": sum(1 for item in mounts if item.get("status") == "mounted"),
            "mount_issue_count": mount_issues,
            "network_mount_count": sum(1 for item in mounts if item.get("kind") == "network"),
            "device_count": len(devices),
            "smart_issue_count": smart_issues,
        },
    }


def disk_info(storage=None):
    if storage:
        for item in storage.get("mounts") or []:
            if item.get("mount") == "/" and item.get("status") == "mounted":
                return {
                    "mount": "/",
                    "total_bytes": item.get("total_bytes"),
                    "used_bytes": item.get("used_bytes"),
                    "free_bytes": item.get("free_bytes"),
                    "percent": item.get("percent"),
                }
    try:
        stat = os.statvfs("/")
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = max(total - free, 0)
        return {
            "mount": "/",
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "percent": round((used / float(total)) * 100.0, 1) if total else None,
        }
    except Exception:
        return {}


def os_release():
    data = {}
    for line in read_first("/etc/os-release").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data.get("PRETTY_NAME") or data.get("NAME") or platform.platform()


def process_rows(sort_key="-pcpu", limit=10):
    command = "ps -eo pid,user:32,etimes,pcpu,pmem,rss,stat,comm,args --sort=%s --no-headers" % sort_key
    output = run(command, timeout=5)
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 8)
        if len(parts) < 8:
            continue
        if len(parts) == 8:
            parts.append(parts[7])
        pid, user, etimes, pcpu, pmem, rss, stat, comm, args = parts
        rows.append(
            {
                "pid": int(pid) if pid.isdigit() else pid,
                "user": username_for_pid(pid) or user,
                "runtime_seconds": int(etimes) if etimes.isdigit() else None,
                "runtime": duration_text(etimes),
                "cpu_percent": numeric(pcpu),
                "mem_percent": numeric(pmem),
                "rss_bytes": bytes_from_kib(rss),
                "state": stat,
                "command": args or comm,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def username_for_pid(pid):
    try:
        status = read_first("/proc/%s/status" % pid)
        for line in status.splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                if pwd is None:
                    return str(uid)
                return pwd.getpwuid(uid).pw_name
    except Exception:
        return ""
    return ""


def cmdline_for_pid(pid):
    raw = read_first("/proc/%s/cmdline" % pid)
    if raw:
        return raw.replace("\x00", " ").strip()
    return read_first("/proc/%s/comm" % pid)


def process_details_for_pid(pid):
    try:
        pid = int(pid)
    except Exception:
        return {}

    command = "ps -p %s -o user:32=,etimes=,pcpu=,pmem=,rss=,stat=,comm=,args=" % pid
    output = run(command, timeout=2)
    if not output:
        return {
            "pid": pid,
            "user": username_for_pid(pid),
            "command": cmdline_for_pid(pid),
        }

    line = output.splitlines()[0].strip()
    parts = line.split(None, 7)
    if len(parts) < 7:
        return {
            "pid": pid,
            "user": username_for_pid(pid),
            "command": cmdline_for_pid(pid),
        }
    if len(parts) == 7:
        parts.append(parts[6])
    user, etimes, pcpu, pmem, rss, stat, comm, args = parts
    return {
        "pid": pid,
        "user": username_for_pid(pid) or user,
        "runtime_seconds": int(etimes) if etimes.isdigit() else None,
        "runtime": duration_text(etimes),
        "cpu_percent": numeric(pcpu),
        "mem_percent": numeric(pmem),
        "rss_bytes": bytes_from_kib(rss),
        "state": stat,
        "command": args or cmdline_for_pid(pid) or comm,
    }


def run_result(command, timeout=5):
    try:
        environment = dict(os.environ)
        environment["LC_ALL"] = "C"
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            env=environment,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "stdout": "", "stderr": "timeout"}
    except Exception as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


def json_lines(text):
    rows = []
    for line in (text or "").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except Exception:
            continue
    return rows


def login_uid_range():
    minimum, maximum = 1000, 60000
    for line in read_first("/etc/login.defs").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("UID_MIN", "UID_MAX"):
            continue
        try:
            value = int(parts[1])
        except Exception:
            continue
        if parts[0] == "UID_MIN":
            minimum = value
        else:
            maximum = value
    return minimum, maximum


def user_resource_summary():
    command = "ps -eo uid=,user:32=,pid=,pcpu=,pmem=,rss=,etimes=,stat= --no-headers"
    output = run(command, timeout=5)
    uid_min, uid_max = login_uid_range()
    users = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 7)
        if len(parts) < 8:
            continue
        uid_text, user, pid, pcpu, pmem, rss, etimes, state = parts
        if not uid_text.isdigit():
            continue
        uid = int(uid_text)
        if uid < uid_min or uid > uid_max or user == "root":
            continue
        if pwd is not None:
            try:
                user = pwd.getpwuid(uid).pw_name
            except Exception:
                continue
        entry = users.setdefault(
            user,
            {
                "user": user,
                "uid": uid,
                "process_count": 0,
                "running_process_count": 0,
                "cpu_percent_sum": 0,
                "mem_percent_sum": 0,
                "rss_bytes": 0,
                "longest_runtime_seconds": 0,
            },
        )
        entry["process_count"] += 1
        if str(state).startswith("R"):
            entry["running_process_count"] += 1
        cpu = numeric(pcpu)
        memory = numeric(pmem)
        if cpu is not None:
            entry["cpu_percent_sum"] += cpu
        if memory is not None:
            entry["mem_percent_sum"] += memory
        entry["rss_bytes"] += bytes_from_kib(rss)
        runtime = int(etimes) if str(etimes).isdigit() else 0
        entry["longest_runtime_seconds"] = max(entry["longest_runtime_seconds"], runtime)

    rows = []
    for entry in users.values():
        entry["cpu_percent_sum"] = round(entry["cpu_percent_sum"], 1)
        entry["mem_percent_sum"] = round(entry["mem_percent_sum"], 1)
        rows.append(entry)
    rows.sort(key=lambda item: (item.get("rss_bytes") or 0, item.get("cpu_percent_sum") or 0), reverse=True)
    return rows[:200]


def size_bytes(value):
    text = str(value or "").strip().split("(", 1)[0].strip().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)?", text, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    binary = "i" in unit
    prefix = unit[0] if len(unit) > 1 else ""
    powers = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}
    return int(number * ((1024 if binary else 1000) ** powers.get(prefix, 0)))


def io_pair(value):
    parts = [item.strip() for item in str(value or "").split("/", 1)]
    if len(parts) != 2:
        return None, None
    return size_bytes(parts[0]), size_bytes(parts[1])


def percent_value(value):
    return numeric(str(value or "").replace("%", ""))


def docker_inspect(ids):
    if not ids:
        return {}
    template = (
        '{"id":{{json .Id}},"image_id":{{json .Image}},"state":{{json .State}},'
        '"restart_count":{{json .RestartCount}},"network_mode":{{json .HostConfig.NetworkMode}},'
        '"ports":{{json .NetworkSettings.Ports}},"networks":{{json .NetworkSettings.Networks}},'
        '"entrypoint":{{json .Config.Entrypoint}},"cmd":{{json .Config.Cmd}},'
        '"config_user":{{json .Config.User}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}'
    )
    result = run_result(["docker", "inspect", "--format", template] + ids[:200], timeout=8)
    return {item.get("id"): item for item in json_lines(result.get("stdout")) if item.get("id")}


def docker_image_metadata(image_ids):
    if not image_ids:
        return {}
    template = (
        '{"id":{{json .Id}},"repo_tags":{{json .RepoTags}},"size":{{json .Size}},'
        '"created":{{json .Created}},"architecture":{{json .Architecture}},'
        '"env":{{json .Config.Env}},"labels":{{json .Config.Labels}}}'
    )
    result = run_result(["docker", "image", "inspect", "--format", template] + list(image_ids)[:100], timeout=8)
    metadata = {}
    for item in json_lines(result.get("stdout")):
        image_id = item.get("id")
        if not image_id:
            continue
        labels = item.get("labels") or {}
        environment = {}
        for entry in item.get("env") or []:
            if "=" in entry:
                key, value = entry.split("=", 1)
                if key in ("VLLM_VERSION", "NVIDIA_VLLM_VERSION", "CUDA_VERSION"):
                    environment[key] = value
        version = labels.get("com.nvidia.vllm.version") or environment.get("VLLM_VERSION")
        if not version:
            for tag in item.get("repo_tags") or []:
                tag_value = tag.rsplit(":", 1)[-1]
                if re.match(r"^v?\d+\.\d+", tag_value):
                    version = tag_value
                    break
        metadata[image_id] = {
            "version": version,
            "nvidia_release": environment.get("NVIDIA_VLLM_VERSION"),
            "cuda_version": environment.get("CUDA_VERSION"),
            "architecture": item.get("architecture"),
            "created_at": item.get("created"),
            "size_bytes": item.get("size"),
        }
    return metadata


def command_tokens(command):
    text = str(command or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    try:
        return shlex.split(text)
    except Exception:
        return text.split()


def command_option(tokens, name, multiple=False):
    values = []
    for index, token in enumerate(tokens):
        if token.startswith(name + "="):
            values.append(token.split("=", 1)[1])
            continue
        if token != name:
            continue
        cursor = index + 1
        while cursor < len(tokens) and not tokens[cursor].startswith("--"):
            values.append(tokens[cursor])
            cursor += 1
            if not multiple:
                break
    if multiple:
        return values
    return values[0] if values else None


def vllm_details(image, command):
    image_lower = str(image or "").lower()
    command_lower = str(command or "").lower()
    if "vllm" not in image_lower and "vllm" not in command_lower:
        return None
    tokens = command_tokens(command)
    model_path = command_option(tokens, "--model")
    if not model_path:
        for index, token in enumerate(tokens[:-1]):
            if token == "serve" and not tokens[index + 1].startswith("-"):
                model_path = tokens[index + 1]
                break
    served_names = command_option(tokens, "--served-model-name", multiple=True)
    is_service = bool(
        model_path
        or served_names
        or "vllm serve" in command_lower
        or "vllm.entrypoints" in command_lower
        or any(token == "serve" for token in tokens)
    )
    if not is_service:
        return {"service": False}
    model_name = served_names[0] if served_names else None
    if not model_name and model_path:
        model_name = model_path.rstrip("/").rsplit("/", 1)[-1]
    details = {
        "service": True,
        "model": model_name or model_path,
        "served_model_names": served_names,
        "port": int(numeric(command_option(tokens, "--port")) or 8000),
    }
    safe_options = {
        "tensor_parallel_size": "--tensor-parallel-size",
        "pipeline_parallel_size": "--pipeline-parallel-size",
        "max_model_len": "--max-model-len",
        "max_num_seqs": "--max-num-seqs",
        "gpu_memory_utilization": "--gpu-memory-utilization",
        "dtype": "--dtype",
        "quantization": "--quantization",
        "task": "--task",
    }
    for key, option in safe_options.items():
        value = command_option(tokens, option)
        if value is not None:
            details[key] = value
    return details


def valid_owner_name(value):
    text = str(value or "").strip()
    return text if re.match(r"^[a-z_][a-z0-9_-]{0,31}$", text) else None


def existing_owner_name(value):
    owner = valid_owner_name(value)
    if not owner or pwd is None:
        return owner
    try:
        pwd.getpwnam(owner)
        return owner
    except Exception:
        return None


def owner_from_home_path(value):
    path = str(value or "")
    if path == "/root" or path.startswith("/root/"):
        return existing_owner_name("root")
    match = re.match(r"^/home/([^/]+)(?:/|$)", path)
    return existing_owner_name(match.group(1)) if match else None


def infer_container_owner(inspect):
    labels = {str(key).lower(): value for key, value in (inspect.get("labels") or {}).items()}
    for key in ("server-probe.owner", "com.server-probe.owner", "owner"):
        owner = valid_owner_name(labels.get(key))
        if owner:
            return {"owner_user": owner, "owner_source": "label", "owner_confidence": "exact"}

    working_dir = labels.get("com.docker.compose.project.working_dir")
    owner = owner_from_home_path(working_dir)
    if owner:
        return {"owner_user": owner, "owner_source": "compose", "owner_confidence": "inferred"}

    owners = []
    for mount in inspect.get("mounts") or []:
        owner = owner_from_home_path((mount or {}).get("Source"))
        if owner and owner not in owners:
            owners.append(owner)
    if len(owners) == 1:
        return {"owner_user": owners[0], "owner_source": "home_mount", "owner_confidence": "inferred"}

    runtime_user = str(inspect.get("config_user") or "root").strip() or "root"
    return {
        "owner_user": None,
        "owner_source": "unknown",
        "owner_confidence": "unknown",
        "runtime_user": runtime_user,
    }


def container_ports(inspect, fallback=""):
    rows = []
    for container_port, bindings in (inspect.get("ports") or {}).items():
        if not bindings:
            rows.append(container_port)
            continue
        for binding in bindings:
            host = binding.get("HostIp") or "0.0.0.0"
            port = binding.get("HostPort")
            if port:
                rows.append("%s:%s->%s" % (host, port, container_port))
    if not rows and fallback:
        rows = [item.strip() for item in str(fallback).split(",") if item.strip()]
    return rows[:20]


def safe_probe_host(value):
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or address.is_private or address.is_link_local
    except Exception:
        return value in ("localhost",)


def endpoint_candidates(container):
    details = container.get("vllm") or {}
    port = int(details.get("port") or 8000)
    inspect = container.get("_inspect") or {}
    candidates = []
    bindings = (inspect.get("ports") or {}).get("%s/tcp" % port) or []
    for binding in bindings:
        host_port = numeric(binding.get("HostPort"))
        if host_port:
            candidates.append(("127.0.0.1", int(host_port)))
    if inspect.get("network_mode") == "host":
        candidates.append(("127.0.0.1", port))
    for network in (inspect.get("networks") or {}).values():
        address = str((network or {}).get("IPAddress") or "")
        if address and safe_probe_host(address):
            candidates.append((address, port))
    unique = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique[:4]


def local_http_request(url, timeout=2.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "server-probe-dashboard"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(1024 * 1024)
            return response.status, payload, round((time.monotonic() - started) * 1000.0, 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1024 * 1024), round((time.monotonic() - started) * 1000.0, 1)
    except Exception:
        return None, b"", round((time.monotonic() - started) * 1000.0, 1)


def probe_vllm(container):
    candidates = endpoint_candidates(container)
    if not candidates:
        return {"status": "not_exposed", "models": []}
    last_latency = None
    for host, port in candidates:
        base = "http://%s:%s" % (host, port)
        health_code, _, health_latency = local_http_request(base + "/health")
        last_latency = health_latency
        models_code, payload, models_latency = local_http_request(base + "/v1/models")
        last_latency = models_latency if models_code is not None else health_latency
        reachable = health_code is not None or models_code is not None
        healthy = (health_code is not None and 200 <= health_code < 300) or (
            models_code is not None and (200 <= models_code < 300 or models_code in (401, 403))
        )
        if not reachable:
            continue
        models = []
        if models_code is not None and 200 <= models_code < 300:
            try:
                data = json.loads(payload.decode("utf-8", "replace"))
                models = [str(item.get("id")) for item in data.get("data", []) if item.get("id")][:20]
            except Exception:
                pass
        return {
            "status": "healthy" if healthy else "unhealthy",
            "endpoint": "%s:%s" % (host, port),
            "latency_ms": last_latency,
            "health_code": health_code,
            "models_code": models_code,
            "models": models,
        }
    return {"status": "unhealthy", "latency_ms": last_latency, "models": []}


def container_id_for_pid(pid, container_ids):
    cgroup = read_first("/proc/%s/cgroup" % pid)
    candidates = re.findall(r"[0-9a-f]{12,64}", cgroup.lower())
    for value in candidates:
        for container_id in container_ids:
            if container_id.startswith(value) or value.startswith(container_id):
                return container_id
    return None


def attach_gpu_containers(gpu, containers):
    by_id = {item.get("_full_id"): item for item in containers if item.get("_full_id")}
    for process in (gpu or {}).get("_all_processes") or (gpu or {}).get("processes") or []:
        container_id = container_id_for_pid(process.get("pid"), by_id.keys())
        if container_id:
            container = by_id[container_id]
            process["container_id"] = container_id[:12]
            process["container_name"] = container.get("name")
            process["container_image"] = container.get("image")
            process["owner_user"] = container.get("owner_user")
            process["owner_confidence"] = container.get("owner_confidence")
            process["model"] = (container.get("vllm") or {}).get("model")
            container.setdefault("gpu_indices", [])
            gpu_index = process.get("gpu_index")
            if gpu_index not in (None, "") and str(gpu_index) not in container["gpu_indices"]:
                container["gpu_indices"].append(str(gpu_index))
            container["gpu_process_count"] = int(container.get("gpu_process_count") or 0) + 1
            container["gpu_memory_used_bytes"] = int(container.get("gpu_memory_used_bytes") or 0) + int(
                process.get("used_memory_bytes") or 0
            )
        process["attributed_user"] = process.get("owner_user") or process.get("user") or "unknown"


def docker_info():
    if not shutil.which("docker"):
        return {"available": False, "accessible": False, "reason": "not_installed", "containers": [], "images": []}

    commands = {
        "containers": ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
        "stats": ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        "images": ["docker", "image", "ls", "-a", "--no-trunc", "--format", "{{json .}}"],
        "disk": ["docker", "system", "df", "--format", "{{json .}}"],
        "version": ["docker", "version", "--format", "{{.Server.Version}}"],
    }
    timeouts = {"containers": 6, "stats": 6, "images": 6, "disk": 6, "version": 4}
    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        futures = {key: executor.submit(run_result, command, timeouts[key]) for key, command in commands.items()}
        results = {key: future.result() for key, future in futures.items()}

    container_result = results["containers"]
    if container_result.get("returncode") != 0:
        error = (container_result.get("stderr") or "").lower()
        reason = "permission_denied" if "permission denied" in error else "daemon_unavailable"
        return {"available": True, "accessible": False, "reason": reason, "containers": [], "images": []}

    ps_rows = json_lines(container_result.get("stdout"))
    ids = [row.get("ID") for row in ps_rows if row.get("ID")]
    inspect_rows = docker_inspect(ids)
    inspect_by_prefix = {key[:12]: value for key, value in inspect_rows.items()}
    stats_rows = json_lines(results["stats"].get("stdout"))
    stats_by_name = {row.get("Name"): row for row in stats_rows if row.get("Name")}
    stats_by_id = {str(row.get("ID") or row.get("Container") or "")[:12]: row for row in stats_rows}

    containers = []
    vllm_image_ids = set()
    for row in ps_rows:
        full_id = str(row.get("ID") or "")
        inspect = inspect_rows.get(full_id) or inspect_by_prefix.get(full_id[:12]) or {}
        state = inspect.get("state") or {}
        stats = stats_by_name.get(row.get("Names")) or stats_by_id.get(full_id[:12]) or {}
        memory_used, memory_limit = io_pair(stats.get("MemUsage"))
        network_rx, network_tx = io_pair(stats.get("NetIO"))
        block_read, block_write = io_pair(stats.get("BlockIO"))
        image = row.get("Image") or ""
        inspect_command = " ".join([str(value) for value in (inspect.get("entrypoint") or []) + (inspect.get("cmd") or [])])
        command = inspect_command or row.get("Command") or ""
        vllm = vllm_details(image, command)
        image_id = inspect.get("image_id")
        if vllm and image_id:
            vllm_image_ids.add(image_id)
        health = ((state.get("Health") or {}).get("Status"))
        current_state = state.get("Status") or row.get("State") or "unknown"
        container = {
            "id": full_id[:12],
            "_full_id": full_id,
            "name": row.get("Names") or full_id[:12],
            "image": image,
            "image_id": str(image_id or "")[:19],
            "state": current_state,
            "status": row.get("Status"),
            "health": health,
            "running": current_state == "running",
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "restart_count": int(inspect.get("restart_count") or 0),
            "cpu_percent": percent_value(stats.get("CPUPerc")),
            "memory_used_bytes": memory_used,
            "memory_limit_bytes": memory_limit,
            "memory_percent": percent_value(stats.get("MemPerc")),
            "network_rx_bytes": network_rx,
            "network_tx_bytes": network_tx,
            "block_read_bytes": block_read,
            "block_write_bytes": block_write,
            "pids": int(numeric(stats.get("PIDs")) or 0),
            "ports": container_ports(inspect, row.get("Ports")),
            "runtime_user": str(inspect.get("config_user") or "root").strip() or "root",
            "_inspect": inspect,
        }
        container.update(infer_container_owner(inspect))
        if vllm:
            container["vllm"] = vllm
        containers.append(container)

    image_metadata = docker_image_metadata(vllm_image_ids)
    vllm_image_containers = [item for item in containers if item.get("vllm")]
    for container in vllm_image_containers:
        metadata = image_metadata.get((container.get("_inspect") or {}).get("image_id")) or {}
        container["vllm"].update({key: value for key, value in metadata.items() if value not in (None, "")})
    vllm_containers = [item for item in vllm_image_containers if (item.get("vllm") or {}).get("service")]
    running_vllm = [item for item in vllm_containers if item.get("running")]
    if running_vllm:
        with ThreadPoolExecutor(max_workers=min(6, len(running_vllm))) as executor:
            probes = list(executor.map(probe_vllm, running_vllm))
        for container, probe in zip(running_vllm, probes):
            container["vllm"]["probe"] = probe

    images = []
    for row in json_lines(results["images"].get("stdout")):
        repository = row.get("Repository") or "<none>"
        tag = row.get("Tag") or "<none>"
        image_id = str(row.get("ID") or "")
        images.append(
            {
                "repository": repository,
                "tag": tag,
                "id": image_id[:19],
                "size_bytes": size_bytes(row.get("Size")),
                "created_at": row.get("CreatedAt"),
                "vllm": "vllm" in (repository + ":" + tag).lower(),
            }
        )
    images.sort(key=lambda item: item.get("size_bytes") or 0, reverse=True)

    disk_usage = {}
    for row in json_lines(results["disk"].get("stdout")):
        kind = str(row.get("Type") or "").lower()
        if not kind:
            continue
        disk_usage[kind] = {
            "total": int(numeric(row.get("TotalCount")) or 0),
            "active": int(numeric(row.get("Active")) or 0),
            "size_bytes": size_bytes(row.get("Size")),
            "reclaimable_bytes": size_bytes(row.get("Reclaimable")),
        }

    for container in containers:
        container.pop("_inspect", None)
    summary = {
        "container_count": len(containers),
        "running_count": sum(1 for item in containers if item.get("running")),
        "stopped_count": sum(1 for item in containers if not item.get("running")),
        "unhealthy_count": sum(1 for item in containers if item.get("running") and item.get("health") == "unhealthy"),
        "restarting_count": sum(1 for item in containers if item.get("state") == "restarting"),
        "vllm_count": len(vllm_containers),
        "vllm_image_container_count": len(vllm_image_containers),
        "vllm_running_count": sum(1 for item in vllm_containers if item.get("running")),
        "vllm_unhealthy_count": sum(
            1
            for item in vllm_containers
            if item.get("running") and ((item.get("vllm") or {}).get("probe") or {}).get("status") == "unhealthy"
        ),
        "image_count": len(images),
    }
    return {
        "available": True,
        "accessible": True,
        "version": results["version"].get("stdout", "").strip() or None,
        "summary": summary,
        "containers": containers[:200],
        "images": images[:100],
        "disk_usage": disk_usage,
    }


def memory_mib_to_bytes(value):
    amount = numeric(value)
    if amount is None:
        return None
    text = str(value).lower()
    if "gib" in text or "gb" in text:
        return int(amount * 1024 * 1024 * 1024)
    if "kib" in text or "kb" in text:
        return int(amount * 1024)
    return int(amount * 1024 * 1024)


def merge_gpu_process(processes, candidate):
    pid = candidate.get("pid")
    if not pid:
        return
    key = (candidate.get("gpu_uuid") or candidate.get("gpu_index") or "unknown", pid)
    current = processes.get(key, {})
    merged = {**current, **{k: v for k, v in candidate.items() if v not in (None, "")}}

    for field in ("used_memory_bytes", "gpu_sm_percent", "gpu_mem_percent", "cpu_percent", "mem_percent", "rss_bytes"):
        values = [current.get(field), candidate.get(field)]
        numbers = [value for value in values if isinstance(value, (int, float))]
        if numbers:
            merged[field] = max(numbers)

    details = process_details_for_pid(pid)
    for key_name, value in details.items():
        if merged.get(key_name) in (None, "") and value not in (None, ""):
            merged[key_name] = value
    if not merged.get("process_name"):
        merged["process_name"] = details.get("command", "").split(" ", 1)[0]
    processes[key] = merged


def safe_process_name(value):
    tokens = command_tokens(value)
    text = tokens[0] if tokens else str(value or "").strip()
    return text.replace("\\", "/").rsplit("/", 1)[-1][:120] or "unknown"


def safe_process_label(process):
    executable = safe_process_name(process.get("process_name") or process.get("command"))
    if not executable.lower().startswith("python"):
        return executable
    tokens = command_tokens(process.get("command"))
    for index, token in enumerate(tokens[:-1]):
        if token != "-m":
            continue
        module = "".join(char for char in tokens[index + 1] if char.isalnum() or char in "._+-")
        if module:
            return "%s · %s" % (executable, module[:80])
    for token in tokens[1:]:
        if not token.lower().endswith(".py"):
            continue
        script = token.replace("\\", "/").rsplit("/", 1)[-1]
        script = "".join(char for char in script if char.isalnum() or char in "._+-")
        if script:
            return "%s · %s" % (executable, script[:80])
    return executable


def gpu_user_summary(processes, prefer_attributed=False):
    users = {}
    for process in processes:
        user = process.get("attributed_user") if prefer_attributed else process.get("user")
        user = user or "unknown"
        entry = users.setdefault(
            user,
            {
                "user": user,
                "process_count": 0,
                "gpu_count": 0,
                "gpu_indices": [],
                "used_memory_bytes": 0,
                "gpu_sm_percent_sum": 0,
                "gpu_sm_percent_max": None,
                "gpu_mem_percent_sum": 0,
                "gpu_mem_percent_max": None,
                "cpu_percent_sum": 0,
                "mem_percent_sum": 0,
                "_pids": set(),
                "_gpus": set(),
                "_cpu_pids": set(),
                "_processes": {},
            },
        )

        pid = process.get("pid")
        if pid not in (None, ""):
            entry["_pids"].add(pid)
        gpu_index = process.get("gpu_index")
        if gpu_index not in (None, ""):
            entry["_gpus"].add(str(gpu_index))

        used_memory = process.get("used_memory_bytes")
        if isinstance(used_memory, (int, float)):
            entry["used_memory_bytes"] += int(used_memory)

        if pid not in (None, ""):
            process_entry = entry["_processes"].setdefault(
                str(pid),
                {
                    "pid": pid,
                    "process_name": safe_process_label(process),
                    "used_memory_bytes": 0,
                    "gpu_indices": set(),
                    "gpu_sm_percent_sum": 0.0,
                    "container_name": process.get("container_name"),
                    "container_image": process.get("container_image"),
                    "model": process.get("model"),
                    "runtime_seconds": process.get("runtime_seconds"),
                    "owner_confidence": process.get("owner_confidence"),
                },
            )
            if isinstance(used_memory, (int, float)):
                process_entry["used_memory_bytes"] += int(used_memory)
            if gpu_index not in (None, ""):
                process_entry["gpu_indices"].add(str(gpu_index))
            process_sm = process.get("gpu_sm_percent")
            if isinstance(process_sm, (int, float)):
                process_entry["gpu_sm_percent_sum"] += process_sm
            for field in ("container_name", "container_image", "model", "runtime_seconds", "owner_confidence"):
                if process_entry.get(field) in (None, "") and process.get(field) not in (None, ""):
                    process_entry[field] = process.get(field)

        sm = process.get("gpu_sm_percent")
        if isinstance(sm, (int, float)):
            entry["gpu_sm_percent_sum"] += sm
            entry["gpu_sm_percent_max"] = sm if entry["gpu_sm_percent_max"] is None else max(entry["gpu_sm_percent_max"], sm)

        gpu_mem = process.get("gpu_mem_percent")
        if isinstance(gpu_mem, (int, float)):
            entry["gpu_mem_percent_sum"] += gpu_mem
            entry["gpu_mem_percent_max"] = (
                gpu_mem if entry["gpu_mem_percent_max"] is None else max(entry["gpu_mem_percent_max"], gpu_mem)
            )

        if pid not in entry["_cpu_pids"]:
            cpu = process.get("cpu_percent")
            mem = process.get("mem_percent")
            if isinstance(cpu, (int, float)):
                entry["cpu_percent_sum"] += cpu
            if isinstance(mem, (int, float)):
                entry["mem_percent_sum"] += mem
            if pid not in (None, ""):
                entry["_cpu_pids"].add(pid)

    rows = []
    for entry in users.values():
        entry["process_count"] = len(entry["_pids"])
        entry["gpu_indices"] = sorted(entry["_gpus"], key=lambda value: int(value) if value.isdigit() else value)
        entry["gpu_count"] = len(entry["gpu_indices"])
        entry["gpu_sm_percent_sum"] = round(entry["gpu_sm_percent_sum"], 1)
        entry["gpu_mem_percent_sum"] = round(entry["gpu_mem_percent_sum"], 1)
        entry["cpu_percent_sum"] = round(entry["cpu_percent_sum"], 1)
        entry["mem_percent_sum"] = round(entry["mem_percent_sum"], 1)
        top_processes = []
        for process_entry in entry["_processes"].values():
            process_entry["gpu_indices"] = sorted(
                process_entry["gpu_indices"], key=lambda value: int(value) if value.isdigit() else value
            )
            process_entry["gpu_sm_percent_sum"] = round(process_entry["gpu_sm_percent_sum"], 1)
            top_processes.append(process_entry)
        top_processes.sort(
            key=lambda item: (item.get("used_memory_bytes") or 0, item.get("gpu_sm_percent_sum") or 0),
            reverse=True,
        )
        entry["top_processes"] = top_processes[:5]
        entry["top_process"] = top_processes[0] if top_processes else None
        for internal in ("_pids", "_gpus", "_cpu_pids", "_processes"):
            entry.pop(internal, None)
        rows.append(entry)

    rows.sort(
        key=lambda item: (
            item.get("used_memory_bytes") or 0,
            item.get("gpu_sm_percent_sum") or 0,
            item.get("process_count") or 0,
        ),
        reverse=True,
    )
    return rows[:100]


def nvidia_gpu_info():
    if not shutil.which("nvidia-smi"):
        return None

    query = (
        "nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,utilization.memory,"
        "memory.total,memory.used,temperature.gpu,power.draw,power.limit "
        "--format=csv,noheader,nounits"
    )
    output = run(query, timeout=5)
    if not output:
        return None

    devices = []
    for row in csv.reader(output.splitlines()):
        row = [item.strip() for item in row]
        if len(row) < 10:
            continue
        total = numeric(row[5])
        used = numeric(row[6])
        devices.append(
            {
                "index": row[0],
                "uuid": row[1],
                "name": row[2],
                "utilization_percent": numeric(row[3]),
                "memory_utilization_percent": numeric(row[4]),
                "memory_total_bytes": int(total * 1024 * 1024) if total is not None else None,
                "memory_used_bytes": int(used * 1024 * 1024) if used is not None else None,
                "memory_percent": round((used / total) * 100.0, 1) if total else None,
                "temperature_c": numeric(row[7]),
                "power_w": numeric(row[8]),
                "power_limit_w": numeric(row[9]),
            }
        )
    uuid_to_device = {device.get("uuid"): device for device in devices if device.get("uuid")}
    index_to_device = {str(device.get("index")): device for device in devices}

    process_output = run(
        "nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory "
        "--format=csv,noheader,nounits",
        timeout=5,
    )
    processes_by_key = {}
    for row in csv.reader(process_output.splitlines()):
        row = [item.strip() for item in row]
        if len(row) < 4 or not row[1].isdigit():
            continue
        device = uuid_to_device.get(row[0], {})
        memory = memory_mib_to_bytes(row[3])
        pid = int(row[1])
        merge_gpu_process(
            processes_by_key,
            {
                "gpu_uuid": row[0],
                "gpu_index": device.get("index"),
                "gpu_name": device.get("name"),
                "pid": pid,
                "process_name": row[2],
                "used_memory_bytes": memory,
                "source": "compute-apps",
            },
        )

    xml_output = run("nvidia-smi -q -x", timeout=6)
    if xml_output:
        try:
            root = ET.fromstring(xml_output)
            for gpu in root.findall(".//gpu"):
                gpu_uuid = (gpu.findtext("uuid") or "").strip()
                device = uuid_to_device.get(gpu_uuid, {})
                for proc in gpu.findall(".//process_info"):
                    pid_text = (proc.findtext("pid") or "").strip()
                    if not pid_text.isdigit():
                        continue
                    merge_gpu_process(
                        processes_by_key,
                        {
                            "gpu_uuid": gpu_uuid,
                            "gpu_index": device.get("index"),
                            "gpu_name": device.get("name"),
                            "pid": int(pid_text),
                            "process_name": (proc.findtext("process_name") or "").strip(),
                            "used_memory_bytes": memory_mib_to_bytes(proc.findtext("used_memory")),
                            "source": "xml",
                        },
                    )
        except Exception:
            pass

    pmon_output = run("nvidia-smi pmon -c 1 -s um", timeout=5)
    for line in pmon_output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 8 or not parts[1].isdigit():
            continue
        gpu_index, pid, process_type = parts[0], parts[1], parts[2]
        sm = parts[3] if len(parts) > 3 else None
        mem = parts[4] if len(parts) > 4 else None
        fb = parts[9] if len(parts) > 9 else None
        command = " ".join(parts[11:]) if len(parts) > 11 else parts[-1]
        device = index_to_device.get(str(gpu_index), {})
        merge_gpu_process(
            processes_by_key,
            {
                "gpu_uuid": device.get("uuid"),
                "gpu_index": str(gpu_index),
                "gpu_name": device.get("name"),
                "pid": int(pid),
                "process_type": process_type,
                "process_name": command,
                "gpu_sm_percent": numeric(sm),
                "gpu_mem_percent": numeric(mem),
                "used_memory_bytes": memory_mib_to_bytes(fb),
                "source": "pmon",
            },
        )

    processes = list(processes_by_key.values())
    user_summary = gpu_user_summary(processes)
    processes.sort(
        key=lambda item: (
            item.get("used_memory_bytes") or 0,
            item.get("gpu_sm_percent") or 0,
            item.get("gpu_mem_percent") or 0,
        ),
        reverse=True,
    )
    return {
        "available": bool(devices),
        "kind": "nvidia",
        "devices": devices,
        "processes": processes[:10],
        "_all_processes": processes,
        "user_summary": user_summary,
    }


def tegrastats_sample():
    if not shutil.which("tegrastats"):
        return ""

    commands = [
        ["tegrastats", "--interval", "100", "--count", "1"],
        ["tegrastats", "--interval", "100"],
    ]
    for command in commands:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            try:
                output, error = process.communicate(timeout=1.4)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    output, error = process.communicate(timeout=0.8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, error = process.communicate(timeout=0.8)
            text = (output or "") + "\n" + (error or "")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for line in lines:
                if "RAM " in line and ("GR3D_FREQ" in line or "GPU" in line or "gpu" in line):
                    return line
        except Exception:
            continue
    return ""


def jetson_device_name():
    model = read_first("/proc/device-tree/model").replace("\x00", "").strip()
    return model or "Jetson integrated GPU"


def jetson_gpu_info():
    output = tegrastats_sample()
    if not output:
        return None

    gpu_match = re.search(r"GR3D_FREQ\s+(\d+(?:\.\d+)?)%", output)
    ram_match = re.search(r"RAM\s+(\d+)\/(\d+)MB", output)
    temp_match = re.search(r"(?:GPU|gpu)@(\d+(?:\.\d+)?)C", output)
    used_mb = numeric(ram_match.group(1)) if ram_match else None
    total_mb = numeric(ram_match.group(2)) if ram_match else None
    device = {
        "index": "0",
        "uuid": "jetson-integrated",
        "name": jetson_device_name(),
        "utilization_percent": numeric(gpu_match.group(1)) if gpu_match else None,
        "memory_total_bytes": int(total_mb * 1024 * 1024) if total_mb is not None else None,
        "memory_used_bytes": int(used_mb * 1024 * 1024) if used_mb is not None else None,
        "memory_percent": round((used_mb / total_mb) * 100.0, 1) if total_mb else None,
        "temperature_c": numeric(temp_match.group(1)) if temp_match else None,
        "raw": output,
    }
    return {"available": True, "kind": "jetson", "devices": [device], "processes": [], "user_summary": []}


def gpu_info():
    nvidia = nvidia_gpu_info()
    jetson = jetson_gpu_info()
    if nvidia and jetson:
        jetson_device = jetson["devices"][0]
        for device in nvidia.get("devices", []):
            device["name"] = jetson_device.get("name") or device.get("name")
            if device.get("utilization_percent") is None:
                device["utilization_percent"] = jetson_device.get("utilization_percent")
            if device.get("memory_percent") is None:
                device["memory_percent"] = jetson_device.get("memory_percent")
            if device.get("memory_total_bytes") is None:
                device["memory_total_bytes"] = jetson_device.get("memory_total_bytes")
            if device.get("memory_used_bytes") is None:
                device["memory_used_bytes"] = jetson_device.get("memory_used_bytes")
            if device.get("temperature_c") is None:
                device["temperature_c"] = jetson_device.get("temperature_c")
            device["tegrastats_raw"] = jetson_device.get("raw")
        nvidia["kind"] = "jetson+nvidia-smi"
        return nvidia
    return nvidia or jetson or {"available": False, "kind": "none", "devices": [], "processes": [], "user_summary": []}


def uptime_seconds():
    text = read_first("/proc/uptime").split()
    if text:
        try:
            return int(float(text[0]))
        except Exception:
            return None
    return None


def collect():
    disk_before = diskstats_snapshot()
    io_started = time.monotonic()
    load1, load5, load15 = load_average()
    with ThreadPoolExecutor(max_workers=5) as executor:
        gpu_future = executor.submit(gpu_info)
        docker_future = executor.submit(docker_info)
        top_cpu_future = executor.submit(process_rows, "-pcpu", 10)
        top_mem_future = executor.submit(process_rows, "-pmem", 10)
        user_resources_future = executor.submit(user_resource_summary)
        cpu = {
            "percent": cpu_percent(),
            "cores": os.cpu_count(),
            "load1": load1,
            "load5": load5,
            "load15": load15,
        }
        memory = memory_info()
        gpu = gpu_future.result()
        docker = docker_future.result()
        processes = {
            "top_cpu": top_cpu_future.result(),
            "top_mem": top_mem_future.result(),
        }
        user_resources = user_resources_future.result()
    attach_gpu_containers(gpu, docker.get("containers") or [])
    all_gpu_processes = gpu.get("_all_processes") or gpu.get("processes") or []
    if all_gpu_processes:
        gpu["user_summary"] = gpu_user_summary(all_gpu_processes, prefer_attributed=True)
        gpu["user_summary_attributed"] = True
    gpu.pop("_all_processes", None)
    for container in docker.get("containers") or []:
        container.pop("_full_id", None)
    disk_after = diskstats_snapshot()
    storage = storage_info(disk_before, disk_after, max(time.monotonic() - io_started, 0.001))
    return {
        "collected_unix": int(time.time()),
        "host": {
            "hostname": socket.gethostname(),
            "os": os_release(),
            "kernel": platform.release(),
            "machine": platform.machine(),
        },
        "uptime_seconds": uptime_seconds(),
        "cpu": cpu,
        "memory": memory,
        "disk": disk_info(storage),
        "storage": storage,
        "docker": docker,
        "gpu": gpu,
        "processes": processes,
        "user_resources": user_resources,
    }


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, separators=(",", ":")))
