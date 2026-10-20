"""Claude backend via the official Anthropic Python SDK (Messages API, client-side tools).

Enabled only when ANTHROPIC_API_KEY is set (the CLI reads it from a local ``.env``). The
assistant content blocks returned by the API are replayed verbatim on later turns, as the
API requires for thinking blocks. The server-side refusal fallback is enabled.
"""

from __future__ import annotations

import os
from typing import Any

from incident_agent.agent.messages import Message, ModelTurn, ToolCall, ToolSpec, Usage

DEFAULT_MODEL = "claude-opus-5-5"


class RefusalError(RuntimeError):
    pass


def to_api_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split off the system prompt and merge tool results into user turns."""
    system = messages[0].content if messages and messages[0].role == "system" else ""
    out: list[dict[str, Any]] = []
    for m in messages[1:] if system else messages:
        if m.role == "assistant":
            if m.raw is not None:
                content: Any = m.raw
            else:
                content = [{"type": "text", "text": m.content}] if m.content else []
                content += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in m.tool_calls
                ]
            out.append({"role": "assistant", "content": content})
            continue
        block = (
            {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            if m.role == "tool"
            else {"type": "text", "text": m.content}
        )
        if out and out[-1]["role"] == "user":
            out[-1]["content"].append(block)
        else:
            out.append({"role": "user", "content": [block]})
    return system, out


class AnthropicModel:
    def __init__(
        self, model: str | None = None, effort: str = "medium", client: Any = None
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.environ.get("IA_ANTHROPIC_MODEL", DEFAULT_MODEL)
        self.effort = effort
        self.name = f"anthropic:{self.model}"

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        system, api_messages = to_api_messages(messages)
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ],
            messages=api_messages,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RefusalError(str(getattr(response, "stop_details", None)))
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"
        ]
        u = response.usage
        cached = (getattr(u, "cache_read_input_tokens", 0) or 0) + (
            getattr(u, "cache_creation_input_tokens", 0) or 0
        )
        usage = Usage(input_tokens=u.input_tokens + cached, output_tokens=u.output_tokens)
        return ModelTurn(text, calls, usage, raw=response.content)
