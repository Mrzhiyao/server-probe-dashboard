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
- User-submitted temporary account and long-term access requests with admin approval workflow
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
python -m server_probe.auth set-password alice --role user --display-name "Alice"
```

Use HTTPS in front of the dashboard when exposing it beyond a trusted LAN.

Logged-in users can submit requests from `/requests`. Normal users see a submit page and their own request list. Admins see an approval page and an account-management page. Admins can grant selected normal users access to the resource dashboard with a per-user permission checkbox. Temporary account requests use the current dashboard snapshot to recommend machines. Long-term access requests can be checked against an imported machine-account index before duplicate requests are created. Admins can provision machine accounts from an approved request or directly from the account-management page when the monitored SSH user, or the optional `provision` SSH user, is root or has sudo permission.

Machine account provisioning creates a home directory, sets `/bin/bash` as the login shell, sets the requested or generated password non-interactively, adds the user to the `docker` group, and, when `/disk_*` directories exist, configures a `diskusers` group with group write access on those directories and adds the user to it. Successful provisioning is recorded in the machine-account database index.
Provisioning also creates or updates a same-name dashboard login account with the same password, preserving an existing user's role.

Admins can change any dashboard user's password, and users can change their own password after entering the current password. Password changes can also be synced to machine accounts with the same username recorded in the machine-account index; the dashboard updates those machines through the same root or sudo-capable provisioning credentials.

Slow targets can override the global SSH command timeout in their server entry:

```json
{
  "id": "large-gpu-host",
  "host": "example.internal",
  "user": "collector",
  "command_timeout_seconds": 45,
  "provision": {
    "user": "root",
    "password_env": "LARGE_GPU_ROOT_PASSWORD"
  }
}
```

The optional `provision` block inherits the host, port, and jump-host settings from the server entry unless overridden.

Existing user and machine-account inventories can be imported from JSON without committing private data:

```bash
python -m server_probe.auth import-users-json --source private-inventory < users.json
```

The JSON input can be either a list or an object with a `users` list. Each item can contain `username`, `password`, `display_name`, `machine_key`, `machine_label`, and optional `metadata`.

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
