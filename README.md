# Server Probe Dashboard

A lightweight SSH-based Linux resource dashboard. The dashboard host periodically connects to monitored servers over SSH and collects CPU, memory, disk, GPU, process, alert, and short-term history data without installing agents on every target machine.

## Features

- Multi-host dashboard cards
- CPU, memory, GPU utilization, GPU memory, temperature, load average, disk usage, and uptime
- Per-host history sparklines, current alerts, and per-user GPU usage summaries
- Top CPU, memory, and GPU process tables
- NVIDIA GPU metrics through `nvidia-smi`
- Jetson GPU metrics through `tegrastats`
- Direct SSH and SSH jump-host collection
- Secrets read from environment variables, not frontend code or API responses

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config/servers.example.json config/servers.json
PROBE_CONFIG=config/servers.json python -m server_probe.app --host 0.0.0.0 --port 8088
```

Open `http://SERVER_IP:8088`.

## Configuration

The real inventory file is `config/servers.json`, which is ignored by Git. Put passwords in a systemd environment file or shell environment:

```ini
DIRECT_SSH_PASSWORD=...
TARGET_SSH_PASSWORD=...
JUMP_PASSWORD=...
```

## systemd

```ini
[Unit]
Description=Server Probe Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/server-probe-dashboard
EnvironmentFile=/opt/server-probe-dashboard/.env
ExecStart=/opt/server-probe-dashboard/.venv/bin/python -m server_probe.app --host 0.0.0.0 --port 8088
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
