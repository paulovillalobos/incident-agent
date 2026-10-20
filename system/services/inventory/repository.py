"""Stock queries used by the reservation endpoint."""

from __future__ import annotations

import sqlite3
from typing import Any

from services.common import db


def availability(conn: sqlite3.Connection, sku: str, *, rtt_ms: float) -> list[dict[str, Any]]:
    """Available quantity per warehouse for one SKU (stock minus active holds)."""
    rows = db.query(
        conn,
        "SELECT s.warehouse, w.region, s.qty - COALESCE(h.held, 0) FROM stock AS s"
        " JOIN warehouses AS w ON w.id = s.warehouse"
        " LEFT JOIN holds AS h ON h.sku = s.sku AND h.warehouse = s.warehouse"
        " WHERE s.sku = ?",
        (sku,),
        db_name="inventory",
        rtt_ms=rtt_ms,
    )
    return [
        {"warehouse": w, "region": r, "available": a, "eta_days": 1 if r == "PT" else 3}
        for w, r, a in rows
    ]


def decrement(conn: sqlite3.Connection, sku: str, warehouse: str, quantity: int, *, rtt_ms: float) -> None:
    db.query(
        conn,
        "UPDATE stock SET qty = qty - ? WHERE sku = ? AND warehouse = ?",
        (quantity, sku, warehouse),
        db_name="inventory",
        rtt_ms=rtt_ms,
    )
