"""inventory: stock lookup and reservation over the stock database.

Applies pending SQL migrations from ``migrations/`` at startup. A background job syncs
stock from the warehouses every 10 seconds inside one write transaction.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.responses import JSONResponse
from opentelemetry.metrics import CallbackOptions, Observation
from pydantic import BaseModel

from services.common import db, otel, runtime
from services.inventory import repository

SERVICE = "inventory"
cfg = runtime.load_config(SERVICE)
otel.setup(SERVICE, cfg["version"])
app = runtime.create_app(SERVICE)
log = logging.getLogger(SERVICE)
log.setLevel(cfg.get("log_level", "INFO"))
tracer = otel.tracer(SERVICE)

DB_PATH = runtime.data_dir() / "inventory.db"
RTT_MS = float(cfg["db"]["rtt_ms"])
MAX_INFLIGHT = int(cfg["limits"]["max_inflight"])
BASE_WORK_MS = float(cfg["limits"]["base_work_ms"])
_local = threading.local()
_inflight = 0
_inflight_lock = threading.Lock()
_meter = otel.meter(SERVICE)
_shed = _meter.create_counter("inventory.requests.shed", description="Requests rejected by the limiter")


def _inflight_gauge(_: CallbackOptions) -> Iterator[Observation]:
    yield Observation(_inflight)


_meter.create_observable_gauge(
    "inventory.inflight", callbacks=[_inflight_gauge], description="Reservations in progress"
)


def migrate() -> None:
    conn = db.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    for path in sorted((runtime.system_root() / "migrations").glob("*.sql")):
        if path.name in applied:
            continue
        started = time.perf_counter()
        conn.executescript(f"BEGIN;\n{path.read_text()}\nCOMMIT;")
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))
        log.info("applied migration %s in %.0fms", path.name, (time.perf_counter() - started) * 1000)
    conn.close()


def conn() -> Any:
    if not hasattr(_local, "conn"):
        _local.conn = db.connect(DB_PATH)
    return _local.conn


@contextmanager
def admission() -> Iterator[bool]:
    """Load shedding: refuse work beyond MAX_INFLIGHT concurrent reservations."""
    global _inflight
    with _inflight_lock:
        admitted = _inflight < MAX_INFLIGHT
        if admitted:
            _inflight += 1
    try:
        yield admitted
    finally:
        if admitted:
            with _inflight_lock:
                _inflight -= 1


class ReserveRequest(BaseModel):
    sku: str
    quantity: int
    order_id: str


@app.post("/reserve", response_model=None)
def reserve(req: ReserveRequest) -> JSONResponse | dict[str, Any]:
    with admission() as admitted:
        if not admitted:
            _shed.add(1)
            log.warning("shedding reservation for %s: %d in flight (max %d)", req.order_id, _inflight, MAX_INFLIGHT)
            return JSONResponse(status_code=503, content={"error": "overloaded"})
        # Request parsing and allocation scoring; contended CPU grows with concurrency.
        time.sleep(BASE_WORK_MS * (1 + _inflight / 8) / 1000)
        availability = repository.availability(conn(), req.sku, rtt_ms=RTT_MS)
        candidates = [a for a in availability if a["available"] >= req.quantity]
        if not candidates:
            return JSONResponse(status_code=409, content={"error": "out of stock"})
        best = max(candidates, key=lambda a: a["available"])
        repository.decrement(conn(), req.sku, best["warehouse"], req.quantity, rtt_ms=RTT_MS)
        return {"warehouse": best["warehouse"], "availability": availability}


def stock_sync_loop(interval_s: float = 10.0, hold_ms: float = 250.0) -> None:
    sync_conn = db.connect(DB_PATH)
    batch = 0
    while True:
        time.sleep(interval_s)
        batch += 1
        with tracer.start_as_current_span("stock_sync", attributes={"sync.batch": batch}):
            started = time.perf_counter()
            sync_conn.execute("BEGIN IMMEDIATE")
            sync_conn.execute(
                "UPDATE stock SET qty = qty + 1 WHERE warehouse = ? AND sku <= 'SKU-00050'",
                (f"WH-{batch % 100 + 1:03d}",),
            )
            time.sleep(hold_ms / 1000)  # warehouse feed is read while the transaction is open
            sync_conn.execute("COMMIT")
            log.info("stock sync batch %d took %.0fms", batch, (time.perf_counter() - started) * 1000)


migrate()
threading.Thread(target=stock_sync_loop, name="stock-sync", daemon=True).start()

if __name__ == "__main__":
    runtime.serve(app, SERVICE)
