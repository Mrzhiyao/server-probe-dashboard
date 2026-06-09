#!/usr/bin/env python3
"""PostgreSQL-backed authentication for the dashboard."""

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone


DEFAULT_ITERATIONS = 260000


def utc_now():
    return datetime.now(timezone.utc)


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
                      is_active BOOLEAN NOT NULL DEFAULT TRUE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      last_login_at TIMESTAMPTZ
                    )
                    """
                )
                cur.execute("ALTER TABLE probe_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
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
                      model_name TEXT NOT NULL,
                      model_size TEXT,
                      purpose TEXT NOT NULL,
                      access_type TEXT NOT NULL DEFAULT 'ssh',
                      gpu_count INTEGER NOT NULL DEFAULT 1,
                      gpu_memory_gb NUMERIC(8, 2),
                      duration_hours INTEGER,
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
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_user_id_idx ON probe_sessions(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_expires_at_idx ON probe_sessions(expires_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_requester_idx ON probe_model_requests(requester_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_model_requests_status_idx ON probe_model_requests(status)")

    def set_password(self, username, password, role=None):
        hashed = password_hash(password)
        role = role if role in ("admin", "user") else None
        with self.connect() as conn:
            with conn.cursor() as cur:
                if role:
                    cur.execute(
                        """
                        INSERT INTO probe_users (username, password_hash, role, is_active)
                        VALUES (%s, %s, %s, TRUE)
                        ON CONFLICT (username)
                        DO UPDATE SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, is_active = TRUE
                        """,
                        (username, hashed, role),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO probe_users (username, password_hash, is_active)
                        VALUES (%s, %s, TRUE)
                        ON CONFLICT (username)
                        DO UPDATE SET password_hash = EXCLUDED.password_hash, is_active = TRUE
                        """,
                        (username, hashed),
                    )

    def list_users(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, role, is_active, created_at, last_login_at
                    FROM probe_users
                    ORDER BY username
                    """
                )
                return [
                    {
                        "id": row[0],
                        "username": row[1],
                        "role": row[2],
                        "is_active": row[3],
                        "created_at": row[4].isoformat() if row[4] else None,
                        "last_login_at": row[5].isoformat() if row[5] else None,
                    }
                    for row in cur.fetchall()
                ]

    def verify_user(self, username, password):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, username, password_hash, role FROM probe_users WHERE username = %s AND is_active = TRUE",
                    (username,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                if not verify_password(password, row[2]):
                    return None
                cur.execute("UPDATE probe_users SET last_login_at = now() WHERE id = %s", (row[0],))
                return {"id": row[0], "username": row[1], "role": row[3]}

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
                    SELECT u.id, u.username, u.role
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
                return {"id": row[0], "username": row[1], "role": row[2]}

    def destroy_session(self, token):
        if not token:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM probe_sessions WHERE token_hash = %s", (token_hash(token),))

    def create_model_request(self, user_id, data, recommendation):
        from psycopg2.extras import Json

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_model_requests (
                      requester_id, model_name, model_size, purpose, access_type,
                      gpu_count, gpu_memory_gb, duration_hours, notes, recommendation
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        data.get("model_name"),
                        data.get("model_size"),
                        data.get("purpose"),
                        data.get("access_type") or "ssh",
                        int(data.get("gpu_count") if data.get("gpu_count") is not None else 1),
                        data.get("gpu_memory_gb"),
                        data.get("duration_hours"),
                        data.get("notes"),
                        Json(recommendation or {}),
                    ),
                )
                return cur.fetchone()[0]

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
                    """
                    SELECT
                      r.id, r.requester_id, u.username, r.model_name, r.model_size,
                      r.purpose, r.access_type, r.gpu_count, r.gpu_memory_gb,
                      r.duration_hours, r.notes, r.status, r.recommendation,
                      r.admin_note, r.allocation_note, a.username, r.decided_at,
                      r.created_at, r.updated_at
                    FROM probe_model_requests r
                    JOIN probe_users u ON u.id = r.requester_id
                    LEFT JOIN probe_users a ON a.id = r.decided_by
                    %s
                    ORDER BY r.created_at DESC
                    """
                    % where,
                    params,
                )
                return [self.model_request_dict(row) for row in cur.fetchall()]

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
                    RETURNING
                      id, requester_id, NULL::text, model_name, model_size,
                      purpose, access_type, gpu_count, gpu_memory_gb,
                      duration_hours, notes, status, recommendation,
                      admin_note, allocation_note, NULL::text, decided_at,
                      created_at, updated_at
                    """,
                    (status, admin_note, allocation_note, admin_id, status, request_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self.model_request_dict(row)

    def model_request_dict(self, row):
        return {
            "id": row[0],
            "requester_id": row[1],
            "requester": row[2],
            "model_name": row[3],
            "model_size": row[4],
            "purpose": row[5],
            "access_type": row[6],
            "gpu_count": row[7],
            "gpu_memory_gb": float(row[8]) if row[8] is not None else None,
            "duration_hours": row[9],
            "notes": row[10],
            "status": row[11],
            "recommendation": row[12] or {},
            "admin_note": row[13],
            "allocation_note": row[14],
            "decided_by": row[15],
            "decided_at": row[16].isoformat() if row[16] else None,
            "created_at": row[17].isoformat() if row[17] else None,
            "updated_at": row[18].isoformat() if row[18] else None,
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
    set_password_parser.add_argument("--stdin", action="store_true", help="Read password from stdin")

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
        store.set_password(args.username, password, role=args.role)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
