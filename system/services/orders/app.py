"""orders: creates an order, reserves stock, charges the customer, emits a fulfilment message."""

from __future__ import annotations

import itertools
import json
import logging
import threading
import uuid
from typing import Any

import httpx
from fastapi.responses import JSONResponse
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from services.common import db, http, otel, runtime
from services.common.container import enforce_memory_limit
from services.orders import tax

SERVICE = "orders"
cfg = runtime.load_config(SERVICE)
otel.setup(SERVICE, cfg["version"])
enforce_memory_limit(int(cfg["memory_limit_mb"]))
app = runtime.create_app(SERVICE)
log = logging.getLogger(SERVICE)
tracer = otel.tracer(SERVICE)
client = httpx.Client()

DATA = runtime.data_dir()
RTT_MS = float(cfg["db"]["rtt_ms"])
pool = db.ConnectionPool(
    DATA / "orders.db",
    size=int(cfg["db"]["pool_size"]),
    acquire_timeout_ms=int(cfg["db"]["acquire_timeout_ms"]),
    name="orders",
)
inventory_policy = http.RetryPolicy.from_config(cfg["inventory"])
payments_policy = http.RetryPolicy.from_config(cfg["payments"])
_replicas = itertools.cycle(cfg["payments"]["replicas"])
_replica_lock = threading.Lock()


def _init_db() -> None:
    conn = db.connect(DATA / "orders.db")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, tier TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, customer_id INTEGER, sku TEXT, quantity INTEGER,
            total_cents INTEGER, warehouse TEXT, charge_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO customers (id, tier)
            WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 2000)
            SELECT i, CASE WHEN i % 10 = 0 THEN 'gold' ELSE 'standard' END FROM n;
        """
    )
    queue = db.connect(DATA / "queue.db")
    queue.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, payload TEXT NOT NULL,
            traceparent TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', last_error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_messages_status ON messages (status, id);
        """
    )
    conn.close()
    queue.close()


_init_db()
_queue = db.connect(DATA / "queue.db")
_queue_lock = threading.Lock()


class OrderRequest(BaseModel):
    customer_id: int
    sku: str
    quantity: int
    region: str
    client_version: str
    delivery_window: str | None = None
    note: str = ""


def unit_price_cents(sku: str) -> int:
    return 500 + (int(sku.split("-")[1]) * 37) % 9500


def next_replica() -> str:
    with _replica_lock:
        return str(next(_replicas))


def publish(topic: str, message: dict[str, Any]) -> None:
    with tracer.start_as_current_span(
        f"{topic} publish",
        kind=SpanKind.PRODUCER,
        attributes={"messaging.system": "sqlite-queue", "messaging.destination.name": topic},
    ):
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        with _queue_lock:
            db.query(
                _queue,
                "INSERT INTO messages (topic, payload, traceparent) VALUES (?, ?, ?)",
                (topic, json.dumps(message), carrier.get("traceparent")),
                db_name="queue",
                rtt_ms=RTT_MS,
            )


@app.post("/orders", response_model=None)
def create_order(req: OrderRequest) -> JSONResponse | dict[str, Any]:
    order_id = uuid.uuid4().hex[:12]
    with pool.connection() as conn:
        rows = db.query(
            conn,
            "SELECT id, tier FROM customers WHERE id = ?",
            (req.customer_id,),
            db_name="orders",
            rtt_ms=RTT_MS,
        )
        if not rows:
            return JSONResponse(status_code=404, content={"error": "unknown customer"})
        amount = unit_price_cents(req.sku) * req.quantity
        total = amount + tax.compute(req.region, amount, use_v2=bool(cfg["flags"]["tax_v2"]))

        reserve = http.post(
            client,
            f"{runtime.url('inventory')}/reserve",
            {"sku": req.sku, "quantity": req.quantity, "order_id": order_id},
            inventory_policy,
        )
        if reserve.status_code == 409:
            return JSONResponse(status_code=409, content={"error": "out of stock"})
        if reserve.status_code != 200:
            log.error("inventory reservation failed for order %s: %d", order_id, reserve.status_code)
            return JSONResponse(status_code=503, content={"error": "inventory unavailable"})
        reservation: dict[str, Any] = reserve.json()

        replica = next_replica()
        charge = http.post(
            client,
            f"{runtime.url(replica)}/charge",
            {"order_id": order_id, "amount_cents": total, "customer_id": req.customer_id},
            payments_policy,
        )
        if charge.status_code != 200:
            log.error(
                "payment failed for order %s via %s: %d %s",
                order_id,
                replica,
                charge.status_code,
                charge.text[:200],
            )
            return JSONResponse(status_code=502, content={"error": "payment failed"})
        charge_id = str(charge.json()["charge_id"])

        db.query(
            conn,
            "INSERT INTO orders (id, customer_id, sku, quantity, total_cents, warehouse, charge_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, req.customer_id, req.sku, req.quantity, total, reservation["warehouse"], charge_id),
            db_name="orders",
            rtt_ms=RTT_MS,
        )
    publish(
        "fulfilment",
        {
            "order_id": order_id,
            "sku": req.sku,
            "quantity": req.quantity,
            "warehouse": reservation["warehouse"],
            "delivery_window": req.delivery_window,
            "note": req.note,
        },
    )
    return {"order_id": order_id, "total_cents": total, "charge_id": charge_id}


if __name__ == "__main__":
    runtime.serve(app, SERVICE)
