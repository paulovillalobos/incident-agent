"""A deterministic model for tests and CI: a policy function decides each turn."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from incident_agent.agent.messages import Message, ModelTurn, ToolCall, ToolSpec, Usage

Policy = Callable[[list[Message], list[ToolSpec]], ModelTurn]


class ScriptedModel:
    def __init__(self, policy: Policy, name: str = "scripted") -> None:
        self.policy = policy
        self.name = name
        self.calls = 0

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        self.calls += 1
        return self.policy(messages, tools)

    @classmethod
    def from_turns(
        cls, turns: Sequence[Sequence[tuple[str, dict[str, Any]]] | str]
    ) -> ScriptedModel:
        """Replay fixed turns; each is a list of (tool, arguments) or a plain text reply."""
        queue = list(turns)

        def policy(messages: list[Message], _: list[ToolSpec]) -> ModelTurn:
            if not queue:
                return ModelTurn("done", [], Usage())
            turn = queue.pop(0)
            if isinstance(turn, str):
                return ModelTurn(turn, [], Usage(output_tokens=len(turn.split())))
            calls = [ToolCall(f"call_{len(messages)}_{i}", n, a) for i, (n, a) in enumerate(turn)]
            return ModelTurn(
                "",
                calls,
                Usage(
                    input_tokens=sum(len(m.content) for m in messages) // 4,
                    output_tokens=len(json.dumps([list(c) for c in turn])) // 4,
                ),
            )

        return cls(policy)


def last_tool_result(messages: list[Message], tool: str) -> dict[str, Any]:
    """Parse the most recent result of ``tool`` from the conversation (for scripted policies)."""
    for message in reversed(messages):
        if message.role == "tool" and message.name == tool:
            parsed: dict[str, Any] = json.loads(message.content)
            return parsed
    raise LookupError(tool)
