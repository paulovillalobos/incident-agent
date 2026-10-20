"""Create and apply unified diffs against a system tree.

Scenario changes and reference fixes are written as exact text edits and rendered to
unified diffs with :func:`make_diff`. Proposed fixes from the agent are applied with
:func:`apply_diff`, which matches hunks by their context and removed lines and ignores
the line numbers and counts in ``@@`` headers (language models often get those wrong;
what is evaluated is the content of the change). Paths must stay inside the tree.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class PatchError(ValueError):
    """The diff is malformed, touches a forbidden path, or does not match the tree."""


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[list[str]] = field(default_factory=list)


def make_diff(root: Path, edits: list[Edit]) -> str:
    """Render exact-text edits as a unified diff (``a/``/``b/`` prefixes). Does not write."""
    by_path: dict[str, str] = {}
    originals: dict[str, str] = {}
    for edit in edits:
        target = root / edit.path
        if edit.path not in by_path:
            originals[edit.path] = target.read_text() if target.exists() else ""
            by_path[edit.path] = originals[edit.path]
        current = by_path[edit.path]
        if edit.old:
            if current.count(edit.old) != 1:
                raise PatchError(f"edit for {edit.path} must match exactly once: {edit.old[:60]!r}")
            by_path[edit.path] = current.replace(edit.old, edit.new, 1)
        else:
            by_path[edit.path] = current + edit.new
    chunks = []
    for path, new_text in by_path.items():
        old_text = originals[path]
        fromfile = f"a/{path}" if old_text else "/dev/null"
        chunks.append(
            "".join(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=fromfile,
                    tofile=f"b/{path}",
                    n=2,
                )
            )
        )
    return "".join(chunks)


def reverse_diff(diff: str) -> str:
    """Invert a unified diff (the rollback of a change)."""
    out = []
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = line[4:], lines[i + 1][4:]
            out.append("--- " + (new.replace("b/", "a/", 1) if new != "/dev/null" else new))
            out.append("+++ " + (old.replace("a/", "b/", 1) if old != "/dev/null" else old))
            i += 2
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\S+) \+(\S+) @@(.*)", line)
            out.append(f"@@ -{m.group(2)} +{m.group(1)} @@{m.group(3)}" if m else line)
        elif line.startswith("+"):
            out.append("-" + line[1:])
        elif line.startswith("-"):
            out.append("+" + line[1:])
        else:
            out.append(line)
        i += 1
    return "\n".join(out) + "\n"


def _clean_path(raw: str) -> str | None:
    raw = raw.strip().split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PatchError(f"path outside the system tree: {raw!r}")
    return str(path)


def parse_diff(diff: str) -> list[FilePatch]:
    text = diff.strip("\n")
    fence = re.match(r"^```[a-z]*\n(.*)\n```$", text, re.S)
    if fence:
        text = fence.group(1)
    lines = text.splitlines()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: list[str] | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_clean_path(line[4:]), _clean_path(lines[i + 1][4:]))
            patches.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith("@@"):
            if current is None:
                raise PatchError("hunk before file header")
            hunk = []
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "+", "-"):
            hunk.append(line)
        elif hunk is not None and line == "":
            hunk.append(" ")
        elif line.startswith(("diff ", "index ", "\\ No newline")) or not line.strip():
            pass
        elif hunk is not None:
            raise PatchError(f"unexpected line in hunk: {line[:80]!r}")
        i += 1
    if not patches:
        raise PatchError("no file headers (--- / +++) found")
    for p in patches:
        for h in p.hunks:
            while h and h[-1] == " ":
                h.pop()
        if not p.hunks:
            raise PatchError(f"no hunks for {p.new_path or p.old_path}")
    return patches


def _apply_hunks(original: list[str], hunks: list[list[str]], path: str) -> list[str]:
    result = list(original)
    cursor = 0
    for hunk in hunks:
        before = [ln[1:] for ln in hunk if ln[:1] in (" ", "-")]
        after = [ln[1:] for ln in hunk if ln[:1] in (" ", "+")]
        if not before:
            result[cursor:cursor] = after
            cursor += len(after)
            continue
        match = _find(result, before, cursor)
        if match is None:
            match = _find(result, before, 0)
        if match is None:
            raise PatchError(f"hunk does not match {path}: {before[0][:60]!r}")
        result[match : match + len(before)] = after
        cursor = match + len(after)
    return result


def _find(haystack: list[str], needle: list[str], start: int) -> int | None:
    stripped = [n.rstrip() for n in needle]
    for idx in range(start, len(haystack) - len(needle) + 1):
        if [h.rstrip() for h in haystack[idx : idx + len(needle)]] == stripped:
            return idx
    return None


def apply_diff(root: Path, diff: str, *, dry_run: bool = False) -> list[str]:
    """Apply ``diff`` to files under ``root``; return the paths changed."""
    changes: dict[Path, str | None] = {}
    for patch in parse_diff(diff):
        path = patch.new_path or patch.old_path
        assert path is not None
        target = root / (patch.old_path or path)
        if patch.old_path is None:
            if target.exists() and target.read_text().strip():
                raise PatchError(f"file already exists: {path}")
            original: list[str] = []
        else:
            if not target.is_file():
                raise PatchError(f"no such file: {patch.old_path}")
            original = target.read_text().splitlines()
        updated = _apply_hunks(original, patch.hunks, path)
        if patch.new_path is None:
            changes[target] = None
        else:
            changes[root / patch.new_path] = "\n".join(updated) + "\n"
    if not dry_run:
        for target, content in changes.items():
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    return sorted(str(p.relative_to(root)) for p in changes)
