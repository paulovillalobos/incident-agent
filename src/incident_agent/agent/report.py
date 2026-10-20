"""The structured investigation report every backend must produce.

Evidence is cited with typed references that the verifier resolves against the
telemetry store and the system tree:

``span:<16 hex>``, ``trace:<32 hex>``, ``log:<integer id>``, ``change:<change id>``,
``metric:<metric name>`` and ``file:<path>`` (optionally ``file:<path>:<line>``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from incident_agent.scenarios.model import CATEGORIES

Category = Literal[
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
]
assert set(Category.__args__) == set(CATEGORIES)  # type: ignore[attr-defined]


class TimelineEntry(BaseModel):
    time: str = Field(description="UTC time HH:MM:SS(.mmm)")
    event: str
    evidence: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    statement: str
    verdict: Literal["confirmed", "rejected"]
    reasoning: str
    evidence: list[str] = Field(default_factory=list)


class RootCause(BaseModel):
    service: str = Field(description="Service (or external dependency) where the fault originates")
    category: Category
    change_id: str | None = Field(
        default=None, description="Change log id that caused it, or null if no change did"
    )
    summary: str
    evidence: list[str] = Field(min_length=1)


class BlastRadius(BaseModel):
    services: list[str]
    impact: str
    evidence: list[str] = Field(default_factory=list)


class ProposedFix(BaseModel):
    summary: str
    diff: str = Field(description="Unified diff against the system tree (a/ and b/ prefixes)")


class Report(BaseModel):
    summary: str
    timeline: list[TimelineEntry]
    hypotheses: list[Hypothesis]
    root_cause: RootCause
    blast_radius: BlastRadius
    fix: ProposedFix

    def references(self) -> list[str]:
        refs: list[str] = []
        for entry in self.timeline:
            refs.extend(entry.evidence)
        for hyp in self.hypotheses:
            refs.extend(hyp.evidence)
        refs.extend(self.root_cause.evidence)
        refs.extend(self.blast_radius.evidence)
        return refs
