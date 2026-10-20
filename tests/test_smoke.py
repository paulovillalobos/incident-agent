"""End-to-end: start the real system, inject a fault, record telemetry, investigate.

Run with ``pytest -m smoke`` (about a minute). Uses its own ports so it can run next to
an evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_agent import alerts
from incident_agent.agent.tools import ToolContext, run_tool
from incident_agent.agent.verify import verify
from incident_agent.baseline import heuristics
from incident_agent.runner import execute
from incident_agent.scenarios import model as scenarios
from incident_agent.telemetry import store

pytestmark = pytest.mark.smoke


def test_pool_exhaustion_end_to_end(tmp_path: Path) -> None:
    scenario = scenarios.by_name("pool_exhaustion")
    out = tmp_path / "snapshot"
    summary = execute.run(scenario, out, base_port=48100, otlp_port=48318, keep_logs=False)
    assert not summary["event_errors"]
    assert summary["telemetry"]["spans"] > 1000

    conn = store.open_readonly(out / "telemetry.db")
    fired = alerts.evaluate(conn, summary["start_ns"])
    conn.close()
    assert fired, "the fault must page"

    truth = json.loads((out / "ground_truth.json").read_text())
    ctx = ToolContext.open(out)
    try:
        timeouts = run_tool(ctx, "search_spans", {"service": "orders", "status": "error"})
        assert timeouts["matching"] > 0
        report = heuristics.rules(ctx, fired[0])
        result = verify(report, ctx.conn, ctx.tree)
    finally:
        ctx.conn.close()
    assert result.accepted, result.feedback()
    assert report.root_cause.service == truth["service"]
    assert report.root_cause.category == truth["category"]
    assert report.root_cause.change_id in truth["change_ids"]
