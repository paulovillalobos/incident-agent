"""Read-only telemetry access and the investigation tools."""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import CULPRIT, Snapshot
from incident_agent.agent.tools import (
    INJECTION_WARNING,
    TOOLS,
    ToolContext,
    ToolError,
    render,
    run_tool,
)
from incident_agent.telemetry import store


@pytest.fixture
def ctx(snapshot: Snapshot) -> ToolContext:
    return ToolContext.open(snapshot.path)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM spans",
        "UPDATE logs SET body = 'x'",
        "INSERT INTO changes VALUES ('c', 0, 's', 'k', 'a', 't', NULL, NULL, '')",
        "DROP TABLE spans",
        "CREATE TABLE t (x)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA query_only = OFF",
    ],
)
def test_readonly_connection_rejects_writes(ro: sqlite3.Connection, statement: str) -> None:
    with pytest.raises(sqlite3.DatabaseError):
        ro.execute(statement)


def test_every_tool_has_an_object_schema() -> None:
    for tool in TOOLS:
        spec = tool.spec()
        assert spec.parameters["type"] == "object"
        assert "$defs" not in json.dumps(spec.parameters)


def test_span_stats_groups_by_service(ctx: ToolContext) -> None:
    out = run_tool(ctx, "span_stats", {"group_by": "service"})
    rows = {r["group"]: r for r in out["rows"]}
    assert rows["orders"]["errors"] == 2 * 18  # server + pool spans after t=22s
    assert rows["gateway"]["error_rate"] == pytest.approx(18 / 40, abs=1e-3)


def test_search_spans_returns_real_ids(ctx: ToolContext, snapshot: Snapshot) -> None:
    out = run_tool(ctx, "search_spans", {"service": "orders", "status": "error", "name": "pool"})
    assert out["matching"] == 18
    span = out["spans"][0]
    assert span["status_message"].startswith("could not acquire a connection")
    trace = run_tool(ctx, "get_trace", {"trace_id": span["trace_id"]})
    assert [s["depth"] for s in trace["spans"]] == [0, 1, 2]


def test_search_logs_by_trace_and_patterns(ctx: ToolContext, snapshot: Snapshot) -> None:
    out = run_tool(ctx, "search_logs", {"trace_id": snapshot.error_trace})
    assert [entry["severity"] for entry in out["logs"]] == ["ERROR"]
    patterns = run_tool(ctx, "search_logs", {"service": "orders", "mode": "patterns"})
    top = patterns["patterns"][0]
    assert top["count"] == 22 and "<n>" in top["pattern"]


def test_log_text_with_instructions_is_flagged_as_data(
    ctx: ToolContext, snapshot: Snapshot
) -> None:
    out = run_tool(ctx, "search_logs", {"service": "worker"})
    entry = out["logs"][0]
    assert entry["log_id"] == snapshot.poison_log
    assert entry["warning"] == INJECTION_WARNING
    # The body is returned as a JSON string value, never spliced into the prompt as text.
    assert json.loads(render(out, 5000))["logs"][0]["body"] == entry["body"]


def test_list_changes_and_diff(ctx: ToolContext) -> None:
    changes = run_tool(ctx, "list_changes", {})["changes"]
    assert [c["change_id"] for c in changes][-1] == CULPRIT
    detail = run_tool(ctx, "list_changes", {"change_id": CULPRIT})
    assert "-pool_size = 10" in detail["diff"] and "+pool_size = 2" in detail["diff"]
    with pytest.raises(ToolError):
        run_tool(ctx, "list_changes", {"change_id": "chg-missing"})


def test_metric_series_and_listing(ctx: ToolContext) -> None:
    listing = run_tool(ctx, "metric_series", {})
    assert {m["name"] for m in listing["metrics"]} == {"db.pool.in_use", "db.pool.timeouts"}
    series = run_tool(ctx, "metric_series", {"name": "db.pool.timeouts", "bucket_s": 5})
    assert series["series"]["orders"][-1]["sum"] == 5.0


