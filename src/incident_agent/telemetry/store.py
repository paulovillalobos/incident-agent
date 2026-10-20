"""SQLite schema for spans, logs, metric points and the change log.

One database file per scenario run. Writers use :func:`open_writable`; the agent and the
evaluation only ever get connections from :func:`open_readonly`, which are opened with
``mode=ro``, ``PRAGMA query_only`` and an authorizer that rejects anything but reads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    service TEXT NOT NULL,
    instance TEXT,
    version TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    status_message TEXT,
    http_status INTEGER,
    attributes TEXT NOT NULL,
    events TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans (trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_service_start ON spans (service, start_ns);

CREATE TABLE IF NOT EXISTS logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    service TEXT NOT NULL,
    instance TEXT,
    version TEXT,
    severity TEXT NOT NULL,
    body TEXT NOT NULL,
    trace_id TEXT,
    span_id TEXT,
    attributes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_trace ON logs (trace_id);
CREATE INDEX IF NOT EXISTS idx_logs_service_ts ON logs (service, ts_ns);

CREATE TABLE IF NOT EXISTS metric_points (
    point_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    service TEXT NOT NULL,
    instance TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    value REAL,
    count INTEGER,
    sum REAL,
    attributes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON metric_points (name, service, ts_ns);

CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    ts_ns INTEGER NOT NULL,
    service TEXT NOT NULL,
    kind TEXT NOT NULL,
    author TEXT NOT NULL,
    title TEXT NOT NULL,
    version_from TEXT,
    version_to TEXT,
    diff TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def open_writable(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _authorize(action: int, arg1: str | None, *_: object) -> int:
    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in {"table_info", "table_list"}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_readonly(path: Path) -> sqlite3.Connection:
    """Read-only connection: OS-level ``mode=ro`` plus a SQL authorizer that denies writes."""
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorize)
    return conn


def checkpoint(path: Path) -> None:
    """Fold the WAL into the main file so a snapshot is a single self-contained file."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
