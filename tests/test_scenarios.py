"""The scenario catalog is consistent: every edit applies, every fix applies after it."""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_agent import patching
from incident_agent.runner.execute import copy_tree
from incident_agent.scenarios import model as scenarios

ALL = scenarios.load_all()


def test_catalog_has_ten_distinct_categories() -> None:
    assert len(ALL) == 10
    assert len({s.ground_truth.category for s in ALL}) == 10


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.name)
def test_events_and_reference_fix_apply_in_order(
    scenario: scenarios.Scenario, tmp_path: Path
) -> None:
    tree = tmp_path / "system"
    copy_tree(scenarios.SYSTEM_TREE, tree)
    for event in scenario.events:
        if event.recorded:
            patching.apply_diff(tree, patching.make_diff(tree, list(event.edits)))
    assert scenario.reference_fix, "every scenario needs a reference fix"
    patching.apply_diff(tree, patching.make_diff(tree, list(scenario.reference_fix)))


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.name)
def test_ground_truth_is_consistent(scenario: scenarios.Scenario) -> None:
    gt = scenario.ground_truth
    culprit = scenario.culprit
    if gt.change == "culprit":
        assert culprit is not None and culprit.recorded
    else:
        assert culprit is None or not culprit.recorded
    assert gt.service in gt.accept_services
    # Every scenario has at least one decoy change so "blame the latest deploy" can fail.
    assert sum(1 for e in scenario.events if e.recorded and not e.culprit) >= 1


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.name)
def test_events_happen_within_the_run(scenario: scenarios.Scenario) -> None:
    assert all(0 < e.at_s < scenario.duration_s - 10 for e in scenario.events)
    for event in scenario.events:
        assert event.recorded or event.action, f"{event.title}: neither edits nor action"
