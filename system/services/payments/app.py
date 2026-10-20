"""payments: charges through the external payment provider and records a ledger entry.

Runs as two replicas (payments-a, payments-b), each with its own ledger volume.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any

import httpx
from fastapi.responses import JSONResponse
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from services.common import db, http, otel, runtime

SERVICE = "payments"
INSTANCE = os.environ.get("IA_INSTANCE", SERVICE)
cfg = runtime.load_config(SERVICE)
otel.setup(SERVICE, cfg["version"])
app = runtime.create_app(SERVICE)
log = logging.getLogger(SERVICE)
tracer = otel.tracer(SERVICE)
client = httpx.Client()
policy = http.RetryPolicy(timeout_ms=int(cfg["provider_timeout_ms"]), connect_retries=1)

VOLUME = runtime.data_dir() / INSTANCE
VOLUME.mkdir(parents=True, exist_ok=True)
LEDGER = VOLUME / "ledger.db"
RTT_MS = float(cfg["db"]["rtt_ms"])
_local = threading.local()


def conn() -> Any:
    """Per-thread ledger connection, capped at the replica's volume quota."""
    if not hasattr(_local, "conn"):
        c = db.connect(LEDGER)
        page_size = c.execute("PRAGMA page_size").fetchone()[0]
        c.execute(f"PRAGMA max_page_count = {int(cfg['volume_quota_mb']) * 1024 * 1024 // page_size}")
        c.execute(
            "CREATE TABLE IF NOT EXISTS ledger (charge_id TEXT PRIMARY KEY, order_id TEXT,"
            " amount_cents INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS audit_export (id INTEGER PRIMARY KEY, chunk BLOB)")
        _local.conn = c
    return _local.conn


class ChargeRequest(BaseModel):
    order_id: str
    amount_cents: int
    customer_id: int


@app.post("/charge", response_model=None)
def charge(req: ChargeRequest) -> JSONResponse | dict[str, Any]:
    region = str(cfg["provider_region"])
    try:
        with tracer.start_as_current_span(
            "provider.charge",
            kind=SpanKind.CLIENT,
            attributes={"peer.service": f"payment-provider-{region}", "provider.region": region},
        ):
            resp = http.post(
                client, f"{runtime.url('provider-' + region)}/v1/charges", req.model_dump(), policy
            )
    except httpx.TimeoutException:
        log.error("payment provider %s timed out after %dms", region, policy.timeout_ms)
        return JSONResponse(status_code=504, content={"error": "provider timeout"})
    if resp.status_code != 200:
        log.error("payment provider %s returned %d", region, resp.status_code)
        return JSONResponse(status_code=502, content={"error": "provider error"})
    charge_id = f"ch_{uuid.uuid4().hex[:14]}"
    db.query(
        conn(),
        "INSERT INTO ledger (charge_id, order_id, amount_cents) VALUES (?, ?, ?)",
        (charge_id, req.order_id, req.amount_cents),
        db_name="ledger",
        rtt_ms=RTT_MS,
    )
    return {"charge_id": charge_id, "provider_ref": resp.json()["id"]}


class ExportRequest(BaseModel):
    megabytes: int


@app.post("/admin/audit-export")
def audit_export(req: ExportRequest) -> dict[str, Any]:
    """Nightly audit export, staged on the replica volume before upload."""
    log.info("audit export started on %s: staging %d MB in %s", INSTANCE, req.megabytes, LEDGER)
    budget = req.megabytes * 1024 * 1024
    written = 0
    error: Exception | None = None
    for size in (256 * 1024, 16 * 1024, 1024):  # smaller chunks once large ones stop fitting
        try:
            while written + size <= budget:
                conn().execute("INSERT INTO audit_export (chunk) VALUES (?)", (os.urandom(size),))
                written += size
        except Exception as exc:
            error = exc
    if written < budget:
        log.warning("audit export stopped after %d bytes: %s", written, error)
    return {"bytes": written}


if __name__ == "__main__":
    runtime.serve(app, INSTANCE)
