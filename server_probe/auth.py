#!/usr/bin/env python3
"""PostgreSQL-backed authentication for the dashboard."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone


DEFAULT_ITERATIONS = 260000


def utc_now():
    return datetime.now(timezone.utc)


def normalized_key(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def contains_cjk(value):
    return any("\u4e00" <= char <= "\u9fff" for char in str(value or ""))


def password_hash(password, iterations=DEFAULT_ITERATIONS):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%s$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored_hash):
    try:
        method, iterations, salt, expected = stored_hash.split("$", 3)
        if method != "pbkdf2_sha256":
            return False
        iterations = int(iterations)
        salt = base64.b64decode(salt.encode("ascii"))
        expected_bytes = base64.b64decode(expected.encode("ascii"))
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected_bytes)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, dsn, session_hours=12):
        self.dsn = dsn
        self.session_hours = int(session_hours)

    def connect(self):
        try:
            import psycopg2
            import psycopg2.extras
        except Exception as exc:
            raise RuntimeError("psycopg2 is required when authentication is enabled") from exc
        return psycopg2.connect(self.dsn)

    def setup(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_users (
                      id BIGSERIAL PRIMARY KEY,
                      username TEXT NOT NULL UNIQUE,
                      password_hash TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'user',
                      display_name TEXT,
                      profile JSONB NOT NULL DEFAULT '{}'::jsonb,
                      is_active BOOLEAN NOT NULL DEFAULT TRUE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      last_login_at TIMESTAMPTZ
                    )
                    """
                )
                cur.execute("ALTER TABLE probe_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
                cur.execute("ALTER TABLE probe_users ADD COLUMN IF NOT EXISTS display_name TEXT")
                cur.execute("ALTER TABLE probe_users ADD COLUMN IF NOT EXISTS profile JSONB NOT NULL DEFAULT '{}'::jsonb")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_sessions (
                      token_hash TEXT PRIMARY KEY,
                      user_id BIGINT NOT NULL REFERENCES probe_users(id) ON DELETE CASCADE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      expires_at TIMESTAMPTZ NOT NULL,
                      last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      ip_address TEXT,
                      user_agent TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_model_requests (
                      id BIGSERIAL PRIMARY KEY,
                      requester_id BIGINT NOT NULL REFERENCES probe_users(id) ON DELETE CASCADE,
                      request_type TEXT NOT NULL DEFAULT 'temporary',
                      owner_name TEXT,
                      model_name TEXT NOT NULL,
                      model_size TEXT,
                      purpose TEXT NOT NULL,
                      access_type TEXT NOT NULL DEFAULT 'ssh',
                      gpu_count INTEGER NOT NULL DEFAULT 1,
                      gpu_memory_gb NUMERIC(8, 2),
                      duration_hours INTEGER,
                      target_machine TEXT,
                      target_machine_label TEXT,
                      requested_account TEXT,
                      requested_password TEXT,
                      notes TEXT,
                      status TEXT NOT NULL DEFAULT 'pending',
                      recommendation JSONB NOT NULL DEFAULT '{}'::jsonb,
                      admin_note TEXT,
                      allocation_note TEXT,
                      decided_by BIGINT REFERENCES probe_users(id) ON DELETE SET NULL,
                      decided_at TIMESTAMPTZ,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS request_type TEXT NOT NULL DEFAULT 'temporary'")
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS owner_name TEXT")
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS target_machine TEXT")
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS target_machine_label TEXT")
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS requested_account TEXT")
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS requested_password TEXT")
                cur.execute("ALTER TABLE probe_model_requests ADD COLUMN IF NOT EXISTS model_key TEXT")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_machine_accounts (
                      id BIGSERIAL PRIMARY KEY,
                      display_name TEXT,
                      username TEXT NOT NULL,
                      machine_key TEXT NOT NULL DEFAULT '',
                      machine_label TEXT,
                      source TEXT NOT NULL DEFAULT 'manual',
                      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_model_catalog_settings (
                      model_key TEXT PRIMARY KEY,
                      enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      served_model_name TEXT,
                      recommended_gpu_count INTEGER NOT NULL DEFAULT 1,
                      verification_status TEXT NOT NULL DEFAULT 'untested',
                      notes TEXT,
                      updated_by BIGINT REFERENCES probe_users(id) ON DELETE SET NULL,
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_model_services (
                      id BIGSERIAL PRIMARY KEY,
                      model_key TEXT NOT NULL,
                      model_name TEXT NOT NULL,
                      served_name TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'deploying',
                      worker_id TEXT NOT NULL,
                      worker_name TEXT,
                      container_name TEXT NOT NULL UNIQUE,
                      gpu_indices JSONB NOT NULL DEFAULT '[]'::jsonb,
                      host_port INTEGER,
                      image TEXT,
                      oneapi_channel_id BIGINT,
                      source TEXT NOT NULL DEFAULT 'managed',
                      error_message TEXT,
                      progress_stage TEXT,
                      progress_percent INTEGER NOT NULL DEFAULT 0,
                      created_by BIGINT REFERENCES probe_users(id) ON DELETE SET NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      started_at TIMESTAMPTZ,
                      stopped_at TIMESTAMPTZ
                    )
                    """
                )
                cur.execute("ALTER TABLE probe_model_services ADD COLUMN IF NOT EXISTS progress_stage TEXT")
                cur.execute("ALTER TABLE probe_model_services ADD COLUMN IF NOT EXISTS progress_percent INTEGER NOT NULL DEFAULT 0")
                cur.execute(
                    """
                    UPDATE probe_model_services
                    SET progress_stage = COALESCE(progress_stage, 'running'),
                        progress_percent = 100
                    WHERE status = 'running' AND progress_percent < 100
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_model_allocations (
                      id BIGSERIAL PRIMARY KEY,
                      service_id BIGINT NOT NULL REFERENCES probe_model_services(id) ON DELETE CASCADE,
                      request_id BIGINT UNIQUE REFERENCES probe_model_requests(id) ON DELETE SET NULL,
                      requester_id BIGINT NOT NULL REFERENCES probe_users(id) ON DELETE CASCADE,
                      owner_name TEXT,
                      status TEXT NOT NULL DEFAULT 'provisioning',
                      oneapi_token_id BIGINT,
                      token_name TEXT,
                      quota BIGINT,
                      expires_at TIMESTAMPTZ,
                      error_message TEXT,
                      created_by BIGINT REFERENCES probe_users(id) ON DELETE SET NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      revoked_at TIMESTAMPTZ
                    )
                    """
                )
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS probe_machine_accounts_username_machine_idx ON probe_machine_accounts(username, machine_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_user_id_idx ON probe_sessions(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_expires_at_idx ON probe_sessions(expires_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_requester_idx ON probe_model_requests(requester_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_status_idx ON probe_model_requests(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_type_idx ON probe_model_requests(request_type)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_machine_accounts_display_idx ON probe_machine_accounts(display_name)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_services_model_idx ON probe_model_services(model_key, status)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_allocations_service_idx ON probe_model_allocations(service_id, status)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_allocations_requester_idx ON probe_model_allocations(requester_id, status)")

    def set_password(self, username, password, role=None, display_name=None):
        hashed = password_hash(password)
        role = role if role in ("admin", "user") else None
        display_name = str(display_name).strip() if display_name else None
        with self.connect() as conn:
            with conn.cursor() as cur:
                if role:
                    cur.execute(
                        """
                        INSERT INTO probe_users (username, password_hash, role, display_name, is_active)
                        VALUES (%s, %s, %s, %s, TRUE)
                        ON CONFLICT (username)
                        DO UPDATE SET
                          password_hash = EXCLUDED.password_hash,
                          role = EXCLUDED.role,
                          display_name = COALESCE(EXCLUDED.display_name, probe_users.display_name),
                          is_active = TRUE
                        """,
                        (username, hashed, role, display_name),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO probe_users (username, password_hash, display_name, is_active)
                        VALUES (%s, %s, %s, TRUE)
                        ON CONFLICT (username)
                        DO UPDATE SET
                          password_hash = EXCLUDED.password_hash,
                          display_name = COALESCE(EXCLUDED.display_name, probe_users.display_name),
                          is_active = TRUE
                        """,
                        (username, hashed, display_name),
                    )

    def user_dict(self, row):
        profile = row[7] or {}
        can_view_dashboard = bool(row[2] == "admin" or profile.get("can_view_dashboard"))
        return {
            "id": row[0],
            "username": row[1],
            "role": row[2],
            "display_name": row[3],
            "is_active": row[4],
            "created_at": row[5].isoformat() if row[5] else None,
            "last_login_at": row[6].isoformat() if row[6] else None,
            "profile": profile,
            "can_view_dashboard": can_view_dashboard,
        }

    def list_users(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, role, display_name, is_active, created_at, last_login_at, profile
                    FROM probe_users
                    ORDER BY role, username
                    """
                )
                return [self.user_dict(row) for row in cur.fetchall()]

    def resource_identity_map(self):
        account_names = {}
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT username, display_name
                    FROM probe_machine_accounts
                    WHERE display_name IS NOT NULL AND btrim(display_name) <> ''
                    """
                )
                for username, display_name in cur.fetchall():
                    key = str(username or "").strip().lower()
                    name = str(display_name or "").strip()
                    if key and name:
                        account_names.setdefault(key, set()).add(name)
                cur.execute(
                    """
                    SELECT username, display_name
                    FROM probe_users
                    WHERE is_active = TRUE AND display_name IS NOT NULL AND btrim(display_name) <> ''
                    """
                )
                current_users = cur.fetchall()

        identities = {
            username: next(iter(names))
            for username, names in account_names.items()
            if len(names) == 1
        }
        for username, display_name in current_users:
            key = str(username or "").strip().lower()
            name = str(display_name or "").strip()
            if key and name and (contains_cjk(name) or key not in identities):
                identities[key] = name
        return identities

    def get_user_by_username(self, username):
        username = str(username or "").strip()
        if not username:
            return None
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, role, display_name, is_active, created_at, last_login_at, profile
                    FROM probe_users
                    WHERE lower(username) = lower(%s)
                    """,
                    (username,),
                )
                row = cur.fetchone()
                return self.user_dict(row) if row else None

    def update_user_permissions(self, username, can_view_dashboard=False):
        username = str(username or "").strip()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE probe_users
                    SET profile = jsonb_set(
                      COALESCE(profile, '{}'::jsonb),
                      '{can_view_dashboard}',
                      to_jsonb(%s::boolean),
                      true
                    )
                    WHERE lower(username) = lower(%s)
                    RETURNING id, username, role, display_name, is_active, created_at, last_login_at, profile
                    """,
                    (bool(can_view_dashboard), username),
                )
                row = cur.fetchone()
                return self.user_dict(row) if row else None

    def model_catalog_settings(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT model_key, enabled, served_model_name, recommended_gpu_count,
                           verification_status, notes, updated_at
                    FROM probe_model_catalog_settings
                    ORDER BY lower(model_key)
                    """
                )
                rows = cur.fetchall()
        return {
            row[0]: {
                "enabled": bool(row[1]),
                "served_model_name": row[2],
                "recommended_gpu_count": row[3],
                "verification_status": row[4],
                "notes": row[5],
                "updated_at": row[6].isoformat() if row[6] else None,
            }
            for row in rows
        }

    def update_model_catalog_setting(self, model_key, admin_id, data):
        model_key = str(model_key or "").strip()
        if not model_key or len(model_key) > 240:
            raise ValueError("invalid model key")
        verification_status = str(data.get("verification_status") or "untested")
        if verification_status not in ("untested", "verified", "blocked"):
            raise ValueError("invalid verification status")
        try:
            gpu_count = max(0, min(int(data.get("recommended_gpu_count") or 0), 16))
        except (TypeError, ValueError):
            raise ValueError("invalid recommended GPU count") from None
        served_model_name = str(data.get("served_model_name") or "").strip()[:200] or None
        notes = str(data.get("notes") or "").strip()[:1000] or None
        enabled = bool(data.get("enabled")) and verification_status != "blocked"
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_model_catalog_settings (
                      model_key, enabled, served_model_name, recommended_gpu_count,
                      verification_status, notes, updated_by, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (model_key) DO UPDATE SET
                      enabled = EXCLUDED.enabled,
                      served_model_name = EXCLUDED.served_model_name,
                      recommended_gpu_count = EXCLUDED.recommended_gpu_count,
                      verification_status = EXCLUDED.verification_status,
                      notes = EXCLUDED.notes,
                      updated_by = EXCLUDED.updated_by,
                      updated_at = now()
                    RETURNING model_key, enabled, served_model_name, recommended_gpu_count,
                              verification_status, notes, updated_at
                    """,
                    (model_key, enabled, served_model_name, gpu_count, verification_status, notes, admin_id),
                )
                row = cur.fetchone()
        return {
            "model_key": row[0],
            "enabled": bool(row[1]),
            "served_model_name": row[2],
            "recommended_gpu_count": row[3],
            "verification_status": row[4],
            "notes": row[5],
            "updated_at": row[6].isoformat() if row[6] else None,
        }

    def model_service_dict(self, row):
        return {
            "id": row[0],
            "model_key": row[1],
            "model_name": row[2],
            "served_name": row[3],
            "status": row[4],
            "worker_id": row[5],
            "worker_name": row[6],
            "container_name": row[7],
            "gpu_indices": [str(value) for value in (row[8] or [])],
            "host_port": row[9],
            "image": row[10],
            "oneapi_channel_id": row[11],
            "source": row[12],
            "error_message": row[13],
            "progress_stage": row[14],
            "progress_percent": int(row[15] or 0),
            "created_at": row[16].isoformat() if row[16] else None,
            "updated_at": row[17].isoformat() if row[17] else None,
            "started_at": row[18].isoformat() if row[18] else None,
            "stopped_at": row[19].isoformat() if row[19] else None,
            "active_allocations": int(row[20] or 0),
            "total_allocations": int(row[21] or 0),
            "next_expiry": row[22].isoformat() if row[22] else None,
        }

    def model_service_select_sql(self):
        return """
            SELECT
              s.id, s.model_key, s.model_name, s.served_name, s.status,
              s.worker_id, s.worker_name, s.container_name, s.gpu_indices,
              s.host_port, s.image, s.oneapi_channel_id, s.source, s.error_message,
              s.progress_stage, s.progress_percent,
              s.created_at, s.updated_at, s.started_at, s.stopped_at,
              COUNT(a.id) FILTER (
                WHERE a.status = 'active' AND (a.expires_at IS NULL OR a.expires_at > now())
              ) AS active_allocations,
              COUNT(a.id) AS total_allocations,
              MIN(a.expires_at) FILTER (
                WHERE a.status = 'active' AND a.expires_at > now()
              ) AS next_expiry
            FROM probe_model_services s
            LEFT JOIN probe_model_allocations a ON a.service_id = s.id
        """

    def list_model_services(self, user=None):
        where = ""
        params = ()
        if user and user.get("role") != "admin":
            where = """
                WHERE EXISTS (
                  SELECT 1 FROM probe_model_allocations visible
                  WHERE visible.service_id = s.id AND visible.requester_id = %s
                )
            """
            params = (user["id"],)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    self.model_service_select_sql()
                    + where
                    + """
                    GROUP BY s.id
                    ORDER BY s.created_at DESC
                    """,
                    params,
                )
                return [self.model_service_dict(row) for row in cur.fetchall()]

    def get_model_service(self, service_id):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    self.model_service_select_sql()
                    + """
                    WHERE s.id = %s
                    GROUP BY s.id
                    """,
                    (service_id,),
                )
                row = cur.fetchone()
                return self.model_service_dict(row) if row else None

    def find_active_model_service(self, model_key):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    self.model_service_select_sql()
                    + """
                    WHERE s.model_key = %s AND s.status IN ('deploying', 'running')
                    GROUP BY s.id
                    ORDER BY CASE WHEN s.status = 'running' THEN 0 ELSE 1 END, s.created_at
                    LIMIT 1
                    """,
                    (str(model_key or "").strip(),),
                )
                row = cur.fetchone()
                return self.model_service_dict(row) if row else None

    def create_model_service(self, data):
        from psycopg2.extras import Json

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_model_services (
                      model_key, model_name, served_name, status, worker_id, worker_name,
                      container_name, gpu_indices, host_port, image, oneapi_channel_id,
                      source, error_message, progress_stage, progress_percent, created_by, started_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              CASE WHEN %s = 'running' THEN now() ELSE NULL END)
                    RETURNING id
                    """,
                    (
                        data.get("model_key"), data.get("model_name"), data.get("served_name"),
                        data.get("status") or "deploying", data.get("worker_id"), data.get("worker_name"),
                        data.get("container_name"), Json(data.get("gpu_indices") or []), data.get("host_port"),
                        data.get("image"), data.get("oneapi_channel_id"), data.get("source") or "managed",
                        data.get("error_message"), data.get("progress_stage"), int(data.get("progress_percent") or 0),
                        data.get("created_by"), data.get("status") or "deploying",
                    ),
                )
                service_id = cur.fetchone()[0]
        return self.get_model_service(service_id)

    def seed_model_service(self, data):
        from psycopg2.extras import Json

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_model_services (
                      model_key, model_name, served_name, status, worker_id, worker_name,
                      container_name, gpu_indices, host_port, image, oneapi_channel_id,
                      source, progress_stage, progress_percent, created_by, started_at
                    ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s, 'seed', 'running', 100, %s, now())
                    ON CONFLICT (container_name) DO NOTHING
                    RETURNING id
                    """,
                    (
                        data.get("model_key"), data.get("model_name"), data.get("served_name"),
                        data.get("worker_id"), data.get("worker_name"), data.get("container_name"),
                        Json(data.get("gpu_indices") or []), data.get("host_port"), data.get("image"),
                        data.get("oneapi_channel_id"), data.get("created_by"),
                    ),
                )
                row = cur.fetchone()
                service_id = row[0] if row else None
                if service_id is None:
                    cur.execute("SELECT id FROM probe_model_services WHERE container_name = %s", (data.get("container_name"),))
                    service_id = cur.fetchone()[0]
        return self.get_model_service(service_id)

    def update_model_service(self, service_id, **values):
        from psycopg2.extras import Json

        columns = {
            "status": "status",
            "gpu_indices": "gpu_indices",
            "host_port": "host_port",
            "oneapi_channel_id": "oneapi_channel_id",
            "error_message": "error_message",
            "progress_stage": "progress_stage",
            "progress_percent": "progress_percent",
        }
        assignments = []
        params = []
        for key, column in columns.items():
            if key not in values:
                continue
            assignments.append("%s = %%s" % column)
            params.append(Json(values[key]) if key == "gpu_indices" else values[key])
        if not assignments:
            return self.get_model_service(service_id)
        status = values.get("status")
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, now())")
            assignments.append("stopped_at = NULL")
        elif status in ("stopped", "failed"):
            assignments.append("stopped_at = now()")
        assignments.append("updated_at = now()")
        params.append(service_id)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE probe_model_services SET %s WHERE id = %%s" % ", ".join(assignments),
                    tuple(params),
                )
        return self.get_model_service(service_id)

    def model_allocation_dict(self, row):
        return {
            "id": row[0],
            "service_id": row[1],
            "request_id": row[2],
            "requester_id": row[3],
            "owner_name": row[4],
            "status": row[5],
            "oneapi_token_id": row[6],
            "token_name": row[7],
            "quota": row[8],
            "expires_at": row[9].isoformat() if row[9] else None,
            "error_message": row[10],
            "created_at": row[11].isoformat() if row[11] else None,
            "updated_at": row[12].isoformat() if row[12] else None,
            "revoked_at": row[13].isoformat() if row[13] else None,
        }

    def get_model_allocation(self, allocation_id=None, request_id=None):
        if allocation_id is None and request_id is None:
            return None
        field = "id" if allocation_id is not None else "request_id"
        value = allocation_id if allocation_id is not None else request_id
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, service_id, request_id, requester_id, owner_name, status,
                           oneapi_token_id, token_name, quota, expires_at, error_message,
                           created_at, updated_at, revoked_at
                    FROM probe_model_allocations
                    WHERE %s = %%s
                    """ % field,
                    (value,),
                )
                row = cur.fetchone()
                return self.model_allocation_dict(row) if row else None

    def create_model_allocation(self, data):
        with self.connect() as conn:
            with conn.cursor() as cur:
                if data.get("request_id") is not None:
                    cur.execute("SELECT id FROM probe_model_allocations WHERE request_id = %s", (data.get("request_id"),))
                    existing = cur.fetchone()
                    if existing:
                        return self.get_model_allocation(allocation_id=existing[0])
                cur.execute(
                    """
                    INSERT INTO probe_model_allocations (
                      service_id, request_id, requester_id, owner_name, status,
                      token_name, quota, expires_at, created_by
                    ) VALUES (%s, %s, %s, %s, 'provisioning', %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        data.get("service_id"), data.get("request_id"), data.get("requester_id"),
                        data.get("owner_name"), data.get("token_name"), data.get("quota"),
                        data.get("expires_at"), data.get("created_by"),
                    ),
                )
                allocation_id = cur.fetchone()[0]
        return self.get_model_allocation(allocation_id=allocation_id)

    def update_model_allocation(self, allocation_id, status, token_id=None, error_message=None):
        if status not in ("provisioning", "active", "failed", "revoked", "expired"):
            raise ValueError("invalid model allocation status")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE probe_model_allocations
                    SET status = %s,
                        oneapi_token_id = COALESCE(%s, oneapi_token_id),
                        error_message = %s,
                        revoked_at = CASE WHEN %s IN ('revoked', 'expired') THEN now() ELSE revoked_at END,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (status, token_id, error_message, status, allocation_id),
                )
        return self.get_model_allocation(allocation_id=allocation_id)

    def update_existing_password(self, username, password):
        username = str(username or "").strip()
        if not username:
            return None
        hashed = password_hash(password)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE probe_users
                    SET password_hash = %s
                    WHERE lower(username) = lower(%s)
                    RETURNING id, username, role, display_name, is_active, created_at, last_login_at, profile
                    """,
                    (hashed, username),
                )
                row = cur.fetchone()
                return self.user_dict(row) if row else None

    def verify_user(self, username, password):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, password_hash, role, display_name, is_active, created_at, last_login_at, profile
                    FROM probe_users
                    WHERE username = %s AND is_active = TRUE
                    """,
                    (username,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                if not verify_password(password, row[2]):
                    return None
                cur.execute("UPDATE probe_users SET last_login_at = now() WHERE id = %s", (row[0],))
                return self.user_dict((row[0], row[1], row[3], row[4], row[5], row[6], row[7], row[8]))

    def create_session(self, user_id, ip_address="", user_agent=""):
        token = secrets.token_urlsafe(32)
        expires_at = utc_now() + timedelta(hours=self.session_hours)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM probe_sessions WHERE expires_at <= now()")
                cur.execute(
                    """
                    INSERT INTO probe_sessions (token_hash, user_id, expires_at, ip_address, user_agent)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (token_hash(token), user_id, expires_at, ip_address[:128], user_agent[:400]),
                )
        return token, expires_at

    def user_for_session(self, token):
        if not token:
            return None
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.id, u.username, u.role, u.display_name, u.is_active, u.created_at, u.last_login_at, u.profile
                    FROM probe_sessions s
                    JOIN probe_users u ON u.id = s.user_id
                    WHERE s.token_hash = %s
                      AND s.expires_at > now()
                      AND u.is_active = TRUE
                    """,
                    (token_hash(token),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute(
                    "UPDATE probe_sessions SET last_seen_at = now() WHERE token_hash = %s",
                    (token_hash(token),),
                )
                return self.user_dict(row)

    def destroy_session(self, token):
        if not token:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM probe_sessions WHERE token_hash = %s", (token_hash(token),))

    def upsert_machine_account(self, username, display_name="", machine_key="", machine_label="", source="manual", metadata=None):
        from psycopg2.extras import Json

        username = str(username or "").strip()
        if not username:
            return None
        machine_key = str(machine_key or machine_label or "").strip()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_machine_accounts (
                      username, display_name, machine_key, machine_label, source, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (username, machine_key)
                    DO UPDATE SET
                      display_name = COALESCE(EXCLUDED.display_name, probe_machine_accounts.display_name),
                      machine_label = COALESCE(EXCLUDED.machine_label, probe_machine_accounts.machine_label),
                      source = EXCLUDED.source,
                      metadata = EXCLUDED.metadata,
                      updated_at = now()
                    RETURNING id
                    """,
                    (
                        username,
                        str(display_name or "").strip() or None,
                        machine_key,
                        str(machine_label or "").strip() or None,
                        str(source or "manual")[:80],
                        Json(metadata or {}),
                    ),
                )
                return cur.fetchone()[0]

    def machine_accounts_for_username(self, username):
        username = str(username or "").strip()
        if not username:
            return []
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, display_name, username, machine_key, machine_label, source, metadata, created_at, updated_at
                    FROM probe_machine_accounts
                    WHERE lower(username) = lower(%s)
                    ORDER BY machine_label NULLS LAST, machine_key
                    """,
                    (username,),
                )
                return [self.machine_account_dict(row) for row in cur.fetchall()]

    def find_existing_accounts(self, display_name="", target_machine="", account_name=""):
        display_name = str(display_name or "").strip()
        account_name = str(account_name or "").strip()
        target_norm = normalized_key(target_machine)
        conditions = []
        params = []
        if display_name:
            conditions.append("lower(display_name) = lower(%s)")
            params.append(display_name)
        if account_name:
            conditions.append("lower(username) = lower(%s)")
            params.append(account_name)
        if not conditions:
            return []
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, display_name, username, machine_key, machine_label, source, metadata, created_at, updated_at
                    FROM probe_machine_accounts
                    WHERE %s
                    ORDER BY updated_at DESC
                    LIMIT 100
                    """
                    % " OR ".join(conditions),
                    tuple(params),
                )
                rows = cur.fetchall()

        matches = []
        for row in rows:
            item = self.machine_account_dict(row)
            machine_values = [
                item.get("machine_key"),
                item.get("machine_label"),
                (item.get("metadata") or {}).get("server_id"),
                (item.get("metadata") or {}).get("host"),
            ]
            machine_norms = [normalized_key(value) for value in machine_values if value]
            machine_matches = not target_norm or any(
                target_norm == value or target_norm in value or value in target_norm for value in machine_norms
            )
            if machine_matches:
                matches.append(item)
            if len(matches) >= 10:
                break
        return matches

    def machine_account_dict(self, row):
        return {
            "id": row[0],
            "display_name": row[1],
            "username": row[2],
            "machine_key": row[3],
            "machine_label": row[4],
            "source": row[5],
            "metadata": row[6] or {},
            "created_at": row[7].isoformat() if row[7] else None,
            "updated_at": row[8].isoformat() if row[8] else None,
        }

    def create_model_request(self, user_id, data, recommendation):
        from psycopg2.extras import Json

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_model_requests (
                      requester_id, request_type, owner_name, model_key, model_name, model_size, purpose, access_type,
                      gpu_count, gpu_memory_gb, duration_hours, target_machine, target_machine_label,
                      requested_account, requested_password, notes, recommendation
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        data.get("request_type") or "temporary",
                        data.get("owner_name"),
                        data.get("model_key"),
                        data.get("model_name"),
                        data.get("model_size"),
                        data.get("purpose"),
                        data.get("access_type") or "ssh",
                        int(data.get("gpu_count") if data.get("gpu_count") is not None else 1),
                        data.get("gpu_memory_gb"),
                        data.get("duration_hours"),
                        data.get("target_machine"),
                        data.get("target_machine_label"),
                        data.get("requested_account"),
                        data.get("requested_password"),
                        data.get("notes"),
                        Json(recommendation or {}),
                    ),
                )
                return cur.fetchone()[0]

    def request_select_sql(self):
        return """
            SELECT
              r.id, r.requester_id, u.username, u.display_name, r.request_type, r.owner_name,
              r.model_key, r.model_name, r.model_size, r.purpose, r.access_type, r.gpu_count,
              r.gpu_memory_gb, r.duration_hours, r.target_machine, r.target_machine_label,
              r.requested_account, r.requested_password, r.notes, r.status,
              r.recommendation, r.admin_note, r.allocation_note, a.username,
              r.decided_at, r.created_at, r.updated_at
            FROM probe_model_requests r
            JOIN probe_users u ON u.id = r.requester_id
            LEFT JOIN probe_users a ON a.id = r.decided_by
        """

    def list_model_requests(self, user):
        is_admin = user.get("role") == "admin"
        with self.connect() as conn:
            with conn.cursor() as cur:
                where = ""
                params = ()
                if not is_admin:
                    where = "WHERE r.requester_id = %s"
                    params = (user["id"],)
                cur.execute(
                    self.request_select_sql()
                    + """
                    %s
                    ORDER BY r.created_at DESC
                    """
                    % where,
                    params,
                )
                return [self.model_request_dict(row, include_secret=is_admin) for row in cur.fetchall()]

    def get_model_request(self, request_id, include_secret=False):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    self.request_select_sql()
                    + """
                    WHERE r.id = %s
                    """,
                    (request_id,),
                )
                row = cur.fetchone()
                return self.model_request_dict(row, include_secret=include_secret) if row else None

    def update_model_request(self, request_id, admin_id, status, admin_note="", allocation_note=""):
        if status not in ("pending", "approved", "rejected", "allocated"):
            raise ValueError("invalid status")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE probe_model_requests
                    SET status = %s,
                        admin_note = %s,
                        allocation_note = %s,
                        decided_by = %s,
                        decided_at = CASE WHEN %s = 'pending' THEN NULL ELSE now() END,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (status, admin_note, allocation_note, admin_id, status, request_id),
                )
                if cur.rowcount < 1:
                    return None
        return self.get_model_request(request_id, include_secret=True)

    def model_request_dict(self, row, include_secret=False):
        secret = row[17]
        return {
            "id": row[0],
            "requester_id": row[1],
            "requester": row[2],
            "requester_display_name": row[3],
            "request_type": row[4],
            "owner_name": row[5],
            "model_key": row[6],
            "model_name": row[7],
            "model_size": row[8],
            "purpose": row[9],
            "access_type": row[10],
            "gpu_count": row[11],
            "gpu_memory_gb": float(row[12]) if row[12] is not None else None,
            "duration_hours": row[13],
            "target_machine": row[14],
            "target_machine_label": row[15],
            "requested_account": row[16],
            "requested_password": secret if include_secret else None,
            "has_requested_password": bool(secret),
            "notes": row[18],
            "status": row[19],
            "recommendation": row[20] or {},
            "admin_note": row[21],
            "allocation_note": row[22],
            "decided_by": row[23],
            "decided_at": row[24].isoformat() if row[24] else None,
            "created_at": row[25].isoformat() if row[25] else None,
            "updated_at": row[26].isoformat() if row[26] else None,
        }


def env_dsn():
    return os.getenv("PROBE_AUTH_DB_DSN", "postgresql://server_probe:server_probe@127.0.0.1:5432/server_probe")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage Server Probe Dashboard auth")
    parser.add_argument("--dsn", default=env_dsn())
    parser.add_argument("--session-hours", default=int(os.getenv("PROBE_AUTH_SESSION_HOURS", "12")), type=int)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    set_password_parser = subparsers.add_parser("set-password")
    set_password_parser.add_argument("username")
    set_password_parser.add_argument("--role", choices=("admin", "user"), default=None)
    set_password_parser.add_argument("--display-name", default=None)
    set_password_parser.add_argument("--stdin", action="store_true", help="Read password from stdin")

    import_parser = subparsers.add_parser("import-users-json")
    import_parser.add_argument("--role", choices=("admin", "user"), default="user")
    import_parser.add_argument("--source", default="json")

    args = parser.parse_args(argv)
    store = AuthStore(args.dsn, args.session_hours)

    if args.command == "init-db":
        store.setup()
        return 0

    if args.command == "set-password":
        if args.stdin:
            password = sys.stdin.read().strip("\r\n")
        else:
            import getpass

            password = getpass.getpass("Password: ")
        if not password:
            raise SystemExit("empty password is not allowed")
        store.setup()
        store.set_password(args.username, password, role=args.role, display_name=args.display_name)
        return 0

    if args.command == "import-users-json":
        payload = json.load(sys.stdin)
        records = payload.get("users") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise SystemExit("expected a JSON list or an object with a users list")
        store.setup()
        imported = 0
        accounts = 0
        for item in records:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            password = str(item.get("password") or "")
            display_name = str(item.get("display_name") or "").strip() or None
            if username and password:
                store.set_password(username, password, role=item.get("role") or args.role, display_name=display_name)
                imported += 1
            if username and (item.get("machine_key") or item.get("machine_label")):
                store.upsert_machine_account(
                    username,
                    display_name=display_name or "",
                    machine_key=item.get("machine_key") or "",
                    machine_label=item.get("machine_label") or "",
                    source=item.get("source") or args.source,
                    metadata=item.get("metadata") or {},
                )
                accounts += 1
        print("imported_users=%s machine_accounts=%s" % (imported, accounts))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
