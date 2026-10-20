"""Non-LLM baselines that produce the same Report and go through the same verifier.

``rollback``: what a tired on-call engineer does first. Blame the most recent change
before the alert and roll it back.

``rules``: anomaly localisation plus a signature table. Errors are attributed to the
deepest failing span of each failing trace; latency to the span kind whose self-time per
request grew most (database, connection pool, uninstrumented downstream). A table of
error-message signatures picks the category, and the fix rolls back the latest change to
the localised service (or drains a failing replica). The signature table was written by
the same person who wrote the scenarios, so read its score as an optimistic reference
for hand-written rules, not as a strawman.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from incident_agent import patching
from incident_agent.agent.report import Report
from incident_agent.agent.tools import ToolContext
from incident_agent.alerts import Alert

NS = 1_000_000_000


@dataclass
class Change:
    change_id: str
    ts_ns: int
    service: str
    kind: str
    title: str
    diff: str


@dataclass
class Finding:
    service: str
    category: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    instance: str | None = None


def _changes(conn: sqlite3.Connection) -> list[Change]:
    return [
        Change(*row)
        for row in conn.execute(
            "SELECT change_id, ts_ns, service, kind, title, diff FROM changes ORDER BY ts_ns"
        )
    ]


def _service_of(change: Change) -> str:
    return change.service.split("-")[0]


def _latest(changes: list[Change], before_ns: int, service: str | None = None) -> Change | None:
    eligible = [
        c
        for c in changes
        if c.ts_ns <= before_ns and (service is None or _service_of(c) == service)
    ]
    return eligible[-1] if eligible else None


def _report(
    ctx: ToolContext,
    alert: Alert,
    finding: Finding,
    change: Change | None,
    diff: str,
    fix_summary: str,
    hypotheses: list[dict[str, Any]],
) -> Report:
    timeline = [
        {
            "time": ctx.ts(alert.started_ns),
            "event": f"{alert.rule} condition starts",
            "evidence": [],
        },
        {"time": ctx.ts(alert.fired_ns), "event": alert.summary, "evidence": []},
    ]
    evidence = list(finding.evidence)
    if change is not None:
        timeline.insert(
            0,
            {
                "time": ctx.ts(change.ts_ns),
                "event": change.title,
                "evidence": [f"change:{change.change_id}"],
            },
        )
        evidence.append(f"change:{change.change_id}")
    return Report.model_validate(
        {
            "summary": f"{finding.category} in {finding.service}: {finding.reason}",
            "timeline": sorted(timeline, key=lambda t: t["time"]),
            "hypotheses": hypotheses,
            "root_cause": {
                "service": finding.service,
                "category": finding.category,
                "change_id": change.change_id if change else None,
                "summary": finding.reason,
                "evidence": evidence or [f"metric:{'queue.depth'}"],
            },
            "blast_radius": {
                "services": blast_radius(ctx, alert),
                "impact": alert.summary,
                "evidence": [],
            },
            "fix": {"summary": fix_summary, "diff": diff},
        }
    )


def blast_radius(ctx: ToolContext, alert: Alert) -> list[str]:
    affected = []
    for (service,) in ctx.conn.execute("SELECT DISTINCT service FROM spans"):
        before = _error_rate(ctx.conn, service, ctx.start_ns, alert.started_ns)
        after = _error_rate(ctx.conn, service, alert.started_ns, ctx.end_ns + NS)
        if after - before > 0.01:
            affected.append(service)
    return sorted(set(affected) | {alert.service})


def _error_rate(conn: sqlite3.Connection, service: str, lo: int, hi: int) -> float:
    n, e = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(status = 'error'), 0) FROM spans"
        " WHERE service = ? AND kind = 'SERVER' AND start_ns >= ? AND start_ns < ?",
        (service, lo, hi),
    ).fetchone()
    return float(e) / n if n else 0.0


# ---------------------------------------------------------------- rollback baseline


def rollback(ctx: ToolContext, alert: Alert) -> Report:
    changes = _changes(ctx.conn)
    change = _latest(changes, alert.fired_ns)
    kind_to_category = {"flag": "bad_feature_flag", "migration": "slow_query"}
    if change is None:
        finding = Finding(
            alert.service, "other", "no change before the alert; nothing to roll back"
        )
        diff, summary = "", "no change to roll back"
    else:
        finding = Finding(
            _service_of(change),
            kind_to_category.get(change.kind, "other"),
            f"most recent change before the alert: {change.title}",
        )
        diff, summary = patching.reverse_diff(change.diff), f"roll back {change.change_id}"
    finding.evidence = _top_error_or_slow_spans(ctx, finding.service, alert, 2)
    hyp = [
        {
            "statement": "the most recent change caused the alert",
            "verdict": "confirmed",
            "reasoning": "rollback-first policy",
            "evidence": [],
        }
    ]
    return _report(ctx, alert, finding, change, diff, summary, hyp)


def _top_error_or_slow_spans(ctx: ToolContext, service: str, alert: Alert, n: int) -> list[str]:
    rows = ctx.conn.execute(
        "SELECT span_id FROM spans WHERE service = ? AND start_ns >= ?"
        " ORDER BY status = 'error' DESC, duration_ms DESC LIMIT ?",
        (service, alert.started_ns, n),
    ).fetchall()
    if not rows:
        rows = ctx.conn.execute(
            "SELECT span_id FROM spans WHERE start_ns >= ? ORDER BY status = 'error' DESC,"
            " duration_ms DESC LIMIT ?",
            (alert.started_ns, n),
        ).fetchall()
    return [f"span:{r[0]}" for r in rows]


# ---------------------------------------------------------------- rule-based baseline

SIGNATURES: list[tuple[str, str]] = [
    (r"could not acquire a connection", "connection_pool_exhaustion"),
    (r"exited with code 137|OOMKilled", "memory_leak"),
    (r"database or disk is full|No space left", "replica_failure"),
    (r"failed to process message", "poison_message"),
    (r"timeout calling|timed out", "timeout_misconfiguration"),
    (r"shedding|overloaded", "retry_storm"),
    (r"KeyError|TypeError|AttributeError|ValueError", "bad_feature_flag"),
]


def _deepest_errors(ctx: ToolContext, lo: int) -> Counter[tuple[str, str]]:
    errors = ctx.conn.execute(
        "SELECT span_id, parent_span_id, service, COALESCE(instance, service) FROM spans"
        " WHERE status = 'error' AND start_ns >= ?",
        (lo,),
    ).fetchall()
    parents_with_error_child = {p for _, p, _, _ in errors if p}
    return Counter(
        (svc, inst) for sid, _, svc, inst in errors if sid not in parents_with_error_child
    )


def _self_time(ctx: ToolContext, lo: int, hi: int) -> tuple[dict[tuple[str, str], float], int]:
    rows = ctx.conn.execute(
        "SELECT span_id, parent_span_id, service, kind, name, duration_ms, attributes, trace_id"
        " FROM spans WHERE start_ns >= ? AND start_ns < ?",
        (lo, hi),
    ).fetchall()
    children: dict[str, float] = defaultdict(float)
    has_child: set[str] = set()
    attrs = {r[0]: json.loads(r[6]) for r in rows}
    for _, parent, *_rest in rows:
        if parent:
            has_child.add(parent)
    for _sid, parent, _svc, _kind, _name, dur, _a, _t in rows:
        if parent:
            children[parent] += dur
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for sid, parent, svc, kind, name, dur, _a, _t in rows:
        own = max(0.0, dur - children.get(sid, 0.0))
        if name == "db.pool.acquire":
            key = (svc, "pool")
        elif kind == "CLIENT" and sid not in has_child:
            a = attrs[sid]
            peer = a.get("peer.service") or (attrs.get(parent or "", {}).get("peer.service"))
            if "db.system" in a:
                key = (svc, "db")
            elif peer:
                key = (
                    str(peer).rsplit("-", 1)[0] if str(peer).count("-") > 1 else str(peer),
                    "external",
                )
            else:
                key = (svc, "client-wait")
        else:
            key = (svc, "self")
        totals[key] += own
    requests = len({r[7] for r in rows if r[2] == "gateway"}) or 1
    return {k: v / requests for k, v in totals.items()}, requests


def _db_spans_per_trace(ctx: ToolContext, service: str, lo: int, hi: int) -> float:
    n, traces = ctx.conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT trace_id) FROM spans WHERE service = ? AND kind = 'CLIENT'"
        " AND attributes LIKE '%db.system%' AND start_ns >= ? AND start_ns < ?",
        (service, lo, hi),
    ).fetchone()
    return float(n) / traces if traces else 0.0


def _signature(ctx: ToolContext, lo: int, service: str | None) -> tuple[str, list[str]] | None:
    params: list[Any] = [lo]
    clause = "ts_ns >= ? AND severity IN ('ERROR', 'WARN', 'WARNING')"
    if service:
        clause += " AND (service = ? OR service = 'platform')"
        params.append(service)
    logs = ctx.conn.execute(
        f"SELECT log_id, body FROM logs WHERE {clause} ORDER BY ts_ns", params
    ).fetchall()
    for pattern, category in SIGNATURES:
        hits = [f"log:{lid}" for lid, body in logs if re.search(pattern, body)]
        if hits:
            return category, hits[:3]
    return None


def localise(ctx: ToolContext, alert: Alert) -> Finding:
    lo = alert.started_ns
    if alert.rule == "UnplannedRestart":
        sig = _signature(ctx, ctx.start_ns, None)
        return Finding(
            alert.service, sig[0] if sig else "other", "unplanned restarts", sig[1] if sig else []
        )
    if alert.rule == "FulfilmentBacklog":
        sig = _signature(ctx, lo - 15 * NS, "worker")
        return Finding(
            "worker", sig[0] if sig else "other", "fulfilment queue backlog", sig[1] if sig else []
        )
    if alert.rule == "CheckoutErrorRate":
        deepest = _deepest_errors(ctx, lo)
        if deepest:
            (service, instance), count = deepest.most_common(1)[0]
            total = sum(c for (s, _), c in deepest.items() if s == service)
            sig = _signature(ctx, lo, service)
            category = sig[0] if sig else "other"
            retries = (
                _metric_ratio(ctx, "http.client.retries", lo) if category == "retry_storm" else None
            )
            if retries and retries[1] > 5:
                return Finding(
                    retries[0],
                    "retry_storm",
                    f"retries from {retries[0]} grew {retries[1]:.0f}x",
                    (sig[1] if sig else []) + ["metric:http.client.retries"],
                )
            finding = Finding(
                service, category, f"deepest failing spans are in {service}", sig[1] if sig else []
            )
            if total and count / total > 0.9 and instance != service:
                finding.instance = instance
            return finding
    before, _ = _self_time(ctx, ctx.start_ns + 2 * NS, lo)
    after, _ = _self_time(ctx, lo, ctx.end_ns + NS)
    deltas = {k: after.get(k, 0.0) - before.get(k, 0.0) for k in after}
    (service, part), delta = max(deltas.items(), key=lambda kv: kv[1])
    if part == "db":
        ratio = _db_spans_per_trace(ctx, service, lo, ctx.end_ns + NS) / max(
            _db_spans_per_trace(ctx, service, ctx.start_ns, lo), 1e-9
        )
        category = "n_plus_one_query" if ratio > 3 else "slow_query"
    else:
        category = {
            "pool": "connection_pool_exhaustion",
            "external": "external_dependency_latency",
        }.get(part, "other")
    reason = f"self-time per request in {service} ({part}) grew by {delta:.0f}ms"
    return Finding(service, category, reason)


def _metric_ratio(ctx: ToolContext, name: str, lo: int) -> tuple[str, float] | None:
    rows = ctx.conn.execute(
        "SELECT service, SUM(ts_ns < ?) , SUM(CASE WHEN ts_ns < ? THEN value ELSE 0 END),"
        " SUM(ts_ns >= ?), SUM(CASE WHEN ts_ns >= ? THEN value ELSE 0 END)"
        " FROM metric_points WHERE name = ? GROUP BY service",
        (lo, lo, lo, lo, name),
    ).fetchall()
    best: tuple[str, float] | None = None
    for service, nb, vb, na, va in rows:
        rate_before = (vb or 0) / max(nb or 0, 1)
        rate_after = (va or 0) / max(na or 0, 1)
        ratio = rate_after / max(rate_before, 0.5)
        if best is None or ratio > best[1]:
            best = (service, ratio)
    return best


def rules(ctx: ToolContext, alert: Alert) -> Report:
    finding = localise(ctx, alert)
    if not any(e.startswith(("span:", "log:", "metric:", "trace:")) for e in finding.evidence):
        finding.evidence = (
            _top_error_or_slow_spans(ctx, finding.service, alert, 3) + finding.evidence
        )
    changes = _changes(ctx.conn)
    change = None
    if finding.category not in ("external_dependency_latency", "replica_failure"):
        change = _latest(changes, alert.fired_ns, finding.service)
    if finding.instance and finding.category == "replica_failure":
        cfg = (ctx.tree / "config" / "orders.toml").read_text()
        match = re.search(r"^replicas = (\[.*\])$", cfg, re.M)
        diff = ""
        if match:
            replicas = [r for r in json.loads(match.group(1)) if r != finding.instance]
            new_line = "replicas = [" + ", ".join(json.dumps(r) for r in replicas) + "]"
            diff = patching.make_diff(
                ctx.tree, [patching.Edit("config/orders.toml", match.group(0), new_line)]
            )
        summary = f"drain {finding.instance} from the payments rotation"
    elif change is not None:
        diff, summary = patching.reverse_diff(change.diff), f"roll back {change.change_id}"
    else:
        diff, summary = "", "no automated remediation"
    hypotheses = [
        {
            "statement": f"{finding.category} in {finding.service}",
            "verdict": "confirmed",
            "reasoning": finding.reason,
            "evidence": finding.evidence[:2],
        }
    ]
    return _report(ctx, alert, finding, change, diff, summary, hypotheses)


BASELINES = {"rollback": rollback, "rules": rules}
