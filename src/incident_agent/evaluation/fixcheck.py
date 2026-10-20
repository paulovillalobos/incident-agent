"""Verify a proposed fix by re-running the scenario with the diff applied.

The system starts from the post-incident tree plus the fix, the unrecorded faults
(provider degradation, the audit export) are replayed at their original times, and the
fix passes if the service level holds over the measurement window:

* client-side 5xx/transport error rate <= 2%,
* client-side p95 latency <= 400 ms,
* no unplanned container restarts,
* fulfilment queue depth never above 50.

Controls (``ia eval controls``) check that every scenario fails without a fix and passes
with its reference fix, so a pass means something.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from incident_agent.runner import execute
from incident_agent.scenarios.model import Scenario
from incident_agent.telemetry import store

MAX_ERROR_RATE = 0.02
MAX_P95_MS = 400.0
MAX_QUEUE_DEPTH = 50
DURATION_S = 36.0


def fault_start(scenario: Scenario) -> float:
    unrecorded = [e.at_s for e in scenario.events if not e.recorded]
    return max(unrecorded, default=0.0)


def diff_key(diff: str) -> str:
    return hashlib.sha256(diff.strip().encode()).hexdigest()[:16]


def check(
    scenario: Scenario, diff: str | None, work_dir: Path, **run_kwargs: Any
) -> dict[str, Any]:
    """Run the verify-mode scenario and judge the service level. ``diff=None`` = no fix."""
    window_from = fault_start(scenario) + 4.0
    duration = max(DURATION_S, window_from + 16.0)
    out = work_dir / "run"
    summary = execute.run(
        scenario,
        out,
        mode="verify",
        fix_diff=diff,
        duration_s=duration,
        keep_logs=False,
        slo_from_s=window_from,
        **run_kwargs,
    )
    conn = store.open_readonly(out / "telemetry.db")
    start_ns = summary["start_ns"]
    depth = conn.execute(
        "SELECT COALESCE(MAX(value), 0) FROM metric_points"
        " WHERE name = 'queue.depth' AND ts_ns >= ?",
        (start_ns + int(window_from * 1e9),),
    ).fetchone()[0]
    conn.close()
    slo = summary["slo_window"]
    restarts = len(summary["restarts"])
    result = {
        "passed": slo["error_rate"] <= MAX_ERROR_RATE
        and slo["p95_ms"] <= MAX_P95_MS
        and restarts == 0
        and depth <= MAX_QUEUE_DEPTH
        and not summary["event_errors"],
        "error_rate": slo["error_rate"],
        "p95_ms": slo["p95_ms"],
        "requests": slo["requests"],
        "restarts": restarts,
        "max_queue_depth": depth,
        "window_from_s": window_from,
        "duration_s": duration,
        "event_errors": summary["event_errors"],
    }
    shutil.rmtree(out, ignore_errors=True)
    return result


def cached_check(scenario: Scenario, diff: str, cache_dir: Path) -> dict[str, Any]:
    """Check each distinct (scenario, diff) once; results are cached as JSON."""
    path = cache_dir / scenario.name / f"{diff_key(diff)}.json"
    if path.exists():
        cached: dict[str, Any] = json.loads(path.read_text())
        return cached
    path.parent.mkdir(parents=True, exist_ok=True)
    result = check(scenario, diff, path.parent / f"work-{diff_key(diff)}")
    result["diff"] = diff
    path.write_text(json.dumps(result, indent=2))
    shutil.rmtree(path.parent / f"work-{diff_key(diff)}", ignore_errors=True)
    return result
