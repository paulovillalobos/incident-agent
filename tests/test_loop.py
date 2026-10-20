"""The investigation loop driven by a scripted model (no LLM)."""

from __future__ import annotations

import json
from typing import Any

from conftest import CULPRIT, Snapshot, make_report
from incident_agent.agent.backends.scripted import ScriptedModel, last_tool_result
from incident_agent.agent.loop import SUBMIT, Budget, investigate, report_in_text, tool_specs
from incident_agent.agent.messages import Message, ModelTurn, ToolCall, ToolSpec, Usage
from incident_agent.agent.tools import ToolContext


def run(snapshot: Snapshot, model: ScriptedModel, budget: Budget | None = None) -> Any:
    ctx = ToolContext.open(snapshot.path)
    try:
        return investigate(model, ctx, snapshot.alert, budget)
    finally:
        ctx.conn.close()


def investigator(snapshot: Snapshot) -> ScriptedModel:
    """A policy that reads tool results and cites only ids it has seen."""
    plan: list[tuple[str, dict[str, Any]]] = [
        ("list_changes", {}),
        ("search_spans", {"service": "orders", "status": "error", "name": "pool"}),
        ("search_logs", {"service": "orders", "severity": "ERROR", "limit": 3}),
        ("read_file", {"path": "config/orders.toml"}),
    ]

    def policy(messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        step = sum(1 for m in messages if m.role == "assistant")
        if step < len(plan):
            name, args = plan[step]
            return ModelTurn("", [ToolCall(f"c{step}", name, args)], Usage(100, 10))
        span = last_tool_result(messages, "search_spans")["spans"][0]
        log = last_tool_result(messages, "search_logs")["logs"][0]
        report = make_report(
            snapshot,
            evidence=[f"span:{span['span_id']}", f"log:{log['log_id']}", f"change:{CULPRIT}"],
        )
        return ModelTurn("submitting", [ToolCall("s", SUBMIT, {"report": report})], Usage(100, 50))

    return ScriptedModel(policy)


def test_investigation_reaches_a_verified_report(snapshot: Snapshot) -> None:
    inv = run(snapshot, investigator(snapshot))
    assert inv.status == "accepted", inv.verification and inv.verification.feedback()
    assert [s.tool for s in inv.steps] == [
        "list_changes", "search_spans", "search_logs", "read_file", SUBMIT,
    ]  # fmt: skip
    record = inv.as_dict()
    assert record["tool_calls"] == 4 and record["usage"]["input_tokens"] == 500
    assert record["report"]["root_cause"]["change_id"] == CULPRIT


def test_fabricated_citation_gets_one_revision(snapshot: Snapshot) -> None:
    bad = make_report(snapshot, evidence=["span:0123456789abcdef", f"change:{CULPRIT}"])
    good = make_report(snapshot)
    model = ScriptedModel.from_turns([[(SUBMIT, {"report": bad})], [(SUBMIT, {"report": good})]])
    inv = run(snapshot, model)
    assert inv.status == "accepted" and inv.submissions == 2
    assert "span:0123456789abcdef: span not found" in inv.steps[0].output


def test_report_stays_rejected_after_max_submissions(snapshot: Snapshot) -> None:
    bad = make_report(snapshot, evidence=["span:0123456789abcdef"])
    model = ScriptedModel.from_turns([[(SUBMIT, {"report": bad})]] * 5)
    inv = run(snapshot, model)
    assert inv.status == "rejected" and inv.submissions == Budget().max_submissions


def test_schema_errors_are_returned_to_the_model(snapshot: Snapshot) -> None:
    broken = make_report(snapshot, category="cosmic_rays")
    model = ScriptedModel.from_turns(
        [[(SUBMIT, {"report": broken})], [(SUBMIT, {"report": make_report(snapshot)})]]
    )
    inv = run(snapshot, model)
    assert "root_cause.category" in inv.steps[0].output and inv.status == "accepted"


def test_tool_budget_forces_submission(snapshot: Snapshot) -> None:
    seen_tools: list[list[str]] = []

    def policy(messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        seen_tools.append([t.name for t in tools])
        if [t.name for t in tools] == [SUBMIT]:
            return ModelTurn(
                "", [ToolCall("s", SUBMIT, {"report": make_report(snapshot)})], Usage()
            )
        return ModelTurn("", [ToolCall(f"c{len(messages)}", "list_services", {})], Usage())

    inv = run(snapshot, ScriptedModel(policy), Budget(max_tool_calls=3))
    assert inv.status == "accepted"
    assert sum(1 for s in inv.steps if s.tool == "list_services") == 3
    assert seen_tools[-1] == [SUBMIT] and len(seen_tools[0]) == len(tool_specs())


def test_calls_beyond_budget_in_one_turn_are_refused(snapshot: Snapshot) -> None:
    many = [("list_services", {})] * 6
    model = ScriptedModel.from_turns([many, [(SUBMIT, {"report": make_report(snapshot)})]])
    inv = run(snapshot, model, Budget(max_tool_calls=4))
    assert [s.error for s in inv.steps[:6]].count("budget") == 2


def test_tool_errors_are_fed_back_not_raised(snapshot: Snapshot) -> None:
    model = ScriptedModel.from_turns(
        [
            [("run_sql", {"query": "DELETE FROM spans"})],
            [(SUBMIT, {"report": make_report(snapshot)})],
        ]
    )
    inv = run(snapshot, model)
    assert inv.steps[0].output.startswith("error: only a single SELECT")
    assert inv.status == "accepted"


def test_no_report_when_model_never_submits(snapshot: Snapshot) -> None:
    inv = run(snapshot, ScriptedModel.from_turns(["thinking"] * 3), Budget(max_turns=3))
    assert inv.status == "no_report" and inv.turns == 3


def test_model_exception_is_recorded(snapshot: Snapshot) -> None:
    def policy(messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        raise RuntimeError("model crashed")

    inv = run(snapshot, ScriptedModel(policy))
    assert inv.status == "error" and "model crashed" in (inv.error or "")


def test_injected_log_text_does_not_reach_the_system_prompt(snapshot: Snapshot) -> None:
    captured: list[list[Message]] = []

    def policy(messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        captured.append(list(messages))
        if len(captured) == 1:
            return ModelTurn("", [ToolCall("c", "search_logs", {"service": "worker"})], Usage())
        return ModelTurn("", [ToolCall("s", SUBMIT, {"report": make_report(snapshot)})], Usage())

    run(snapshot, ScriptedModel(policy))
    final = captured[-1]
    assert "ignore previous" not in final[0].content.lower()
    tool_msg = next(m for m in final if m.role == "tool")
    assert '"warning":"contains instruction-like text' in tool_msg.content


def test_report_written_as_text_is_verified_like_a_submission(snapshot: Snapshot) -> None:
    text = "Here is my report:\n```json\n" + json.dumps(make_report(snapshot)) + "\n```"
    inv = run(snapshot, ScriptedModel.from_turns([text]))
    assert inv.status == "accepted" and inv.submissions == 1
    assert inv.steps[-1].arguments == {"from_text": True}


def test_text_without_a_report_is_nudged() -> None:
    assert report_in_text("I will look at the logs next.") is None
    assert report_in_text('{"note": "no root cause"}') is None
    assert report_in_text('{"report": {"root_cause": {}}}') == {"root_cause": {}}
