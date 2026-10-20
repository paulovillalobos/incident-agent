"""Execute a scenario against the live system and keep a telemetry snapshot.

Incident mode: start the healthy system, drive load, apply the scenario's events on
schedule (recording changes with their diffs), then store everything the agent may see
(``telemetry.db`` and the post-incident ``system/`` tree) next to ``ground_truth.json``.

Verify mode: start from the post-incident tree with a proposed fix applied, replay only
the unrecorded faults, and measure whether the service level recovers.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from incident_agent import patching
from incident_agent.runner.loadgen import LoadGenerator, LoadResult
from incident_agent.runner.platform_log import PlatformLog
from incident_agent.runner.supervisor import Supervisor, allocate_ports
from incident_agent.scenarios.model import SYSTEM_TREE, Event, Scenario
from incident_agent.telemetry import store

DEFAULT_BASE_PORT = 47100
DEFAULT_OTLP_PORT = 47318


@dataclass
class ChangeRecord:
    change_id: str
    ts_ns: int
    service: str
    kind: str
    author: str
    title: str
    version_from: str | None
    version_to: str | None
    diff: str


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".data", "*.pyc"))


def change_id(scenario: str, replicate: int, index: int) -> str:
    return "chg-" + hashlib.sha1(f"{scenario}:{replicate}:{index}".encode()).hexdigest()[:8]


def _version(tree: Path, service: str) -> str | None:
    import tomllib

    path = tree / "config" / f"{service.split('-')[0]}.toml"
    if not path.exists():
        return None
    return str(tomllib.loads(path.read_text()).get("version"))


class _Receiver:
    def __init__(self, db: Path, port: int, log_dir: Path) -> None:
        self.port = port
        self.log = (log_dir / "receiver.log").open("ab")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "incident_agent.telemetry.receiver",
                "--db",
                str(db),
                "--port",
                str(port),
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/status", timeout=0.5)
                return
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("telemetry receiver did not start")

    def drain_and_stop(self, quiet_s: float = 1.5, timeout_s: float = 30) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = httpx.get(f"http://127.0.0.1:{self.port}/status", timeout=2).json()
            if status["queued"] == 0 and status["idle_s"] >= quiet_s:
                break
            time.sleep(0.3)
        self.proc.terminate()
        self.proc.wait(timeout=30)
        self.log.close()
        return status


def _run_action(event: Event, ports: dict[str, int]) -> None:
    if event.action == "provider_latency":
        target = f"provider-{event.params['region']}"
        httpx.post(
            f"http://127.0.0.1:{ports[target]}/admin/latency",
            json={"median_ms": event.params["median_ms"], "sigma": event.params.get("sigma")},
            timeout=5,
        )
    elif event.action == "audit_export":
        httpx.post(
            f"http://127.0.0.1:{ports[event.params['instance']]}/admin/audit-export",
            json={"megabytes": event.params["megabytes"]},
            timeout=60,
        )
    else:
        raise ValueError(f"unknown action {event.action}")


def window_stats(result: LoadResult, lo: float, hi: float) -> dict[str, Any]:
    """Client-side error rate and p95 latency for requests started in [lo, hi)."""
    rows = [s for s in result.samples if lo <= s.ts < hi]
    if not rows:
        return {"requests": 0, "error_rate": 1.0, "p95_ms": 0.0}
    latencies = sorted(s.latency_ms for s in rows)
    errors = sum(1 for s in rows if s.status == 0 or s.status >= 500)
    return {
        "requests": len(rows),
        "error_rate": round(errors / len(rows), 4),
        "p95_ms": round(latencies[int(0.95 * (len(latencies) - 1))], 1),
    }


def summarize_load(result: LoadResult, start: float, bucket_s: float = 5.0) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[int, float]]] = {}
    for s in result.samples:
        buckets.setdefault(int((s.ts - start) // bucket_s), []).append((s.status, s.latency_ms))
    out = []
    for idx in sorted(buckets):
        rows = buckets[idx]
        latencies = sorted(lat for _, lat in rows)
        errors = sum(1 for status, _ in rows if status == 0 or status >= 500)
        out.append(
            {
                "t_s": idx * bucket_s,
                "requests": len(rows),
                "errors": errors,
                "error_rate": round(errors / len(rows), 4),
                "p95_ms": round(latencies[int(0.95 * (len(latencies) - 1))], 1),
            }
        )
    return out


def run(
    scenario: Scenario,
    out_dir: Path,
    *,
    replicate: int = 1,
    mode: str = "incident",
    fix_diff: str | None = None,
    duration_s: float | None = None,
    base_port: int = DEFAULT_BASE_PORT,
    otlp_port: int = DEFAULT_OTLP_PORT,
    keep_logs: bool = True,
    slo_from_s: float | None = None,
) -> dict[str, Any]:
    """Run ``scenario`` once and write the snapshot into ``out_dir`` (recreated)."""
    out_dir = out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    tree = out_dir / "system"
    copy_tree(SYSTEM_TREE, tree)
    log_dir = out_dir / "logs"
    log_dir.mkdir()
    data_dir = Path(tempfile.mkdtemp(prefix=f"ia-{scenario.name}-"))
    db_path = out_dir / "telemetry.db"
    duration = duration_s or scenario.duration_s

    if mode == "verify":
        for event in scenario.events:
            if event.recorded:
                patching.apply_diff(tree, patching.make_diff(tree, list(event.edits)))
        if fix_diff:
            patching.apply_diff(tree, fix_diff)

    ports = allocate_ports(base_port)
    endpoint = f"http://127.0.0.1:{otlp_port}"
    receiver = _Receiver(db_path, otlp_port, log_dir)
    platform = PlatformLog(endpoint)
    supervisor = Supervisor(tree, data_dir, log_dir, ports, endpoint, platform)
    changes: list[ChangeRecord] = []
    timeline: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        supervisor.start_all()
        start = time.time()
        load = LoadGenerator(
            f"http://127.0.0.1:{ports['gateway']}/checkout", scenario.rate, seed=replicate
        )
        load.start()

        def schedule() -> None:
            for index, event in enumerate(scenario.events):
                if mode == "verify" and event.recorded:
                    continue
                delay = start + event.at_s - time.time()
                if delay > 0:
                    time.sleep(delay)
                try:
                    _apply_event(index, event)
                except Exception as exc:  # recorded and surfaced after the run
                    errors.append(f"event {index} ({event.title}): {exc}")

        def _apply_event(index: int, event: Event) -> None:
            ts = time.time_ns()
            entry: dict[str, Any] = {
                "t_s": round(ts / 1e9 - start, 2),
                "title": event.title,
                "kind": event.kind,
            }
            if event.recorded:
                before = _version(tree, event.service)
                diff = patching.make_diff(tree, list(event.edits))
                patching.apply_diff(tree, diff)
                cid = change_id(scenario.name, replicate, index)
                entry["change_id"] = cid
                changes.append(
                    ChangeRecord(
                        cid,
                        ts,
                        event.service,
                        event.kind,
                        event.author,
                        event.title,
                        before,
                        _version(tree, event.service),
                        diff,
                    )
                )
                for name in event.restart:
                    platform.emit("platform", f"rollout of {name} started (change {cid})")
                    supervisor.restart(name, f"change {cid}")
            else:
                _run_action(event, ports)
            timeline.append(entry)

        scheduler = threading.Thread(target=schedule, daemon=True)
        scheduler.start()
        time.sleep(max(0.0, start + duration - time.time()))
        result = load.stop()
        scheduler.join(timeout=30)
        end = time.time()
    finally:
        supervisor.stop_all()
        status = receiver.drain_and_stop()
        shutil.rmtree(data_dir, ignore_errors=True)

    conn = store.open_writable(db_path)
    with conn:
        conn.executemany(
            "INSERT INTO changes VALUES (?,?,?,?,?,?,?,?,?)",
            [tuple(asdict(c).values()) for c in changes],
        )
        meta = {
            "scenario": scenario.name,
            "replicate": str(replicate),
            "mode": mode,
            "start_ns": str(int(start * 1e9)),
            "end_ns": str(int(end * 1e9)),
        }
        conn.executemany("INSERT INTO run_meta VALUES (?, ?)", list(meta.items()))
    conn.close()
    store.checkpoint(db_path)

    summary: dict[str, Any] = {
        "scenario": scenario.name,
        "replicate": replicate,
        "mode": mode,
        "start_ns": int(start * 1e9),
        "end_ns": int(end * 1e9),
        "timeline": timeline,
        "event_errors": errors,
        "restarts": [
            {"t_s": round(t - start, 2), "service": n, "code": c} for t, n, c in supervisor.restarts
        ],
        "load": summarize_load(result, start),
        "dropped": result.dropped,
        "telemetry": status,
    }
    if slo_from_s is not None:
        summary["slo_window"] = window_stats(result, start + slo_from_s, end - 1)
        summary["slo_window"]["from_s"] = slo_from_s
    if mode == "incident":
        culprit_ids = [
            change_id(scenario.name, replicate, i)
            for i, e in enumerate(scenario.events)
            if e.culprit and e.recorded
        ]
        gt = scenario.ground_truth
        (out_dir / "ground_truth.json").write_text(
            json.dumps(
                {
                    "scenario": scenario.name,
                    "service": gt.service,
                    "accept_services": list(gt.accept_services),
                    "category": gt.category,
                    "change_ids": culprit_ids if gt.change == "culprit" else [],
                    "explanation": gt.explanation,
                },
                indent=2,
            )
        )
    if not keep_logs:
        shutil.rmtree(log_dir, ignore_errors=True)
    (out_dir / "run.json").write_text(json.dumps(summary, indent=2))
    return summary
