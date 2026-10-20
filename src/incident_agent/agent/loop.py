"""The investigation loop: model turn -> tool calls -> results, until a verified report.

No agent framework: the loop is small, and owning it keeps every guardrail explicit
(tool budget, wall-clock limit, per-tool timeout, output caps, citation verification with
one revision round) and identical across the scripted, local and hosted backends.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import ValidationError

from incident_agent.agent import prompts
from incident_agent.agent.messages import ChatModel, Message, ToolCall, ToolSpec, Usage
from incident_agent.agent.report import Report
from incident_agent.agent.tools import (
    TOOLS,
    ToolContext,
    ToolError,
    render,
    run_tool,
    simplify_schema,
)
from incident_agent.agent.verify import Verification, verify
from incident_agent.alerts import Alert

SUBMIT = "submit_report"


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int = 16
    max_turns: int = 28
    max_seconds: float = 900.0
    max_output_chars: int = 3500
    max_submissions: int = 2


@dataclass
class Step:
    index: int
    tool: str
    arguments: dict[str, Any]
    output: str
    duration_ms: float
    error: str | None = None


@dataclass
class Investigation:
    backend: str
    status: str = "running"  # accepted | rejected | no_report | error
    steps: list[Step] = field(default_factory=list)
    report: Report | None = None
    verification: Verification | None = None
    submissions: int = 0
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    seconds: float = 0.0
    error: str | None = None
    final_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "status": self.status,
            "steps": [asdict(s) for s in self.steps],
            "report": self.report.model_dump() if self.report else None,
            "verification": asdict(self.verification) if self.verification else None,
            "submissions": self.submissions,
            "turns": self.turns,
            "tool_calls": sum(1 for s in self.steps if s.tool != SUBMIT),
            "usage": asdict(self.usage),
            "seconds": round(self.seconds, 2),
            "error": self.error,
            "final_text": self.final_text[:2000],
        }


def tool_specs() -> list[ToolSpec]:
    specs = [t.spec() for t in TOOLS]
    specs.append(
        ToolSpec(
            SUBMIT,
            "Submit the final investigation report. Every evidence reference is verified.",
            simplify_schema(Report.model_json_schema()),
        )
    )
    return specs


def report_in_text(text: str) -> dict[str, Any] | None:
    """A report written as JSON in the reply instead of a ``submit_report`` call.

    Small local models often do this. The JSON is taken from a fenced block or from the
    outermost braces, and treated as a submission only if it has a ``root_cause``; it then
    goes through exactly the same validation and verification as a tool call.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("report"), dict):
        parsed = parsed["report"]
    return parsed if isinstance(parsed, dict) and "root_cause" in parsed else None


def _submit(
    ctx: ToolContext, inv: Investigation, call: ToolCall, budget: Budget
) -> tuple[str, bool]:
    """Validate and verify a submitted report. Returns (tool result, finished)."""
    inv.submissions += 1
    args = call.arguments.get("report", call.arguments)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            return f"report is not valid JSON: {exc}", inv.submissions >= budget.max_submissions
    try:
        report = Report.model_validate(args)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8])
        return (
            f"report rejected, schema errors: {errors}",
            inv.submissions >= budget.max_submissions,
        )
    result = verify(report, ctx.conn, ctx.tree)
    inv.report, inv.verification = report, result
    if result.accepted and result.diff_applies:
        return "report accepted", True
    if inv.submissions >= budget.max_submissions:
        return "report recorded with verification errors", True
    status = "accepted, but" if result.accepted else "rejected:"
    return (
        f"report {status} fix it and call submit_report again.\n{result.feedback()}",
        False,
    )


def investigate(
    model: ChatModel, ctx: ToolContext, alert: Alert, budget: Budget | None = None
) -> Investigation:
    budget = budget or Budget()
    inv = Investigation(backend=model.name)
    specs = tool_specs()
    submit_only = [s for s in specs if s.name == SUBMIT]
    messages = [
        Message("system", prompts.SYSTEM),
        Message(
            "user", prompts.alert_message(alert, ctx.ts(alert.fired_ns), ctx.ts(alert.started_ns))
        ),
    ]
    started = time.monotonic()
    tool_calls = 0
    finished = False
    try:
        while not finished and inv.turns < budget.max_turns:
            if time.monotonic() - started > budget.max_seconds:
                inv.error = f"wall-clock limit of {budget.max_seconds:.0f}s reached"
                break
            exhausted = tool_calls >= budget.max_tool_calls
            turn = model.complete(messages, submit_only if exhausted else specs)
            inv.turns += 1
            inv.usage.add(turn.usage)
            messages.append(
                Message("assistant", turn.text, tool_calls=turn.tool_calls, raw=turn.raw)
            )
            if turn.text:
                inv.final_text = turn.text
            if not turn.tool_calls:
                written = report_in_text(turn.text)
                if written is None:
                    messages.append(
                        Message("user", prompts.BUDGET_EXHAUSTED if exhausted else prompts.NUDGE)
                    )
                    continue
                call = ToolCall(f"text_{inv.turns}", SUBMIT, {"report": written})
                output, finished = _submit(ctx, inv, call, budget)
                inv.steps.append(Step(len(inv.steps) + 1, SUBMIT, {"from_text": True}, output, 0.0))
                if not finished:
                    messages.append(Message("user", prompts.TEXT_REPORT + "\n" + output))
                continue
            for call in turn.tool_calls:
                t0 = time.perf_counter()
                error = None
                if call.name == SUBMIT:
                    output, finished = _submit(ctx, inv, call, budget)
                elif exhausted or tool_calls >= budget.max_tool_calls:
                    output, error = prompts.BUDGET_EXHAUSTED, "budget"
                else:
                    tool_calls += 1
                    try:
                        output = render(
                            run_tool(ctx, call.name, call.arguments), budget.max_output_chars
                        )
                    except ToolError as exc:
                        output, error = f"error: {exc}", str(exc)
                inv.steps.append(
                    Step(
                        len(inv.steps) + 1,
                        call.name,
                        call.arguments,
                        output,
                        round((time.perf_counter() - t0) * 1000, 1),
                        error,
                    )
                )
                messages.append(Message("tool", output, tool_call_id=call.id, name=call.name))
                if finished:
                    break
            if not finished and tool_calls >= budget.max_tool_calls and not exhausted:
                messages.append(Message("user", prompts.BUDGET_EXHAUSTED))
    except Exception as exc:
        inv.error = f"{type(exc).__name__}: {exc}"
        inv.status = "error"
    inv.seconds = time.monotonic() - started
    if inv.status != "error":
        if inv.report is None:
            inv.status = "no_report"
        else:
            assert inv.verification is not None
            inv.status = "accepted" if inv.verification.accepted else "rejected"
    return inv
