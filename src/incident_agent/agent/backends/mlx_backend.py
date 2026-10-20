"""Local model backend on Apple silicon with MLX (default: Qwen3-4B-Instruct-2507, 4-bit).

The conversation is rendered with the model's own chat template (Qwen's ``<tool_call>``
format). The KV cache is kept between turns: each turn only the tokens after the longest
common prefix with the previous turn are processed, which makes a 15-turn investigation
practical on a laptop.
"""

from __future__ import annotations

import json
import re
from typing import Any

from incident_agent.agent.messages import Message, ModelTurn, ToolCall, ToolSpec, Usage

DEFAULT_REPO = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)


class ContextOverflowError(RuntimeError):
    pass


def to_chat_template(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            entry["tool_calls"] = [
                {"type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in m.tool_calls
            ]
        out.append(entry)
    return out


def parse_tool_calls(text: str, turn: int) -> tuple[str, list[ToolCall]]:
    """Extract ``<tool_call>{json}</tool_call>`` blocks; return the remaining text and calls."""
    calls: list[ToolCall] = []
    for i, raw in enumerate(_TOOL_CALL.findall(text)):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
            for end in range(len(raw), 0, -1):  # tolerate trailing garbage after the object
                if raw[end - 1] == "}":
                    try:
                        payload = json.loads(raw[:end])
                        break
                    except json.JSONDecodeError:
                        continue
        if not isinstance(payload, dict) or "name" not in payload:
            continue
        args = payload.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        calls.append(
            ToolCall(
                f"call_{turn}_{i}", str(payload["name"]), args if isinstance(args, dict) else {}
            )
        )
    remaining = _TOOL_CALL.sub("", text).strip()
    return remaining, calls


class MLXModel:
    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        *,
        temperature: float = 0.7,
        top_p: float = 0.8,
        max_tokens: int = 3072,
        max_context: int = 28000,
    ) -> None:
        from mlx_lm import load

        loaded = load(repo)
        self.model, self.tokenizer = loaded[0], loaded[1]
        self.repo = repo
        self.name = "mlx:" + repo.split("/")[-1]
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.max_context = max_context
        self.turn = 0
        self.reset(seed=0)

    def reset(self, seed: int) -> None:
        """Start a new conversation: empty KV cache and a fixed sampling seed."""
        import mlx.core as mx
        from mlx_lm.models.cache import make_prompt_cache

        mx.random.seed(seed)
        self.cache = make_prompt_cache(self.model)
        self.cached: list[int] = []
        self.turn = 0

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import trim_prompt_cache
        from mlx_lm.sample_utils import make_sampler

        self.turn += 1
        tool_json = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        text = self.tokenizer.apply_chat_template(  # type: ignore[no-untyped-call]
            to_chat_template(messages), tools=tool_json, add_generation_prompt=True, tokenize=False
        )
        prompt: list[int] = self.tokenizer.encode(text, add_special_tokens=False)
        if len(prompt) > self.max_context:
            raise ContextOverflowError(
                f"prompt has {len(prompt)} tokens (limit {self.max_context})"
            )
        common = 0
        for a, b in zip(prompt, self.cached, strict=False):
            if a != b:
                break
            common += 1
        common = min(common, len(prompt) - 1)
        if len(self.cached) > common:
            trim_prompt_cache(self.cache, len(self.cached) - common)
        suffix = prompt[common:]
        sampler = make_sampler(temp=self.temperature, top_p=self.top_p)
        out_text, out_tokens = "", []
        for resp in stream_generate(
            self.model,
            self.tokenizer,
            prompt=suffix,
            max_tokens=self.max_tokens,
            sampler=sampler,
            prompt_cache=self.cache,
        ):
            out_text += resp.text
            out_tokens.append(resp.token)
            if resp.finish_reason:
                break
        offset = int(self.cache[0].offset)
        self.cached = (prompt + out_tokens)[:offset]
        content, calls = parse_tool_calls(out_text, self.turn)
        usage = Usage(
            input_tokens=len(prompt), output_tokens=len(out_tokens), prefill_tokens=len(suffix)
        )
        return ModelTurn(content, calls, usage, raw={"text": out_text})
