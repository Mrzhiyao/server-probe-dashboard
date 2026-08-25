# Server Probe Dashboard

A lightweight SSH-based Linux resource dashboard. The dashboard host periodically connects to monitored servers over SSH and collects CPU, memory, disk, GPU, process, alert, and short-term history data without installing agents on every target machine.

## Features

- Multi-host dashboard cards
- CPU, memory, GPU utilization, GPU memory, temperature, load average, disk usage, and uptime
- Storage and NAS view with real-mount detection, inode usage, local disk I/O rates, physical disk inventory, and optional SMART health
- Read-only Docker inventory with container resources, image disk usage, GPU process mapping, and expected-container alerts
- vLLM service discovery with safe argument extraction, model/version details, and local `/health` plus `/v1/models` probing
- Per-host history sparklines, current alerts, and per-user GPU usage summaries
- Optional Feishu webhook notifications with alert confirmation, cooldown reminders, and recovery messages
- Optional hourly Feishu summaries of ordinary-user CPU, memory, GPU memory, top GPU processes, and attributed containers
- Authenticated per-person drill-down pages linked from Feishu cards
- Optional Feishu WebSocket bot for read-only person, machine, and idle-GPU queries
- Optional PostgreSQL metric history with restart recovery, 24-hour, 7-day, and 30-day downsampled views
- Top CPU, memory, and GPU process tables
- NVIDIA GPU metrics through `nvidia-smi`
- Jetson GPU metrics through `tegrastats`
- Direct SSH and SSH jump-host collection
- Optional PostgreSQL-backed login and session access control
- User-submitted temporary account and long-term access requests with admin approval workflow
- NAS-backed model catalog with architecture, weight-size, quantization, verification, and admin enable/disable controls
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
PROBE_AUTH_COOKIE_SECURE=auto
```

Initialize the auth tables and create an admin user:

```bash
python -m server_probe.auth init-db
python -m server_probe.auth set-password admin --role admin
python -m server_probe.auth set-password alice --role user --display-name "Alice"
```

Use HTTPS in front of the dashboard when exposing it beyond a trusted LAN.
The `auto` cookie mode adds the `Secure` flag for HTTPS reverse-proxy requests while still allowing direct HTTP access on a trusted LAN. Use `1` for HTTPS-only deployments or `0` for HTTP-only deployments.
When reverse-proxied, the application only accepts `X-Real-IP` from a loopback peer; direct clients cannot override the address used by login rate limiting.

## Feishu Query Bot

The optional enterprise self-built bot uses Feishu's long-connection SDK and does not require a public callback URL. Store `FEISHU_APP_ID` and `FEISHU_APP_SECRET` only in the server environment file. It supports resource queries, account-request forms, administrator approval cards, and guarded direct provisioning. Set `FEISHU_ADMIN_OPEN_IDS` before enabling approval or provisioning actions; the backend still enforces administrator identity and account-operation permissions for every callback.

Set `FEISHU_BOT_LLM_BASE_URL`, `FEISHU_BOT_LLM_API_KEY`, and `FEISHU_BOT_LLM_MODEL` to enable the optional OpenAI-compatible query planner. When enabled, the model interprets natural-language queries, short follow-ups, and multiple questions in one message, then produces up to three validated tool plans. Supported filters include machine group, GPU count, minimum free GPU memory, and idle status. A read-only usage-ranking tool reports high-resource users from the current hourly aggregation and groups machine accounts by the registered display name. A machine-catalog tool grounds questions such as “what is the eight-GPU machine?” in a sanitized inventory containing machine labels, groups, GPU counts, and model names. Composed process questions are represented as a constrained resource-query plan with machine scope, metric, and result limit, allowing GPU-memory, memory, and CPU rankings without exposing full command lines. The model receives the user's current text, the previous query plan, and this sanitized inventory; live utilization metrics, passwords, approval records, and SSH credentials are not sent to it. Live results are always calculated locally and rendered from the dashboard snapshot. Destructive or privileged operations remain protected by fixed backend allowlists and role checks.

Run it separately from the dashboard HTTP service using `systemd/server-probe-feishu-bot.service` so connection failures and restarts do not affect metric collection.

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

## Alert Notifications

Feishu group-bot delivery is enabled explicitly for real-time alerts, hourly usage reports, or both. Keep the webhook and optional signing secret in the server environment file; never place them in the inventory or frontend:

```ini
PROBE_NOTIFICATIONS_ENABLED=1
PROBE_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...
PROBE_FEISHU_SIGNING_SECRET=...
PROBE_NOTIFICATION_CRITICAL_CONSECUTIVE=2
PROBE_NOTIFICATION_WARNING_AFTER_SECONDS=300
PROBE_NOTIFICATION_COOLDOWN_SECONDS=1800
PROBE_NOTIFICATION_RECOVERY_ENABLED=1
```

The signing secret is optional when the bot has not enabled signature verification. By default, critical alerts require two consecutive samples, warnings must remain active for five minutes, active notifications repeat after a 30-minute cooldown, and recovery notifications are sent. A server going offline does not falsely resolve its earlier resource alerts; those alerts are evaluated after collection recovers.

For a quieter operational summary, leave real-time alerts disabled and enable the hourly report:

```ini
PROBE_NOTIFICATIONS_ENABLED=0
PROBE_USAGE_REPORT_ENABLED=1
PROBE_USAGE_REPORT_INTERVAL_SECONDS=3600
PROBE_USAGE_REPORT_EXCLUDED_USERS=root,nobody
PROBE_USAGE_REPORT_MAX_USERS=80
PROBE_USAGE_REPORT_DETAIL_USERS=10
```

The collector aggregates all processes owned by ordinary login UIDs once per regular dashboard refresh. The hourly report resolves account names from the PostgreSQL user and machine-account index, groups a person's accounts and machines under one display name, shows cluster summary indicators and capacity bars, then highlights GPU users and high-memory users. For each machine it also reports the process using the most GPU memory. Container processes are attributed to the inferred container owner when possible; multi-GPU memory for the same PID is combined. Machine-specific account names remain visible for auditing. Each person links to an authenticated `/usage` page with current GPU-process and container details. Root and system accounts are omitted. This is metadata-only monitoring: full process command lines, environment variables, and user files are not included in the report.

Logged-in users can submit requests from `/requests`. Normal users see a submit page and their own request list. Admins see an approval page and an account-management page. Admins can grant selected normal users access to the resource dashboard with a per-user permission checkbox. Temporary account requests use the current dashboard snapshot to recommend machines. Long-term access requests can be checked against an imported machine-account index before duplicate requests are created. Admins can provision machine accounts from an approved request or directly from the account-management page when the monitored SSH user, or the optional `provision` SSH user, is root or has sudo permission.

Machine account provisioning creates a home directory, sets `/bin/bash` as the login shell, sets the requested or generated password non-interactively, adds the user to the `docker` group, and, when `/disk_*` directories exist, configures a `diskusers` group with group write access on those directories and adds the user to it. Successful provisioning is recorded in the machine-account database index.
Provisioning also creates or updates a same-name dashboard login account with the same password, preserving an existing user's role.

## Model Catalog

The authenticated request center includes a model-service page backed by an existing mounted model directory. The scanner reads only immediate model metadata and weight-file sizes; it never reads tensor contents. Administrators can verify models, choose the served API name and recommended GPU count, and enable selected models for normal users. Enabled models can be copied directly into the existing API request form.

```ini
PROBE_MODEL_CATALOG_ROOT=/nas/yaozhi/models
PROBE_MODEL_DEPLOYMENT_ROOT=/mnt/bnu-model-nas/yaozhi/models
PROBE_MODEL_CATALOG_CACHE_SECONDS=300
```

## Managed Model Services

The optional managed-service workflow connects the enabled model catalog to vLLM workers and an existing One API SQLite deployment. It keeps model-service instances separate from per-user API allocations, so an already-running model is reused while each approved requester receives an independently restricted and expiring token.

Copy `config/model-deployments.example.json` to an ignored private configuration file and set:

```ini
PROBE_MODEL_DEPLOYMENT_CONFIG=/opt/server-probe-dashboard/config/model-deployments.json
```

Only administrators can deploy directly or approve a model request. The generated API key is returned once by the protected deployment endpoint and is sent by the Feishu bot as a private message; list and status APIs never include token secrets. Worker model roots, GPU allowlists, Docker images, port ranges, One API database paths, and optional pre-existing seed services are all configured outside Git.

`PROBE_MODEL_CATALOG_ROOT` is the path visible to the dashboard host. `PROBE_MODEL_DEPLOYMENT_ROOT` is the corresponding read-only path on deployment workers. Verification and enablement settings are stored in PostgreSQL; NAS credentials and model contents are not stored in the database or returned to normal users.

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

Docker does not retain the Linux user who originally ran `docker run`. The dashboard reports an owner only when it can use an explicit label or infer one from a Compose working directory or `/home/<user>` bind mount. Add an explicit label for exact attribution:

```bash
docker run --label server-probe.owner="$USER" ...
```

Docker Compose can use the same label:

```yaml
services:
  model-api:
    labels:
      server-probe.owner: "${USER}"
```

The container-internal runtime user is displayed separately and is never treated as the creator.

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
