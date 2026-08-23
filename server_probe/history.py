#!/usr/bin/env python3
"""PostgreSQL-backed metric history for the dashboard."""

import math
from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc)


def as_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return utc_now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def bucket_seconds(hours, max_points):
    seconds = max(float(hours), 1.0 / 60.0) * 3600.0
    points = max(2, int(max_points))
    return max(60, int(math.ceil(seconds / points / 60.0) * 60))


class HistoryStore:
    def __init__(self, dsn, retention_days=30):
        self.dsn = dsn
        self.retention_days = max(1, int(retention_days))

    def connect(self):
        try:
            import psycopg2
            import psycopg2.extras
        except Exception as exc:
            raise RuntimeError("psycopg2 is required for persistent metric history") from exc
        return psycopg2.connect(self.dsn)

    def setup(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS probe_metric_history (
                      server_id TEXT NOT NULL,
                      collected_at TIMESTAMPTZ NOT NULL,
                      status TEXT NOT NULL,
                      cpu_percent DOUBLE PRECISION,
                      memory_percent DOUBLE PRECISION,
                      gpu_percent DOUBLE PRECISION,
                      gpu_memory_percent DOUBLE PRECISION,
                      gpu_peak_percent DOUBLE PRECISION,
                      disk_percent DOUBLE PRECISION,
                      load1 DOUBLE PRECISION,
                      storage JSONB NOT NULL DEFAULT '{}'::jsonb,
                      PRIMARY KEY (server_id, collected_at)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS probe_metric_history_time_idx ON probe_metric_history(collected_at)"
                )

    def append_samples(self, samples):
        rows = []
        for server_id, sample in samples:
            if not server_id or not sample:
                continue
            rows.append(
                (
                    str(server_id),
                    parse_time(sample.get("time")),
                    str(sample.get("status") or "unknown"),
                    as_float(sample.get("cpu")),
                    as_float(sample.get("mem")),
                    as_float(sample.get("gpu")),
                    as_float(sample.get("gpu_mem")),
                    as_float(sample.get("gpu_peak")),
                    as_float(sample.get("disk")),
                    as_float(sample.get("load1")),
                    sample.get("storage") or {},
                )
            )
        if not rows:
            return 0
        with self.connect() as conn:
            with conn.cursor() as cur:
                from psycopg2.extras import Json, execute_values

                values = [row[:-1] + (Json(row[-1]),) for row in rows]
                execute_values(
                    cur,
                    """
                    INSERT INTO probe_metric_history (
                      server_id, collected_at, status, cpu_percent, memory_percent,
                      gpu_percent, gpu_memory_percent, gpu_peak_percent, disk_percent,
                      load1, storage
                    ) VALUES %s
                    ON CONFLICT (server_id, collected_at) DO UPDATE SET
                      status = EXCLUDED.status,
                      cpu_percent = EXCLUDED.cpu_percent,
                      memory_percent = EXCLUDED.memory_percent,
                      gpu_percent = EXCLUDED.gpu_percent,
                      gpu_memory_percent = EXCLUDED.gpu_memory_percent,
                      gpu_peak_percent = EXCLUDED.gpu_peak_percent,
                      disk_percent = EXCLUDED.disk_percent,
                      load1 = EXCLUDED.load1,
                      storage = EXCLUDED.storage
                    """,
                    values,
                    page_size=200,
                )
        return len(rows)

    def load_recent(self, server_ids, limit_per_server=240):
        server_ids = [str(value) for value in server_ids if value]
        if not server_ids:
            return {}
        limit_per_server = max(2, min(2000, int(limit_per_server)))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT server_id, collected_at, status, cpu_percent, memory_percent,
                           gpu_percent, gpu_memory_percent, gpu_peak_percent, disk_percent,
                           load1, storage
                    FROM (
                      SELECT h.*,
                             row_number() OVER (PARTITION BY server_id ORDER BY collected_at DESC) AS row_num
                      FROM probe_metric_history h
                      WHERE server_id = ANY(%s)
                    ) recent
                    WHERE row_num <= %s
                    ORDER BY server_id, collected_at
                    """,
                    (server_ids, limit_per_server),
                )
                return self.rows_by_server(cur.fetchall())

    def query_range(self, server_ids, hours=24, max_points=240):
        server_ids = [str(value) for value in server_ids if value]
        if not server_ids:
            return {}
        hours = max(1.0 / 60.0, min(float(hours), self.retention_days * 24.0))
        max_points = max(30, min(720, int(max_points)))
        interval_seconds = bucket_seconds(hours, max_points)
        end = utc_now()
        start = end - timedelta(hours=hours)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT server_id,
                           date_bin((%s || ' seconds')::interval, collected_at, TIMESTAMPTZ '1970-01-01 00:00:00+00') AS bucket,
                           CASE WHEN bool_or(status = 'online') THEN 'online' ELSE 'offline' END AS status,
                           avg(cpu_percent), avg(memory_percent), avg(gpu_percent),
                           avg(gpu_memory_percent), avg(gpu_peak_percent), avg(disk_percent),
                           avg(load1), '{}'::jsonb
                    FROM probe_metric_history
                    WHERE server_id = ANY(%s) AND collected_at >= %s AND collected_at <= %s
                    GROUP BY server_id, bucket
                    ORDER BY server_id, bucket
                    """,
                    (str(interval_seconds), server_ids, start, end),
                )
                return self.rows_by_server(cur.fetchall())

    def rows_by_server(self, rows):
        history = {}
        for row in rows:
            sample = {
                "time": row[1].isoformat() if row[1] else None,
                "status": row[2],
                "cpu": as_float(row[3]),
                "mem": as_float(row[4]),
                "gpu": as_float(row[5]),
                "gpu_mem": as_float(row[6]),
                "gpu_peak": as_float(row[7]),
                "disk": as_float(row[8]),
                "load1": as_float(row[9]),
                "storage": row[10] or {},
            }
            history.setdefault(row[0], []).append(sample)
        return history

    def cleanup(self):
        cutoff = utc_now() - timedelta(days=self.retention_days)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM probe_metric_history WHERE collected_at < %s", (cutoff,))
                return cur.rowcount

    def stats(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*), min(collected_at), max(collected_at), count(DISTINCT server_id) FROM probe_metric_history"
                )
                count, oldest, newest, servers = cur.fetchone()
        return {
            "row_count": int(count or 0),
            "oldest_at": oldest.isoformat() if oldest else None,
            "newest_at": newest.isoformat() if newest else None,
            "server_count": int(servers or 0),
            "retention_days": self.retention_days,
        }
