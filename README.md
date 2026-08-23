# Server Probe Dashboard

A lightweight SSH-based Linux resource dashboard. The dashboard host periodically connects to monitored servers over SSH and collects CPU, memory, disk, GPU, process, alert, and short-term history data without installing agents on every target machine.

## Features

- Multi-host dashboard cards
- CPU, memory, GPU utilization, GPU memory, temperature, load average, disk usage, and uptime
- Storage and NAS view with real-mount detection, inode usage, local disk I/O rates, physical disk inventory, and optional SMART health
- Read-only Docker inventory with container resources, image disk usage, GPU process mapping, and expected-container alerts
- vLLM service discovery with safe argument extraction, model/version details, and local `/health` plus `/v1/models` probing
- Per-host history sparklines, current alerts, and per-user GPU usage summaries
- Optional PostgreSQL metric history with restart recovery, 24-hour, 7-day, and 30-day downsampled views
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

## Persistent History

Metric history can reuse the authentication PostgreSQL database or use a separate DSN. Data is written once per refresh for each server, restored into the short-term cache after a service restart, and retained for 30 days by default:

```json
{
  "history_retention_points": 240,
  "persistent_history": {
    "enabled": true,
    "retention_days": 30
  }
}
```

When authentication and history share PostgreSQL, `PROBE_AUTH_DB_DSN` is sufficient. Set `PROBE_HISTORY_DB_DSN` only when history should use a different database. Environment overrides are also available through `PROBE_HISTORY_ENABLED` and `PROBE_HISTORY_RETENTION_DAYS`.

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

Mounts listed in `/etc/fstab` are treated as expected automatically. Manually mounted filesystems can be marked as required in a server entry so a later unmount becomes an alert:

```json
{
  "id": "storage-host",
  "host": "example.internal",
  "user": "root",
  "password_env": "DIRECT_SSH_PASSWORD",
  "expected_mounts": [
    {
      "mount": "/nas",
      "source": "//nas.example.com/share/team",
      "fstype": "cifs"
    }
  ]
}
```

Stopped containers are inventory only and do not alert by default. Add container names to `expected_containers` when a service must remain running:

```json
{
  "expected_containers": ["model-api", "postgres"]
}
```

Docker collection uses `docker ps`, `docker stats --no-stream`, `docker system df`, and narrowly formatted inspect output. Environment variables and complete commands are never returned to the browser. vLLM detection exposes only allowlisted model/runtime flags and probes local or private container endpoints.

Local filesystem capacity and inode data are collected with short timeouts. Network mounts are never traversed for capacity checks because a stale CIFS/NFS mount can put `stat` or `df` into uninterruptible kernel I/O; their health instead uses mount metadata, CIFS kernel connection state when available, and bounded service-port probes. SMART data appears when `smartctl` is installed and the SSH collector user has permission to read the device.

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
