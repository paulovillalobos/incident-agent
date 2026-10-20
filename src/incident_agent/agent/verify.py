"""Check every citation in a report against the telemetry store and the system tree.

A report is accepted only if every reference resolves, the root cause cites at least one
piece of telemetry (span, trace, log or metric), and a cited change id exists. Whether the
proposed diff applies is checked too, but that affects fix scoring, not acceptance.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from incident_agent import patching
from incident_agent.agent.report import Report

TELEMETRY_KINDS = {"span", "trace", "log", "metric"}
_PATTERNS = {
    "span": re.compile(r"^[0-9a-f]{16}$"),
    "trace": re.compile(r"^[0-9a-f]{32}$"),
    "log": re.compile(r"^\d+$"),
}


@dataclass
class Verification:
    accepted: bool
    valid: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    diff_applies: bool = False
    diff_error: str | None = None
    diff_files: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        lines = [*self.errors]
        lines += [f"{ref}: {reason}" for ref, reason in self.invalid]
        if not self.diff_applies:
            lines.append(f"fix.diff does not apply: {self.diff_error}")
        return "\n".join(lines)


def check_reference(ref: str, conn: sqlite3.Connection, tree: Path) -> str | None:
    """Return ``None`` if ``ref`` resolves, else the reason it does not."""
    kind, sep, value = ref.strip().partition(":")
    if not sep or not value:
        return "not a typed reference (expected kind:id)"
    kind = kind.lower()
    if kind in _PATTERNS and not _PATTERNS[kind].match(value):
        return f"malformed {kind} id"
    if kind == "span":
        row = conn.execute("SELECT 1 FROM spans WHERE span_id = ?", (value,)).fetchone()
    elif kind == "trace":
        row = conn.execute("SELECT 1 FROM spans WHERE trace_id = ? LIMIT 1", (value,)).fetchone()
    elif kind == "log":
        row = conn.execute("SELECT 1 FROM logs WHERE log_id = ?", (int(value),)).fetchone()
    elif kind == "change":
        row = conn.execute("SELECT 1 FROM changes WHERE change_id = ?", (value,)).fetchone()
    elif kind == "metric":
        name = value.split("@")[0]
        row = conn.execute("SELECT 1 FROM metric_points WHERE name = ? LIMIT 1", (name,)).fetchone()
    elif kind == "file":
        path, _, line = value.partition(":")
        rel = PurePosixPath(path.removeprefix("./"))
        if ".." in rel.parts or rel.is_absolute():
            return "path outside the system tree"
        target = tree / rel
        if not target.is_file():
            return "no such file"
        if line:
            if not line.split("-")[0].isdigit():
                return "malformed line number"
            if int(line.split("-")[0]) > len(target.read_text().splitlines()):
                return "line beyond end of file"
        return None
    else:
        return f"unknown reference kind '{kind}'"
    return None if row else f"{kind} not found in telemetry"


def verify(report: Report, conn: sqlite3.Connection, tree: Path) -> Verification:
    result = Verification(accepted=False)
    for ref in dict.fromkeys(report.references()):
        reason = check_reference(ref, conn, tree)
        if reason is None:
            result.valid.append(ref)
        else:
            result.invalid.append((ref, reason))
    rc = report.root_cause
    if not any(
        r.split(":")[0].lower() in TELEMETRY_KINDS and r in result.valid for r in rc.evidence
    ):
        result.errors.append(
            "root_cause.evidence must cite at least one existing span, trace, log or metric"
        )
    if rc.change_id is not None:
        cid = rc.change_id.removeprefix("change:")
        if not conn.execute("SELECT 1 FROM changes WHERE change_id = ?", (cid,)).fetchone():
            result.errors.append(f"root_cause.change_id {rc.change_id} is not in the change log")
    result.accepted = not result.invalid and not result.errors

    if report.fix.diff.strip():
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "system"
            shutil.copytree(tree, copy)
            try:
                result.diff_files = patching.apply_diff(copy, report.fix.diff)
                result.diff_applies = True
            except patching.PatchError as exc:
                result.diff_error = str(exc)
    else:
        result.diff_error = "empty diff"
    return result
