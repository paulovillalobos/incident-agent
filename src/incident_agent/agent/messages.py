"""Provider-neutral conversation types shared by the loop and every model backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Provider-specific payload that must be replayed verbatim (e.g. thinking blocks).
    raw: Any = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Tokens actually processed this turn after prompt-cache reuse (local backends).
    prefill_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.prefill_tokens += other.prefill_tokens


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: Any = None


class ChatModel(Protocol):
    """One model turn: given the conversation and the tools, return text and tool calls."""

    name: str

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelTurn: ...
