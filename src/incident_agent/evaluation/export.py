"""Export investigations as self-contained JSON for the React UI.

Each file holds the investigation record (tool calls, report, verification), its score,
the ground truth, every cited piece of evidence resolved from the telemetry store, and
the full span tree of the cited trace for the waterfall. The UI needs no backend.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from incident_agent.evaluation import harness
from incident_agent.telemetry import store

_REPO_ROOT = str(Path(__file__).resolve().parents[3])


def _redact(text: str) -> str:
    """Replace absolute local paths (stack traces in recorded logs) with repo-relative ones."""
    return text.replace(_REPO_ROOT, ".").replace(str(Path.home()), "~")


MAX_TRACE_SPANS = 250


def _span(conn: sqlite3.Connection, span_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT span_id, trace_id, parent_span_id, service, instance, version, name, kind,"
        " start_ns, duration_ms, status, status_message, http_status FROM spans"
        " WHERE span_id = ?",
        (span_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "span_id",
        "trace_id",
        "parent_span_id",
        "service",
        "instance",
        "version",
        "name",
        "kind",
        "start_ns",
        "duration_ms",
        "status",
        "status_message",
        "http_status",
    )
    return dict(zip(keys, row, strict=True))


def resolve(ref: str, conn: sqlite3.Connection, tree: Path) -> dict[str, Any]:
    """Look up one evidence reference; ``found`` is False for ids that do not exist."""
    kind, _, value = ref.partition(":")
    out: dict[str, Any] = {"ref": ref, "kind": kind, "found": False}
    if kind == "span":
        span = _span(conn, value)
        if span:
            out.update(found=True, span=span)
    elif kind == "trace":
        n = conn.execute("SELECT COUNT(*) FROM spans WHERE trace_id = ?", (value,)).fetchone()[0]
        out.update(found=n > 0, span_count=n)
    elif kind == "log" and value.isdigit():
        row = conn.execute(
            "SELECT log_id, ts_ns, service, instance, severity, body, trace_id, span_id FROM logs"
            " WHERE log_id = ?",
            (int(value),),
        ).fetchone()
        if row:
            keys = (
                "log_id",
                "ts_ns",
                "service",
                "instance",
                "severity",
                "body",
                "trace_id",
                "span_id",
            )
            out.update(found=True, log=dict(zip(keys, row, strict=True)))
    elif kind == "change":
        row = conn.execute(
            "SELECT change_id, ts_ns, service, kind, author, title, diff FROM changes"
            " WHERE change_id = ?",
            (value.removeprefix("change:"),),
        ).fetchone()
        if row:
            change_keys = ("change_id", "ts_ns", "service", "kind", "author", "title", "diff")
            out.update(found=True, change=dict(zip(change_keys, row, strict=True)))
    elif kind == "metric":
        name = value.split("@")[0]
        n = conn.execute("SELECT COUNT(*) FROM metric_points WHERE name = ?", (name,)).fetchone()[0]
        out.update(found=n > 0, points=n)
    elif kind == "file":
        path = value.split(":")[0].removeprefix("./")
        target = tree / path
        if ".." not in Path(path).parts and target.is_file():
            out.update(found=True, lines=len(target.read_text().splitlines()))
    return out


def trace_for(report: dict[str, Any], conn: sqlite3.Connection) -> str | None:
    """The trace the waterfall shows: the first cited trace, else the first cited span's."""
    refs = list(report["root_cause"]["evidence"])
    for entry in report["timeline"]:
        refs += entry["evidence"]
    for ref in refs:
        if ref.startswith("trace:"):
            return str(ref.removeprefix("trace:"))
    for ref in refs:
        if ref.startswith("span:"):
            span = _span(conn, ref.removeprefix("span:"))
            if span:
                return str(span["trace_id"])
    return None


def waterfall(trace_id: str, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT span_id FROM spans WHERE trace_id = ? ORDER BY start_ns LIMIT ?",
        (trace_id, MAX_TRACE_SPANS),
    ).fetchall()
    return [s for (sid,) in rows if (s := _span(conn, sid))]


def export(out_dir: Path, replicate: int | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scores = {(r["backend"], r["scenario"], r["replicate"]): r for r in harness.score_all()}
    index: list[dict[str, Any]] = []
    written: list[Path] = []
    for record in harness.load_records():
        if replicate is not None and record["replicate"] != replicate:
            continue
        snapshot = harness.snapshot_dir(record["scenario"], record["replicate"])
        conn = store.open_readonly(snapshot / "telemetry.db")
        record["tool_calls"] = harness.executed_tool_calls(record)
        report = record["report"]
        meta = dict(conn.execute("SELECT key, value FROM run_meta").fetchall())
        evidence = (
            [resolve(ref, conn, snapshot / "system") for ref in dict.fromkeys(_refs(report))]
            if report
            else []
        )
        trace_id = trace_for(report, conn) if report else None
        changes = [
            dict(
                zip(("change_id", "ts_ns", "service", "kind", "author", "title"), row, strict=True)
            )
            for row in conn.execute(
                "SELECT change_id, ts_ns, service, kind, author, title FROM changes ORDER BY ts_ns"
            )
        ]
        doc = {
            "id": "--".join(
                (harness.slug(record["backend"]), record["scenario"], f"r{record['replicate']}")
            ),
            "record": record,
            "score": scores.get((record["backend"], record["scenario"], record["replicate"])),
            "ground_truth": json.loads((snapshot / "ground_truth.json").read_text()),
            "run": {"start_ns": int(meta["start_ns"]), "end_ns": int(meta["end_ns"])},
            "changes": changes,
            "evidence": evidence,
            "trace": {"trace_id": trace_id, "spans": waterfall(trace_id, conn) if trace_id else []},
        }
        conn.close()
        path = out_dir / f"{doc['id']}.json"
        path.write_text(_redact(json.dumps(doc, default=str)))
        written.append(path)
        score = doc["score"] or {}
        index.append(
            {
                "id": doc["id"],
                "backend": record["backend"],
                "scenario": record["scenario"],
                "replicate": record["replicate"],
                "status": record["status"],
                "root_cause_correct": bool(score.get("root_cause_correct")),
                "fix_verified": score.get("fix_verified"),
                "tool_calls": harness.executed_tool_calls(record),
                "seconds": record["seconds"],
            }
        )
    index.sort(key=lambda r: (r["scenario"], r["backend"], r["replicate"]))
    (out_dir / "index.json").write_text(json.dumps(index, indent=1))
    return written


def _refs(report: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for entry in report["timeline"]:
        refs += entry["evidence"]
    for hyp in report["hypotheses"]:
        refs += hyp["evidence"]
    refs += report["root_cause"]["evidence"]
    refs += report["blast_radius"]["evidence"]
    return refs
