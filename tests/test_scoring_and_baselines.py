"""Scoring against ground truth, the non-LLM baselines and the model-output parsers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from conftest import CULPRIT, DECOY, Snapshot, make_report
from incident_agent.agent.backends.anthropic_backend import to_api_messages
from incident_agent.agent.backends.mlx_backend import parse_tool_calls
from incident_agent.agent.messages import Message, ToolCall
from incident_agent.agent.report import Report
from incident_agent.agent.tools import ToolContext
from incident_agent.agent.verify import verify
from incident_agent.baseline import heuristics
from incident_agent.evaluation.harness import wilson
from incident_agent.evaluation.scoring import score


def record_for(
    snapshot: Snapshot, ro: sqlite3.Connection, report: dict[str, Any]
) -> dict[str, Any]:
    parsed = Report.model_validate(report)
    v = verify(parsed, ro, snapshot.tree)
    return {
        "status": "accepted" if v.accepted else "rejected",
        "report": parsed.model_dump(),
        "verification": {"valid": v.valid, "invalid": v.invalid, "diff_applies": v.diff_applies},
    }


def truth(snapshot: Snapshot) -> dict[str, Any]:
    t: dict[str, Any] = json.loads((snapshot.path / "ground_truth.json").read_text())
    return t


def test_correct_report_scores_correct(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    row = score(record_for(snapshot, ro, make_report(snapshot)), truth(snapshot), ro)
    assert row["root_cause_correct"] and row["fix_applies"]
    assert row["refs"] == row["refs_valid"] and row["evidence_relevance"] == 1.0


def test_wrong_change_is_not_correct(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    row = score(
        record_for(snapshot, ro, make_report(snapshot, change_id=DECOY)), truth(snapshot), ro
    )
    assert row["service_ok"] and row["category_ok"] and not row["change_ok"]
    assert not row["root_cause_correct"]


def test_rejected_report_is_never_correct(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    report = make_report(snapshot, evidence=["span:0123456789abcdef", f"change:{CULPRIT}"])
    row = score(record_for(snapshot, ro, report), truth(snapshot), ro)
    assert row["service_ok"] and row["category_ok"] and row["change_ok"]
    assert not row["accepted"] and not row["root_cause_correct"]


def test_injection_following_is_detected(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    report = make_report(snapshot, service="inventory", category="memory_leak", change_id=None)
    t = truth(snapshot) | {"scenario": "poison_message"}
    assert score(record_for(snapshot, ro, report), t, ro)["injection_followed"]


def test_baselines_produce_verifiable_reports(snapshot: Snapshot) -> None:
    ctx = ToolContext.open(snapshot.path)
    try:
        for name, baseline in heuristics.BASELINES.items():
            report = baseline(ctx, snapshot.alert)
            result = verify(report, ctx.conn, ctx.tree)
            assert result.accepted, (name, result.feedback())
        rollback = heuristics.rollback(ctx, snapshot.alert)
        assert rollback.root_cause.change_id == CULPRIT  # latest change before the alert
        assert verify(rollback, ctx.conn, ctx.tree).diff_applies  # reverts that change
    finally:
        ctx.conn.close()


def test_wilson_interval() -> None:
    lo, hi = wilson(15, 30)
    assert (round(lo, 4), round(hi, 4)) == (0.3315, 0.6685)
    assert wilson(0, 0) == (0.0, 0.0)


def test_parse_qwen_tool_calls() -> None:
    text = (
        'Let me look.\n<tool_call>\n{"name": "search_logs", "arguments": {"service": "orders"}}\n'
        '</tool_call>\n<tool_call>{"name": "list_changes", "arguments": "{}"}}</tool_call>'
    )
    remaining, calls = parse_tool_calls(text, turn=3)
    assert remaining == "Let me look."
    assert [(c.name, c.arguments) for c in calls] == [
        ("search_logs", {"service": "orders"}),
        ("list_changes", {}),
    ]
    assert calls[0].id == "call_3_0"


def test_parse_ignores_garbage_tool_calls() -> None:
    _, calls = parse_tool_calls("<tool_call>not json</tool_call><tool_call>{}</tool_call>", 1)
    assert calls == []


def test_anthropic_message_conversion_merges_tool_results() -> None:
    messages = [
        Message("system", "sys"),
        Message("user", "page"),
        Message(
            "assistant", "", tool_calls=[ToolCall("t1", "a", {}), ToolCall("t2", "b", {"x": 1})]
        ),
        Message("tool", "r1", tool_call_id="t1", name="a"),
        Message("tool", "r2", tool_call_id="t2", name="b"),
        Message("user", "nudge"),
    ]
    system, api = to_api_messages(messages)
    assert system == "sys"
    assert [m["role"] for m in api] == ["user", "assistant", "user"]
    assert [b["type"] for b in api[2]["content"]] == ["tool_result", "tool_result", "text"]
    assert api[1]["content"][1] == {"type": "tool_use", "id": "t2", "name": "b", "input": {"x": 1}}
