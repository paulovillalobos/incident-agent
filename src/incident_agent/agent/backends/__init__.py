"""Backend registry: LLM backends (mlx, anthropic) and the non-LLM baselines."""

from __future__ import annotations

import os
from pathlib import Path

from incident_agent.agent.messages import ChatModel

LLM_BACKENDS = ("mlx", "anthropic")
BASELINES = ("rules", "rollback")


class BackendUnavailableError(RuntimeError):
    pass


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=VALUE lines into the environment without overriding existing variables."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_model(name: str) -> ChatModel:
    if name == "mlx":
        try:
            from incident_agent.agent.backends.mlx_backend import MLXModel
        except ImportError as exc:  # pragma: no cover - depends on the platform
            raise BackendUnavailableError("mlx-lm is not installed (uv sync --extra mlx)") from exc
        return MLXModel(os.environ.get("IA_MLX_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit"))
    if name == "anthropic":
        load_dotenv()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise BackendUnavailableError("ANTHROPIC_API_KEY is not set (put it in .env)")
        from incident_agent.agent.backends.anthropic_backend import AnthropicModel

        return AnthropicModel()
    raise BackendUnavailableError(f"unknown backend {name!r}")
