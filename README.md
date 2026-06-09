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
- Optional PostgreSQL-backed login and session access control
- User-submitted model/resource requests with admin approval workflow
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

## Access Control

Authentication is disabled by default. To enable it, install the requirements, create a PostgreSQL database, then set:

```ini
PROBE_AUTH_ENABLED=1
PROBE_AUTH_DB_DSN=postgresql://server_probe:change-me@127.0.0.1:5432/server_probe
PROBE_AUTH_SESSION_HOURS=12
```

Initialize the auth tables and create an admin user:

```bash
python -m server_probe.auth init-db
python -m server_probe.auth set-password admin --role admin
python -m server_probe.auth set-password alice --role user
```

Use HTTPS in front of the dashboard when exposing it beyond a trusted LAN.

Logged-in users can submit model/resource requests from `/requests`. Admins can review all requests, see machine recommendations based on the current dashboard snapshot, create users, and mark requests as approved, rejected, or allocated.

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
