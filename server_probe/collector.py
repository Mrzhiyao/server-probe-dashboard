#!/usr/bin/env python3
"""Collect one-shot Linux host metrics and print JSON.

This file is intentionally dependency-free because it is copied over SSH and
executed on the target machine through stdin.
"""

import csv
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
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
        servers[host_match.group(1).lower()] = {
            "connected": status == 1 and "DISCONNECTED" not in section,
            "tcp_status": status,
        }
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

    mounted_network = [item for item in mounts if item["status"] == "mounted" and item.get("kind") == "network"]
    if mounted_network:
        cifs_servers = parse_cifs_debug(read_first("/proc/fs/cifs/DebugData"))
        workers = min(6, len(mounted_network))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            probes = list(executor.map(lambda item: network_mount_probe(item, cifs_servers), mounted_network))
        for item, probe in zip(mounted_network, probes):
            item.update(probe)
            if probe.get("error"):
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
        if item.get("status") == "unresponsive" or (item.get("expected") and item.get("status") != "mounted")
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


def gpu_user_summary(processes):
    users = {}
    for process in processes:
        user = process.get("user") or "unknown"
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
        for internal in ("_pids", "_gpus", "_cpu_pids"):
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
    return rows[:10]


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
    cpu = {
        "percent": cpu_percent(),
        "cores": os.cpu_count(),
        "load1": load1,
        "load5": load5,
        "load15": load15,
    }
    memory = memory_info()
    gpu = gpu_info()
    processes = {
        "top_cpu": process_rows("-pcpu", 10),
        "top_mem": process_rows("-pmem", 10),
    }
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
        "gpu": gpu,
        "processes": processes,
    }


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, separators=(",", ":")))
