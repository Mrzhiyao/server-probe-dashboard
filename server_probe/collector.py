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


def disk_info():
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
        "name": "Jetson integrated GPU",
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
    load1, load5, load15 = load_average()
    return {
        "collected_unix": int(time.time()),
        "host": {
            "hostname": socket.gethostname(),
            "os": os_release(),
            "kernel": platform.release(),
            "machine": platform.machine(),
        },
        "uptime_seconds": uptime_seconds(),
        "cpu": {
            "percent": cpu_percent(),
            "cores": os.cpu_count(),
            "load1": load1,
            "load5": load5,
            "load15": load15,
        },
        "memory": memory_info(),
        "disk": disk_info(),
        "gpu": gpu_info(),
        "processes": {
            "top_cpu": process_rows("-pcpu", 10),
            "top_mem": process_rows("-pmem", 10),
        },
    }


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, separators=(",", ":")))
