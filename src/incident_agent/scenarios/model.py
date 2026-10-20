"""Scenario definitions: timed changes and faults with a known root cause.

A scenario lives in ``scenarios/<name>/scenario.toml``. Each event happens ``at_s``
seconds into the run and is either a recorded change (exact text edits to the system
tree, rendered as a diff into the change log, followed by restarts) or an unrecorded
operational/external fault (``action``). ``[ground_truth]`` is never shown to the agent.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incident_agent.patching import Edit

CATEGORIES = (
    "external_dependency_latency",
    "connection_pool_exhaustion",
    "bad_feature_flag",
    "slow_query",
    "retry_storm",
    "memory_leak",
    "timeout_misconfiguration",
    "poison_message",
    "replica_failure",
    "n_plus_one_query",
    "other",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG = REPO_ROOT / "scenarios"
SYSTEM_TREE = REPO_ROOT / "system"


@dataclass(frozen=True)
class Event:
    at_s: float
    kind: str
    service: str
    title: str
    author: str = "deploy-bot"
    edits: tuple[Edit, ...] = ()
    restart: tuple[str, ...] = ()
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    culprit: bool = False

    @property
    def recorded(self) -> bool:
        return bool(self.edits)


@dataclass(frozen=True)
class GroundTruth:
    service: str
    category: str
    explanation: str
    accept_services: tuple[str, ...]
    change: str  # "culprit" or "none"
    fix_check: str = "rerun"


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    duration_s: float
    rate: float
    events: tuple[Event, ...]
    ground_truth: GroundTruth
    reference_fix: tuple[Edit, ...]
    path: Path

    @property
    def culprit(self) -> Event | None:
        return next((e for e in self.events if e.culprit), None)


def _edits(raw: list[dict[str, str]]) -> tuple[Edit, ...]:
    return tuple(Edit(path=e["path"], old=e.get("old", ""), new=e["new"]) for e in raw)


def load(path: Path) -> Scenario:
    data = tomllib.loads((path / "scenario.toml").read_text())
    events = tuple(
        Event(
            at_s=float(e["at_s"]),
            kind=e["kind"],
            service=e["service"],
            title=e["title"],
            author=e.get("author", "deploy-bot"),
            edits=_edits(e.get("edits", [])),
            restart=tuple(e.get("restart", [])),
            action=e.get("action"),
            params=e.get("params", {}),
            culprit=bool(e.get("culprit", False)),
        )
        for e in data["events"]
    )
    gt = data["ground_truth"]
    if gt["category"] not in CATEGORIES:
        raise ValueError(f"{path.name}: unknown category {gt['category']}")
    return Scenario(
        name=data["name"],
        title=data["title"],
        duration_s=float(data.get("duration_s", 45)),
        rate=float(data.get("rate", 25)),
        events=tuple(sorted(events, key=lambda e: e.at_s)),
        ground_truth=GroundTruth(
            service=gt["service"],
            category=gt["category"],
            explanation=gt["explanation"],
            accept_services=tuple(gt.get("accept_services", [gt["service"]])),
            change=gt.get("change", "culprit"),
            fix_check=gt.get("fix_check", "rerun"),
        ),
        reference_fix=_edits(data.get("reference_fix", [])),
        path=path,
    )


def load_all(catalog: Path = CATALOG) -> list[Scenario]:
    return [load(p) for p in sorted(catalog.iterdir()) if (p / "scenario.toml").exists()]


def by_name(name: str, catalog: Path = CATALOG) -> Scenario:
    return load(catalog / name)
