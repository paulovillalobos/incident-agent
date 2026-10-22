"""Fix verification thresholds and caching; evidence resolution for the UI export."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import CULPRIT, SEC, S, Snapshot, make_report
from incident_agent.evaluation import export, fixcheck
from incident_agent.patching import Edit
from incident_agent.scenarios.model import Event, GroundTruth, Scenario
from incident_agent.telemetry import store


def scenario(*events: Event) -> Scenario:
    truth = GroundTruth("orders", "config", "pool shrunk", ("orders",), "culprit")
    return Scenario("synthetic", "synthetic", 60.0, 20.0, events, truth, (), Path("."))


DEPLOY = Event(5.0, "deploy", "orders", "1.7.4", edits=(Edit("a.toml", "x", "y"),))
OUTAGE = Event(12.0, "provider", "payments", "provider degraded")
EXPORT = Event(18.0, "job", "orders", "audit export")


def test_fault_start_is_the_last_unrecorded_event() -> None:
    assert fixcheck.fault_start(scenario(DEPLOY, OUTAGE, EXPORT)) == 18.0
    assert fixcheck.fault_start(scenario(DEPLOY)) == 0.0


def test_diff_key_ignores_surrounding_whitespace() -> None:
    key = fixcheck.diff_key("--- a/x\n+++ b/x\n")
    assert key == fixcheck.diff_key("\n--- a/x\n+++ b/x\n\n")
    assert len(key) == 16
    assert key != fixcheck.diff_key("--- a/y\n+++ b/y\n")


GOOD: dict[str, Any] = {"error_rate": 0.02, "p95_ms": 400.0, "requests": 900}


def fake_run(
    calls: list[dict[str, Any]],
    slo: dict[str, Any],
    depths: list[tuple[float, float]],
    restarts: int = 0,
    event_errors: tuple[str, ...] = (),
) -> Any:
    """Stand in for execute.run: record the call and write a telemetry store."""

    def run(_: Scenario, out: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        conn = store.open_writable(out / "telemetry.db")
        conn.executemany(
            "INSERT INTO metric_points (ts_ns, service, instance, name, kind, value, count, sum,"
            " attributes) VALUES (?, 'orders', 'orders', 'queue.depth', 'gauge', ?, NULL, NULL,"
            " '{}')",
            [(S + int(at_s * SEC), value) for at_s, value in depths],
        )
        conn.commit()
        conn.close()
        return {
            "start_ns": S,
            "slo_window": slo,
            "restarts": ["orders"] * restarts,
            "event_errors": list(event_errors),
        }

    return run


@pytest.mark.parametrize(
    ("slo", "depths", "restarts", "event_errors", "passed"),
    [
        (GOOD, [(30.0, 50.0)], 0, (), True),  # every limit met exactly
        (GOOD, [(10.0, 400.0), (30.0, 5.0)], 0, (), True),  # backlog before the window
        ({**GOOD, "error_rate": 0.021}, [], 0, (), False),
        ({**GOOD, "p95_ms": 400.5}, [], 0, (), False),
        (GOOD, [], 1, (), False),
        (GOOD, [(30.0, 51.0)], 0, (), False),
        (GOOD, [], 0, ("audit export did not start",), False),
    ],
)
def test_fix_passes_only_if_every_service_level_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slo: dict[str, Any],
    depths: list[tuple[float, float]],
    restarts: int,
    event_errors: tuple[str, ...],
    passed: bool,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        fixcheck.execute, "run", fake_run(calls, slo, depths, restarts, event_errors)
    )
    result = fixcheck.check(scenario(DEPLOY, OUTAGE, EXPORT), "a diff", tmp_path)

    assert result["passed"] is passed
    # The window opens 4 s after the last unrecorded fault, and runs at least 16 s.
    assert calls[0]["slo_from_s"] == result["window_from_s"] == 22.0
    assert calls[0]["duration_s"] == result["duration_s"] == 38.0
    assert calls[0]["mode"] == "verify" and calls[0]["fix_diff"] == "a diff"
    assert not (tmp_path / "run").exists()


def test_cached_check_verifies_each_diff_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[str | None] = []

    def check(_: Scenario, diff: str | None, work_dir: Path, **__: Any) -> dict[str, Any]:
        runs.append(diff)
        work_dir.mkdir(parents=True)
        return {"passed": True}

    monkeypatch.setattr(fixcheck, "check", check)
    s = scenario(DEPLOY, OUTAGE)
    first = fixcheck.cached_check(s, "fix one\n", tmp_path)
    again = fixcheck.cached_check(s, "fix one", tmp_path)
    fixcheck.cached_check(s, "fix two", tmp_path)

    assert runs == ["fix one\n", "fix two"]
    assert first == again == {"passed": True, "diff": "fix one\n"}
    stored = tmp_path / "synthetic" / f"{fixcheck.diff_key('fix one')}.json"
    assert json.loads(stored.read_text()) == first
    assert not list((tmp_path / "synthetic").glob("work-*"))


def test_cited_evidence_is_resolved(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    refs = {
        "span": f"span:{snapshot.error_span}",
        "trace": f"trace:{snapshot.error_trace}",
        "log": f"log:{snapshot.error_log}",
        "change": f"change:{CULPRIT}",
        "metric": "metric:db.pool.timeouts@orders",
        "file": "file:./config/orders.toml:3",
    }
    out = {kind: export.resolve(ref, ro, snapshot.tree) for kind, ref in refs.items()}

    assert all(r["found"] for r in out.values()), out
    assert out["span"]["span"]["trace_id"] == snapshot.error_trace
    assert out["trace"]["span_count"] > 1
    assert out["change"]["change"]["change_id"] == CULPRIT
    assert out["metric"]["points"] > 0
    assert out["file"]["lines"] == len(
        (snapshot.tree / "config/orders.toml").read_text().splitlines()
    )


@pytest.mark.parametrize(
    "ref",
    [
        "span:ffffffffffffffff",
        "trace:" + "0" * 32,
        "log:999999",
        "log:latest",
        "change:chg-nothere",
        "metric:cpu.magic",
        "file:config/nope.toml",
        "file:../telemetry.db",
        "pod:orders-1",
    ],
)
def test_missing_or_unsafe_evidence_is_not_found(
    snapshot: Snapshot, ro: sqlite3.Connection, ref: str
) -> None:
    resolved = export.resolve(ref, ro, snapshot.tree)
    assert resolved["found"] is False
    assert resolved["ref"] == ref


def test_waterfall_trace_is_the_first_cited_trace_else_a_cited_span(
    snapshot: Snapshot, ro: sqlite3.Connection
) -> None:
    report = make_report(snapshot)
    assert export.trace_for(report, ro) == snapshot.error_trace

    for entry in report["timeline"]:
        entry["evidence"] = [e for e in entry["evidence"] if not e.startswith("trace:")]
    assert export.trace_for(report, ro) == snapshot.error_trace  # from the cited span

    report["root_cause"]["evidence"] = [f"change:{CULPRIT}"]
    assert export.trace_for(report, ro) is None


def test_export_redacts_local_paths() -> None:
    home = str(Path.home())
    text = f"File {export._REPO_ROOT}/system/orders.py, line 3; config in {home}/.config"
    assert export._redact(text) == "File ./system/orders.py, line 3; config in ~/.config"
