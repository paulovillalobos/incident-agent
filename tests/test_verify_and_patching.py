"""Citation verification and unified-diff handling."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import CULPRIT, Snapshot, fix_diff, make_report
from incident_agent import patching
from incident_agent.agent.report import Report
from incident_agent.agent.verify import check_reference, verify
from incident_agent.patching import Edit, PatchError


def test_correct_report_is_accepted(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    result = verify(Report.model_validate(make_report(snapshot)), ro, snapshot.tree)
    assert result.accepted, result.feedback()
    assert result.diff_applies and result.diff_files == ["config/orders.toml"]


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ("span:ffffffffffffffff", "span not found"),
        ("span:xyz", "malformed span id"),
        ("trace:" + "0" * 32, "trace not found"),
        ("log:999999", "log not found"),
        ("change:chg-nothere", "change not found"),
        ("metric:cpu.magic", "metric not found"),
        ("file:config/nope.toml", "no such file"),
        ("file:../telemetry.db", "outside the system tree"),
        ("file:config/orders.toml:9999", "beyond end of file"),
        ("orders is slow", "not a typed reference"),
        ("pod:orders-1", "unknown reference kind"),
    ],
)
def test_bad_references_are_explained(
    snapshot: Snapshot, ro: sqlite3.Connection, ref: str, reason: str
) -> None:
    assert reason in (check_reference(ref, ro, snapshot.tree) or "")


def test_fabricated_span_rejects_the_report(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    report = make_report(snapshot, evidence=["span:0123456789abcdef", f"change:{CULPRIT}"])
    result = verify(Report.model_validate(report), ro, snapshot.tree)
    assert not result.accepted
    assert ("span:0123456789abcdef", "span not found in telemetry") in result.invalid


def test_root_cause_needs_telemetry_evidence(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    report = make_report(snapshot, evidence=[f"change:{CULPRIT}", "file:config/orders.toml"])
    result = verify(Report.model_validate(report), ro, snapshot.tree)
    assert not result.accepted
    assert any("at least one existing span" in e for e in result.errors)


def test_unknown_change_id_rejected(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    result = verify(
        Report.model_validate(make_report(snapshot, change_id="chg-made-up")), ro, snapshot.tree
    )
    assert not result.accepted and "not in the change log" in result.feedback()


def test_diff_that_does_not_apply_is_reported(snapshot: Snapshot, ro: sqlite3.Connection) -> None:
    report = make_report(snapshot)
    report["fix"]["diff"] = report["fix"]["diff"].replace("-pool_size = 2", "-pool_size = 3")
    result = verify(Report.model_validate(report), ro, snapshot.tree)
    assert result.accepted and not result.diff_applies  # fix scoring, not acceptance


def test_make_apply_reverse_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    original = "a = 1\nb = 2\nc = 3\nd = 4\n"
    (tmp_path / "config" / "x.toml").write_text(original)
    diff = patching.make_diff(tmp_path, [Edit("config/x.toml", "b = 2", "b = 20")])
    assert patching.apply_diff(tmp_path, diff, dry_run=True) == ["config/x.toml"]
    assert (tmp_path / "config" / "x.toml").read_text() == original
    patching.apply_diff(tmp_path, diff)
    assert "b = 20" in (tmp_path / "config" / "x.toml").read_text()
    rollback = patching.reverse_diff(diff)
    assert rollback.index("-b = 20") < rollback.index("+b = 2\n")
    patching.apply_diff(tmp_path, rollback)
    assert (tmp_path / "config" / "x.toml").read_text() == original


def test_apply_diff_refuses_paths_outside_the_tree(tmp_path: Path) -> None:
    evil = "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -0,0 +1 @@\n+pwned\n"
    with pytest.raises(PatchError):
        patching.apply_diff(tmp_path, evil)
    assert not (tmp_path.parent / "outside.txt").exists()


def test_model_style_diff_without_exact_line_numbers_applies(snapshot: Snapshot) -> None:
    diff = fix_diff(snapshot.tree)
    sloppy = "\n".join(
        "@@ -1,1 +1,1 @@" if line.startswith("@@") else line for line in diff.splitlines()
    )
    copy = snapshot.path.parent / "sloppy"
    import shutil

    shutil.copytree(snapshot.tree, copy, dirs_exist_ok=True)
    assert patching.apply_diff(copy, sloppy + "\n") == ["config/orders.toml"]


def test_commentary_after_a_reference_is_dropped_but_the_id_is_still_checked(
    snapshot: Snapshot, ro: sqlite3.Connection
) -> None:
    report = make_report(
        snapshot,
        evidence=[
            f"span:{snapshot.error_span}: pool acquire timed out after 500ms",
            "LOG:999999 (could not acquire)",
            f"span:<{snapshot.error_span}>",
            "orders pool is small",
        ],
    )
    parsed = Report.model_validate(report)
    assert parsed.root_cause.evidence == [
        f"span:{snapshot.error_span}",
        "log:999999",
        "orders pool is small",
    ]
    result = verify(parsed, ro, snapshot.tree)
    assert {ref for ref, _ in result.invalid} == {"log:999999", "orders pool is small"}
