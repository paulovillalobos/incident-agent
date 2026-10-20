"""SQLite access with client spans, a simulated network round trip and a bounded pool.

The databases are local SQLite files, but every statement sleeps ``rtt_ms`` first so that
the cost of a query round trip resembles a networked database. Statements are recorded
as CLIENT spans with ``db.*`` attributes.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from services.common import otel

_tracer = otel.tracer("services.db")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _operation(sql: str) -> str:
    return sql.lstrip().split(None, 1)[0].upper()


def _table(sql: str) -> str:
    words = sql.replace("(", " ").replace(",", " ").split()
    upper = [w.upper() for w in words]
    for keyword in ("FROM", "INTO", "UPDATE"):
        if keyword in upper:
            idx = upper.index(keyword) + 1
            if idx < len(words):
                return words[idx]
    return "?"


def query(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    db_name: str,
    rtt_ms: float = 0.0,
) -> list[tuple[Any, ...]]:
    """Execute one statement inside a CLIENT span and return all rows."""
    op = _operation(sql)
    with _tracer.start_as_current_span(
        f"{op} {db_name}.{_table(sql)}",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system": "sqlite",
            "db.name": db_name,
            "db.operation": op,
            "db.statement": " ".join(sql.split()),
        },
    ) as span:
        if rtt_ms:
            time.sleep(rtt_ms / 1000)
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        span.set_attribute("db.response.rows", len(rows))
        return rows


class PoolTimeoutError(RuntimeError):
    """Raised when no connection becomes free within the acquire timeout."""


class ConnectionPool:
    """Fixed-size pool; acquiring a connection is itself traced and measured."""

    def __init__(self, path: Path, *, size: int, acquire_timeout_ms: int, name: str) -> None:
        self._path = path
        self._size = size
        self._timeout_s = acquire_timeout_ms / 1000
        self._name = name
        self._slots = threading.BoundedSemaphore(size)
        self._idle: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._in_use = 0
        meter = otel.meter("services.db")
        self._wait = meter.create_histogram(
            "db.pool.wait_time", unit="ms", description="Time spent waiting for a connection"
        )
        self._timeouts = meter.create_counter(
            "db.pool.timeouts", description="Acquire attempts that timed out"
        )

        def in_use(_: CallbackOptions) -> Iterator[Observation]:
            yield Observation(self._in_use, {"db.pool.name": self._name})

        meter.create_observable_gauge(
            "db.pool.in_use", callbacks=[in_use], description="Connections checked out"
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        attrs: dict[str, str | int] = {"db.pool.name": self._name, "db.pool.size": self._size}
        with _tracer.start_as_current_span("db.pool.acquire", attributes=attrs) as span:
            started = time.perf_counter()
            acquired = self._slots.acquire(timeout=self._timeout_s)
            waited_ms = (time.perf_counter() - started) * 1000
            span.set_attribute("db.pool.wait_ms", round(waited_ms, 2))
            self._wait.record(waited_ms, {"db.pool.name": self._name})
            if not acquired:
                self._timeouts.add(1, {"db.pool.name": self._name})
                message = (
                    f"could not acquire a connection from pool '{self._name}' within "
                    f"{int(self._timeout_s * 1000)}ms (size={self._size}, in_use={self._in_use})"
                )
                span.set_status(Status(StatusCode.ERROR, message))
                raise PoolTimeoutError(message)
        with self._lock:
            conn = self._idle.pop() if self._idle else connect(self._path)
            self._in_use += 1
        try:
            yield conn
        finally:
            with self._lock:
                self._idle.append(conn)
                self._in_use -= 1
            self._slots.release()


def current_trace_ids() -> tuple[str, str]:
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
