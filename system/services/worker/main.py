"""worker: consumes fulfilment messages from the queue in order of arrival."""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from opentelemetry import propagate
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from services.common import db, otel, runtime

SERVICE = "worker"
cfg = runtime.load_config(SERVICE)
otel.setup(SERVICE, cfg["version"])
log = logging.getLogger(SERVICE)
tracer = otel.tracer(SERVICE)
QUEUE = runtime.data_dir() / "queue.db"
MAX_ATTEMPTS = int(cfg["max_attempts"])
_running = True
_depth = 0


def _depth_gauge(_: CallbackOptions) -> Iterator[Observation]:
    yield Observation(_depth, {"messaging.destination.name": "fulfilment"})


otel.meter(SERVICE).create_observable_gauge(
    "queue.depth", callbacks=[_depth_gauge], description="Pending fulfilment messages"
)
_processed = otel.meter(SERVICE).create_counter("queue.messages.processed")


def parse_window(raw: str) -> tuple[datetime, datetime]:
    start, end = raw.split("-")
    return datetime.strptime(start.strip(), "%H:%M"), datetime.strptime(end.strip(), "%H:%M")


def fulfil(message: dict[str, Any]) -> None:
    if message.get("delivery_window"):
        parse_window(message["delivery_window"])
    time.sleep(0.003)  # pick list generation


def process_next(conn: Any) -> bool:
    global _depth
    _depth = conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'pending'").fetchone()[0]
    row = conn.execute(
        "SELECT id, payload, traceparent, attempts FROM messages"
        " WHERE status = 'pending' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    msg_id, payload, traceparent, attempts = row
    ctx = propagate.extract({"traceparent": traceparent} if traceparent else {})
    with tracer.start_as_current_span(
        "fulfilment process",
        context=ctx,
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "sqlite-queue",
            "messaging.destination.name": "fulfilment",
            "messaging.message.id": str(msg_id),
            "messaging.message.delivery_attempt": attempts + 1,
        },
    ) as span:
        try:
            fulfil(json.loads(payload))
        except Exception as exc:
            attempts += 1
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            dead = MAX_ATTEMPTS > 0 and attempts >= MAX_ATTEMPTS
            conn.execute(
                "UPDATE messages SET attempts = ?, last_error = ?, status = ? WHERE id = ?",
                (attempts, str(exc), "dead" if dead else "pending", msg_id),
            )
            log.error(
                "failed to process message %d (attempt %d): %s: %s payload=%s",
                msg_id,
                attempts,
                type(exc).__name__,
                exc,
                payload,
            )
            if dead:
                log.warning("message %d moved to dead letter after %d attempts", msg_id, attempts)
            time.sleep(cfg["poll_interval_ms"] / 1000)
            return True
        conn.execute("UPDATE messages SET status = 'done' WHERE id = ?", (msg_id,))
        _processed.add(1)
        return True


def main() -> None:
    def stop(*_: object) -> None:
        global _running
        _running = False

    signal.signal(signal.SIGTERM, stop)
    while not QUEUE.exists() and _running:
        time.sleep(0.2)
    conn = db.connect(QUEUE)
    log.info("worker %s consuming fulfilment (max_attempts=%d)", cfg["version"], MAX_ATTEMPTS)
    ready_file = os.environ.get("IA_READY_FILE")
    if ready_file:
        open(ready_file, "w").close()
    while _running:
        try:
            busy = process_next(conn)
        except Exception:
            log.exception("queue poll failed")
            busy = False
        if not busy:
            time.sleep(cfg["poll_interval_ms"] / 1000)
    otel.shutdown()


if __name__ == "__main__":
    main()
