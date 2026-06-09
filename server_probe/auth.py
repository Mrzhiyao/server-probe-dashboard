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
                      is_active BOOLEAN NOT NULL DEFAULT TRUE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      last_login_at TIMESTAMPTZ
                    )
                    """
                )
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
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_user_id_idx ON probe_sessions(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS probe_sessions_expires_at_idx ON probe_sessions(expires_at)")

    def set_password(self, username, password):
        hashed = password_hash(password)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_users (username, password_hash, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (username)
                    DO UPDATE SET password_hash = EXCLUDED.password_hash, is_active = TRUE
                    """,
                    (username, hashed),
                )

    def verify_user(self, username, password):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, username, password_hash FROM probe_users WHERE username = %s AND is_active = TRUE",
                    (username,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                if not verify_password(password, row[2]):
                    return None
                cur.execute("UPDATE probe_users SET last_login_at = now() WHERE id = %s", (row[0],))
                return {"id": row[0], "username": row[1]}

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
                    SELECT u.id, u.username
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
                return {"id": row[0], "username": row[1]}

    def destroy_session(self, token):
        if not token:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM probe_sessions WHERE token_hash = %s", (token_hash(token),))


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
        store.set_password(args.username, password)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
