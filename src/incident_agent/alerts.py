"""Alert rules evaluated over the telemetry store after a run.

The alert is the only thing the agent is told up front, so it is computed from the same
telemetry the agent can query, never written by hand. Rules use 5-second buckets and must
hold for three consecutive buckets (15 s), so a short blip from a routine restart does not
page anyone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

BUCKET_S = 5
SUSTAIN = 3
ERROR_RATE = 0.05
P95_MS = 400.0
BACKLOG = 50


@dataclass(frozen=True)
class Alert:
    rule: str
    service: str
    summary: str
    started_ns: int
    fired_ns: int
    value: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[int(0.95 * (len(ordered) - 1))]


def _sustained(buckets: list[tuple[int, float]], threshold: float) -> tuple[int, int, float] | None:
    run: list[tuple[int, float]] = []
    for idx, value in buckets:
        if value > threshold and (not run or run[-1][0] == idx - 1):
            run.append((idx, value))
        elif value > threshold:
            run = [(idx, value)]
        else:
            run = []
        if len(run) >= SUSTAIN:
            return run[0][0], run[-1][0], max(v for _, v in run)
    return None


def evaluate(conn: sqlite3.Connection, start_ns: int) -> list[Alert]:
    """Return alerts in firing order (earliest first)."""
    width = BUCKET_S * 1_000_000_000
    rows = conn.execute(
        "SELECT start_ns, duration_ms, COALESCE(http_status, 0) FROM spans"
        " WHERE service = 'gateway' AND kind = 'SERVER' AND name = 'POST /checkout'"
    ).fetchall()
    per_bucket: dict[int, list[tuple[float, int]]] = {}
    for ts, dur, status in rows:
        per_bucket.setdefault((ts - start_ns) // width, []).append((dur, status))
    err = [(i, sum(1 for _, s in v if s >= 500) / len(v)) for i, v in sorted(per_bucket.items())]
    lat = [(i, _p95([d for d, _ in v])) for i, v in sorted(per_bucket.items())]
    depth: dict[int, float] = {}
    for ts, value in conn.execute(
        "SELECT ts_ns, value FROM metric_points WHERE name = 'queue.depth'"
    ).fetchall():
        idx = (ts - start_ns) // width
        depth[idx] = max(depth.get(idx, 0.0), value)
    restarts = conn.execute(
        "SELECT ts_ns, body FROM logs WHERE service = 'platform'"
        " AND body LIKE 'container % exited%'"
        " ORDER BY ts_ns LIMIT 1"
    ).fetchone()

    alerts: list[Alert] = []
    hit = _sustained(err, ERROR_RATE)
    if hit:
        alerts.append(
            Alert(
                "CheckoutErrorRate",
                "gateway",
                f"gateway POST /checkout 5xx rate above {ERROR_RATE:.0%} for 15s"
                f" (peak {hit[2]:.1%})",
                start_ns + hit[0] * width,
                start_ns + (hit[1] + 1) * width,
                round(hit[2], 4),
            )
        )
    hit = _sustained(lat, P95_MS)
    if hit:
        alerts.append(
            Alert(
                "CheckoutLatencyP95",
                "gateway",
                f"gateway POST /checkout p95 latency above {P95_MS:.0f}ms for 15s"
                f" (peak {hit[2]:.0f}ms)",
                start_ns + hit[0] * width,
                start_ns + (hit[1] + 1) * width,
                round(hit[2], 1),
            )
        )
    hit = _sustained(sorted(depth.items()), BACKLOG)
    if hit:
        alerts.append(
            Alert(
                "FulfilmentBacklog",
                "worker",
                f"fulfilment queue depth above {BACKLOG} for 15s (peak {hit[2]:.0f})",
                start_ns + hit[0] * width,
                start_ns + (hit[1] + 1) * width,
                hit[2],
            )
        )
    if restarts:
        alerts.append(
            Alert(
                "UnplannedRestart",
                restarts[1].split()[1],
                f"unplanned restart of container {restarts[1].split()[1]}",
                restarts[0],
                restarts[0],
                1.0,
            )
        )
    return sorted(alerts, key=lambda a: a.fired_ns)
