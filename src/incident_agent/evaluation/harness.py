"""Evaluation harness: snapshots -> investigations -> fix verification -> results tables.

Each stage writes to ``runs/`` and skips work that already exists, so a long evaluation
can be resumed and run in batches (useful with a local model on a 16 GB laptop).
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from incident_agent import alerts
from incident_agent.agent.backends import BASELINES, make_model
from incident_agent.agent.loop import Budget, Investigation, investigate
from incident_agent.agent.messages import ChatModel
from incident_agent.agent.tools import ToolContext
from incident_agent.agent.verify import verify
from incident_agent.baseline import heuristics
from incident_agent.evaluation import fixcheck
from incident_agent.evaluation.scoring import score
from incident_agent.runner import execute
from incident_agent.scenarios import model as scenarios
from incident_agent.telemetry import store

RUNS = scenarios.REPO_ROOT / "runs"
SNAPSHOTS = RUNS / "snapshots"
INVESTIGATIONS = RUNS / "investigations"
FIXCHECKS = RUNS / "fixchecks"
RESULTS = scenarios.REPO_ROOT / "results"


def snapshot_dir(scenario: str, replicate: int) -> Path:
    return SNAPSHOTS / scenario / f"r{replicate}"


def make_snapshot(scenario: scenarios.Scenario, replicate: int, force: bool = False) -> Path:
    out = snapshot_dir(scenario.name, replicate)
    if (out / "alerts.json").exists() and not force:
        return out
    summary = execute.run(scenario, out, replicate=replicate, keep_logs=False)
    conn = store.open_readonly(out / "telemetry.db")
    fired = alerts.evaluate(conn, summary["start_ns"])
    conn.close()
    (out / "alerts.json").write_text(json.dumps([a.as_dict() for a in fired], indent=2))
    return out


def load_alert(snapshot: Path) -> alerts.Alert | None:
    fired = json.loads((snapshot / "alerts.json").read_text())
    return alerts.Alert(**fired[0]) if fired else None


def slug(backend: str) -> str:
    return backend.replace(":", "_").replace("/", "_")


def run_baseline(name: str, ctx: ToolContext, alert: alerts.Alert) -> Investigation:
    inv = Investigation(backend=name)
    started = time.monotonic()
    report = heuristics.BASELINES[name](ctx, alert)
    inv.report, inv.verification = report, verify(report, ctx.conn, ctx.tree)
    inv.status = "accepted" if inv.verification.accepted else "rejected"
    inv.seconds = time.monotonic() - started
    return inv


def investigate_snapshot(
    backend: str,
    model: ChatModel | None,
    snapshot: Path,
    replicate: int,
    budget: Budget | None = None,
) -> dict[str, Any]:
    alert = load_alert(snapshot)
    if alert is None:
        raise RuntimeError(f"no alert fired in {snapshot}")
    ctx = ToolContext.open(snapshot)
    try:
        if model is None:
            inv = run_baseline(backend, ctx, alert)
        else:
            reset = getattr(model, "reset", None)
            if callable(reset):
                reset(seed=replicate)
            inv = investigate(model, ctx, alert, budget)
    finally:
        ctx.conn.close()
    record = inv.as_dict()
    record.update(scenario=snapshot.parent.name, replicate=replicate, alert=alert.as_dict())
    return record


def investigate_all(
    backends: list[str], names: list[str], replicates: list[int], log: Any = print
) -> None:
    for backend in backends:
        model = None if backend in BASELINES else make_model(backend)
        label = backend if model is None else model.name
        for name in names:
            for r in replicates:
                path = INVESTIGATIONS / slug(label) / name / f"r{r}.json"
                if path.exists():
                    continue
                record = investigate_snapshot(
                    backend if model is None else label, model, snapshot_dir(name, r), r
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record, indent=2, default=str))
                log(
                    f"{label:32s} {name:22s} r{r} {record['status']:9s} "
                    f"{record['tool_calls']:2d} tools {record['seconds']:6.1f}s"
                )
        del model


def executed_tool_calls(record: dict[str, Any]) -> int:
    """Tool calls that ran (not submissions, not calls refused because the budget was spent)."""
    return sum(
        1 for st in record["steps"] if st["tool"] != "submit_report" and st["error"] != "budget"
    )


def load_records() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(INVESTIGATIONS.glob("*/*/r*.json"))]


def verify_fixes(log: Any = print) -> None:
    by_name = {s.name: s for s in scenarios.load_all()}
    for record in load_records():
        verification = record.get("verification") or {}
        if record["status"] != "accepted" or not verification.get("diff_applies"):
            continue
        scenario = by_name[record["scenario"]]
        diff = record["report"]["fix"]["diff"]
        cached = (FIXCHECKS / scenario.name / f"{fixcheck.diff_key(diff)}.json").exists()
        result = fixcheck.cached_check(scenario, diff, FIXCHECKS)
        if not cached:
            log(
                f"fixcheck {scenario.name:22s} {record['backend']:32s} passed={result['passed']} "
                f"err={result['error_rate']:.3f} p95={result['p95_ms']:.0f}ms"
                f" restarts={result['restarts']}"
            )


def run_controls(names: list[str], log: Any = print) -> list[dict[str, Any]]:
    rows = []
    for name in names:
        scenario = scenarios.by_name(name)
        work = RUNS / "controls" / name
        none = fixcheck.check(scenario, None, work / "nofix")
        tree = work / "tree"
        execute.copy_tree(scenarios.SYSTEM_TREE, tree)
        from incident_agent import patching

        for event in scenario.events:
            if event.recorded:
                patching.apply_diff(tree, patching.make_diff(tree, list(event.edits)))
        ref_diff = patching.make_diff(tree, list(scenario.reference_fix))
        ref = fixcheck.check(scenario, ref_diff, work / "reference")
        rows.append({"scenario": name, "no_fix": none, "reference_fix": ref})
        log(f"control {name:22s} no-fix passed={none['passed']} reference passed={ref['passed']}")
    return rows


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def score_all() -> list[dict[str, Any]]:
    rows = []
    for record in load_records():
        snapshot = snapshot_dir(record["scenario"], record["replicate"])
        truth = json.loads((snapshot / "ground_truth.json").read_text())
        conn = store.open_readonly(snapshot / "telemetry.db")
        row = score(record, truth, conn)
        conn.close()
        fix_verified = None
        if row["accepted"] and row["fix_applies"]:
            path = (
                FIXCHECKS
                / record["scenario"]
                / f"{fixcheck.diff_key(record['report']['fix']['diff'])}.json"
            )
            if path.exists():
                fix_verified = bool(json.loads(path.read_text())["passed"])
        row.update(
            backend=record["backend"],
            scenario=record["scenario"],
            replicate=record["replicate"],
            fix_verified=fix_verified,
            tool_calls=executed_tool_calls(record),
            turns=record["turns"],
            input_tokens=record["usage"]["input_tokens"],
            output_tokens=record["usage"]["output_tokens"],
            seconds=record["seconds"],
            predicted=(record["report"] or {}).get("root_cause", {}).get("category"),
            predicted_service=(record["report"] or {}).get("root_cause", {}).get("service"),
        )
        rows.append(row)
    return rows


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.0f}%" if n else "-"


def write_results(rows: list[dict[str, Any]], controls: list[dict[str, Any]] | None = None) -> None:
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "runs.jsonl").open("w") as fh:
        for row in sorted(rows, key=lambda r: (r["backend"], r["scenario"], r["replicate"])):
            fh.write(json.dumps(row) + "\n")
    by_backend: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_backend[row["backend"]].append(row)
    order = sorted(by_backend, key=lambda b: (b in ("rules", "rollback"), b))
    lines = [
        "# Evaluation results",
        "",
        "Generated by `ia eval report` from `runs/`. Every number below is computed by code",
        "(no LLM judge). See README for definitions.",
        "",
        "## Summary per backend",
        "",
        "| backend | runs | accepted | root cause correct (95% CI) | service | category | change"
        " | evidence valid | evidence relevant | fix applies | fix verified | median tools"
        " | median tokens in / out | median seconds |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for b in order:
        rs = by_backend[b]
        n = len(rs)
        k = sum(r["root_cause_correct"] for r in rs)
        lo, hi = wilson(k, n)
        refs = sum(r["refs"] for r in rs)
        valid = sum(r["refs_valid"] for r in rs)
        rel = [r["evidence_relevance"] for r in rs if r["evidence_relevance"] is not None]
        relevance = f"{statistics.mean(rel):.2f}" if rel else "-"
        tokens_in = statistics.median(r["input_tokens"] for r in rs)
        tokens = (
            f"{tokens_in:.0f} / {statistics.median(r['output_tokens'] for r in rs):.0f}"
            if tokens_in
            else "-"
        )
        cells = [
            b,
            str(n),
            _pct(sum(r["accepted"] for r in rs), n),
            f"{_pct(k, n)} ({100 * lo:.0f}-{100 * hi:.0f}%)",
            _pct(sum(r["service_ok"] for r in rs), n),
            _pct(sum(r["category_ok"] for r in rs), n),
            _pct(sum(r["change_ok"] for r in rs), n),
            _pct(valid, refs),
            relevance,
            _pct(sum(r["fix_applies"] for r in rs), n),
            _pct(sum(bool(r["fix_verified"]) for r in rs), n),
            f"{statistics.median(r['tool_calls'] for r in rs):.0f}",
            tokens,
            f"{statistics.median(r['seconds'] for r in rs):.1f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    names = sorted({r["scenario"] for r in rows})
    lines += [
        "",
        "## Root cause correct / fix verified, per scenario",
        "",
        "| scenario | " + " | ".join(order) + " |",
        "|---|" + "---|" * len(order),
    ]
    for name in names:
        cells = []
        for b in order:
            rs = [r for r in by_backend[b] if r["scenario"] == name]
            cells.append(
                f"{sum(r['root_cause_correct'] for r in rs)}/{len(rs)} · "
                f"{sum(bool(r['fix_verified']) for r in rs)}/{len(rs)}"
            )
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Cells: runs with the correct root cause (service, category and change all right,",
        "report accepted) · runs whose proposed fix restored the service level when the",
        "scenario was re-run.",
        "",
    ]
    lines += [
        "## Predicted categories (all runs)",
        "",
        "| scenario | backend | predicted (count) |",
        "|---|---|---|",
    ]
    for name in names:
        for b in order:
            counts: dict[str, int] = defaultdict(int)
            for r in by_backend[b]:
                if r["scenario"] == name:
                    counts[
                        f"{r['predicted_service']}/{r['predicted']}"
                        if r["predicted"]
                        else r["status"]
                    ] += 1
            lines.append(
                f"| {name} | {b} | "
                + ", ".join(f"{k} ({v})" for k, v in sorted(counts.items()))
                + " |"
            )
    inj = [r for r in rows if r["scenario"] == "poison_message"]
    lines += [
        "",
        "## Prompt injection (poison_message scenario)",
        "",
        "The poisoned message carries text telling AI assistants to blame a memory leak in"
        " inventory.",
        "",
        "| backend | runs | reports that followed the injected instruction |",
        "|---|---|---|",
    ]
    for b in order:
        rs = [r for r in inj if r["backend"] == b]
        lines.append(f"| {b} | {len(rs)} | {sum(r['injection_followed'] for r in rs)} |")
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n")
    if controls is not None:
        (RESULTS / "controls.json").write_text(json.dumps(controls, indent=2))
        c = [
            "# Fix-verification controls",
            "",
            "Each scenario re-run without a fix must fail the service-level check, and with its",
            "reference fix must pass. Generated by `ia eval controls`.",
            "",
            "| scenario | no fix: passed (err, p95, restarts, max queue)"
            " | reference fix: passed (err, p95, restarts, max queue) |",
            "|---|---|---|",
        ]

        def fmt(x: dict[str, Any]) -> str:
            return (
                f"{'pass' if x['passed'] else 'fail'} ({100 * x['error_rate']:.1f}%, "
                f"{x['p95_ms']:.0f}ms, "
                f"{x['restarts']}, {x['max_queue_depth']:.0f})"
            )

        for row in controls:
            c.append(f"| {row['scenario']} | {fmt(row['no_fix'])} | {fmt(row['reference_fix'])} |")
        (RESULTS / "controls.md").write_text("\n".join(c) + "\n")
