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
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS probe_machine_accounts_username_machine_idx ON probe_machine_accounts(username, machine_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_user_id_idx ON probe_sessions(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_expires_at_idx ON probe_sessions(expires_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_requester_idx ON probe_model_requests(requester_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_status_idx ON probe_model_requests(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_type_idx ON probe_model_requests(request_type)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_machine_accounts_display_idx ON probe_machine_accounts(display_name)")

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

    def list_users(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, role, display_name, is_active, created_at, last_login_at
                    FROM probe_users
                    ORDER BY role, username
                    """
                )
                return [
                    {
                        "id": row[0],
                        "username": row[1],
                        "role": row[2],
                        "display_name": row[3],
                        "is_active": row[4],
                        "created_at": row[5].isoformat() if row[5] else None,
                        "last_login_at": row[6].isoformat() if row[6] else None,
                    }
                    for row in cur.fetchall()
                ]

    def get_user_by_username(self, username):
        username = str(username or "").strip()
        if not username:
            return None
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, role, display_name, is_active, created_at, last_login_at
                    FROM probe_users
                    WHERE lower(username) = lower(%s)
                    """,
                    (username,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "username": row[1],
                    "role": row[2],
                    "display_name": row[3],
                    "is_active": row[4],
                    "created_at": row[5].isoformat() if row[5] else None,
                    "last_login_at": row[6].isoformat() if row[6] else None,
                }

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
                    RETURNING id, username, role, display_name, is_active
                    """,
                    (hashed, username),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "username": row[1],
                    "role": row[2],
                    "display_name": row[3],
                    "is_active": row[4],
                }

    def verify_user(self, username, password):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, password_hash, role, display_name
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
                return {"id": row[0], "username": row[1], "role": row[3], "display_name": row[4]}

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
                    SELECT u.id, u.username, u.role, u.display_name
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
                return {"id": row[0], "username": row[1], "role": row[2], "display_name": row[3]}

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
                      requester_id, request_type, owner_name, model_name, model_size, purpose, access_type,
                      gpu_count, gpu_memory_gb, duration_hours, target_machine, target_machine_label,
                      requested_account, requested_password, notes, recommendation
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        data.get("request_type") or "temporary",
                        data.get("owner_name"),
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
              r.model_name, r.model_size, r.purpose, r.access_type, r.gpu_count,
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
        secret = row[16]
        return {
            "id": row[0],
            "requester_id": row[1],
            "requester": row[2],
            "requester_display_name": row[3],
            "request_type": row[4],
            "owner_name": row[5],
            "model_name": row[6],
            "model_size": row[7],
            "purpose": row[8],
            "access_type": row[9],
            "gpu_count": row[10],
            "gpu_memory_gb": float(row[11]) if row[11] is not None else None,
            "duration_hours": row[12],
            "target_machine": row[13],
            "target_machine_label": row[14],
            "requested_account": row[15],
            "requested_password": secret if include_secret else None,
            "has_requested_password": bool(secret),
            "notes": row[17],
            "status": row[18],
            "recommendation": row[19] or {},
            "admin_note": row[20],
            "allocation_note": row[21],
            "decided_by": row[22],
            "decided_at": row[23].isoformat() if row[23] else None,
            "created_at": row[24].isoformat() if row[24] else None,
            "updated_at": row[25].isoformat() if row[25] else None,
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