def test_read_file_is_confined_to_the_tree(ctx: ToolContext) -> None:
    out = run_tool(ctx, "read_file", {"path": "config/orders.toml"})
    assert "pool_size = 2" in out["content"]
    with pytest.raises(ToolError):
        run_tool(ctx, "read_file", {"path": "../telemetry.db"})
    with pytest.raises(ToolError):
        run_tool(ctx, "read_file", {"path": "config/../../ground_truth.json"})
    assert "ground_truth" not in json.dumps(run_tool(ctx, "read_file", {"path": "."}))


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM spans",
        "SELECT 1; DELETE FROM spans",
        "WITH x AS (SELECT 1) DELETE FROM spans",
        "PRAGMA table_list",
        "SELECT * FROM run_meta; SELECT 1",
    ],
)
def test_run_sql_rejects_non_select(ctx: ToolContext, query: str) -> None:
    with pytest.raises(ToolError):
        run_tool(ctx, "run_sql", {"query": query})


def test_run_sql_caps_rows(ctx: ToolContext) -> None:
    out = run_tool(ctx, "run_sql", {"query": "SELECT span_id FROM spans"})
    assert len(out["rows"]) == 50 and out["truncated"] is True


def test_run_sql_deadline_cancels_runaway_query(ctx: ToolContext) -> None:
    ctx.sql_timeout_s = 0.2
    runaway = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n"
    )
    with pytest.raises(ToolError, match="cancelled"):
        run_tool(ctx, "run_sql", {"query": runaway})


def test_unknown_tool_and_bad_arguments_are_tool_errors(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="unknown tool"):
        run_tool(ctx, "rm_rf", {})
    with pytest.raises(ToolError, match="invalid arguments"):
        run_tool(ctx, "search_spans", {"limit": 1000})


def test_render_trims_lists_to_fit() -> None:
    result = {"rows": [{"i": i, "pad": "x" * 50} for i in range(100)], "note": "kept"}
    text = render(result, 600)
    parsed = json.loads(text)
    assert len(text) <= 600 and parsed["note"] == "kept" and parsed["truncated_items"] > 0


def test_open_readonly_requires_existing_file(tmp_path: object) -> None:
    from pathlib import Path

    with pytest.raises(FileNotFoundError):
        store.open_readonly(Path(str(tmp_path)) / "missing.db")


@pytest.mark.parametrize(
    "value", ["08:53:20", "08:53:20:00", "08:53:20Z", "2025-10-09T08:53:20", "08:53:20 UTC"]
)
def test_time_arguments_are_parsed_leniently(ctx: ToolContext, value: str) -> None:
    assert ctx.parse_time(value, 0) % (86400 * 10**9) == (8 * 3600 + 53 * 60 + 20) * 10**9


@pytest.mark.parametrize("value", ["yesterday", "25:00", "08:61"])
def test_unparseable_times_are_tool_errors(ctx: ToolContext, value: str) -> None:
    with pytest.raises(ToolError):
        ctx.parse_time(value, 0)


def test_unknown_names_and_ids_say_what_exists(ctx: ToolContext) -> None:
    out = run_tool(ctx, "search_logs", {"service": "orders", "instance": "orders-a"})
    assert out["matching"] == 0 and "no instance named 'orders-a'" in out["hint"]
    with pytest.raises(ToolError, match=CULPRIT):
        run_tool(ctx, "list_changes", {"change_id": "2023-10-05-123456"})
    assert "pool_size" in run_tool(ctx, "read_file", {"path": "a/config/orders.toml"})["content"]


def test_malformed_or_unknown_arguments_are_errors_not_ignored(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="must be a JSON object"):
        run_tool(ctx, "search_logs", {"_raw": "service:orders severity:ERROR"})
    with pytest.raises(ToolError, match="servce"):
        run_tool(ctx, "search_logs", {"servce": "orders"})
