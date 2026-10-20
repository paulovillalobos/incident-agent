"""The structured investigation report every backend must produce.

Evidence is cited with typed references that the verifier resolves against the
telemetry store and the system tree:

``span:<16 hex>``, ``trace:<32 hex>``, ``log:<integer id>``, ``change:<change id>``,
``metric:<metric name>`` and ``file:<path>`` (optionally ``file:<path>:<line>``).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

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


_REF = re.compile(r"^\s*(span|trace|log|change|metric|file):\s*([^\s,;]+)", re.I)


def _refs(value: object) -> object:
    """Keep the typed reference and drop commentary models append to it.

    ``"span:1795...1065: gateway request failed"`` becomes ``"span:1795...1065"``. Only the
    formatting is normalised: the id itself is still resolved by the verifier, and entries
    that do not start with a reference kind are kept as they are (and then rejected).
    """
    if not isinstance(value, list):
        return value
    out = []
    for item in value:
        match = _REF.match(item) if isinstance(item, str) else None
        if match:
            kind, ident = match[1].lower(), match[2].rstrip(":.)")
            out.append(f"{kind}:{ident}")
        else:
            out.append(item)
    return list(dict.fromkeys(out))


Refs = Annotated[list[str], BeforeValidator(_refs)]


class TimelineEntry(BaseModel):
    time: str = Field(description="UTC time HH:MM:SS(.mmm)")
    event: str
    evidence: Refs = Field(default_factory=list)


class Hypothesis(BaseModel):
    statement: str
    verdict: Literal["confirmed", "rejected"]
    reasoning: str
    evidence: Refs = Field(default_factory=list)


class RootCause(BaseModel):
    service: str = Field(description="Service (or external dependency) where the fault originates")
    category: Category
    change_id: str | None = Field(
        default=None, description="Change log id that caused it, or null if no change did"
    )
    summary: str
    evidence: Refs = Field(min_length=1)


class BlastRadius(BaseModel):
    services: list[str]
    impact: str
    evidence: Refs = Field(default_factory=list)


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
