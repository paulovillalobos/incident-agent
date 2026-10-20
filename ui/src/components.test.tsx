import { fireEvent, render, screen } from "@testing-library/react";
import { DiffView } from "./components/DiffView";
import { lineClass } from "./diff";
import { EvidenceList } from "./components/EvidenceList";
import { ToolTimeline } from "./components/ToolTimeline";
import { Waterfall } from "./components/Waterfall";
import { layout } from "./layout";
import type { Evidence, Span } from "./types";

const span = (id: string, parent: string | null, start: number, dur: number, status = "ok"): Span => ({
  span_id: id,
  trace_id: "t".repeat(32),
  parent_span_id: parent,
  service: "orders",
  instance: null,
  version: "1.7.4",
  name: `op-${id}`,
  kind: "INTERNAL",
  start_ns: start,
  duration_ms: dur,
  status,
  status_message: status === "error" ? "pool timeout" : null,
  http_status: null,
});

describe("Waterfall", () => {
  const spans = [span("a", null, 0, 100), span("b", "a", 10e6, 50, "error"), span("c", "b", 20e6, 10)];

  it("lays spans out relative to the trace and nests children", () => {
    const rows = layout(spans);
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 2]);
    expect(rows[0]?.offsetPct).toBe(0);
    expect(rows[0]?.widthPct).toBe(100);
    expect(rows[1]?.offsetPct).toBeCloseTo(10);
  });

  it("marks error and cited spans", () => {
    const { container } = render(<Waterfall spans={spans} cited={new Set(["b"])} />);
    expect(container.querySelectorAll(".wf-bar.error")).toHaveLength(1);
    expect(container.querySelector(".wf-row.cited")?.getAttribute("data-span")).toBe("b");
  });

  it("says so when no trace was cited", () => {
    render(<Waterfall spans={[]} cited={new Set()} />);
    expect(screen.getByText("No trace was cited.")).toBeInTheDocument();
  });
});

describe("DiffView", () => {
  it("classifies diff lines", () => {
    expect(["--- a/x", "+++ b/x", "@@ -1 +1 @@", "-old", "+new", " same"].map(lineClass)).toEqual([
      "diff-file", "diff-file", "diff-hunk", "diff-del", "diff-add", "diff-ctx",
    ]);
  });

  it("renders one row per line", () => {
    const { container } = render(<DiffView diff={"--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"} />);
    expect(container.querySelectorAll(".diff div")).toHaveLength(5);
  });
});

describe("EvidenceList", () => {
  const evidence: Evidence[] = [
    { ref: "span:b", kind: "span", found: true, span: span("b", "a", 0, 500, "error") },
    { ref: "span:0123456789abcdef", kind: "span", found: false },
    {
      ref: "change:chg-1",
      kind: "change",
      found: true,
      change: { change_id: "chg-1", ts_ns: 0, service: "orders", kind: "config", author: "joao", title: "pool", diff: "-pool_size = 10\n+pool_size = 2\n" },
    },
  ];
  const verification = {
    accepted: false,
    valid: ["span:b", "change:chg-1"],
    invalid: [["span:0123456789abcdef", "span not found in telemetry"]] as [string, string][],
    errors: [],
    diff_applies: true,
    diff_error: null,
  };

  it("shows why a reference was rejected", () => {
    render(<EvidenceList evidence={evidence} verification={verification} selected={null} onSelect={() => {}} />);
    expect(screen.getByText("rejected: span not found in telemetry")).toBeInTheDocument();
    expect(screen.getAllByText("verified")).toHaveLength(2);
  });

  it("shows a change's diff when selected", () => {
    const onSelect = vi.fn();
    const { container, rerender } = render(
      <EvidenceList evidence={evidence} verification={verification} selected={null} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByText("change:chg-1"));
    expect(onSelect).toHaveBeenCalledWith("change:chg-1");
    expect(container.querySelector(".diff")).toBeNull();
    rerender(<EvidenceList evidence={evidence} verification={verification} selected="change:chg-1" onSelect={onSelect} />);
    expect(container.querySelector(".diff-add")?.textContent).toBe("+pool_size = 2");
  });
});

describe("ToolTimeline", () => {
  it("expands a tool call to show its output", () => {
    const steps = [
      { index: 1, tool: "list_changes", arguments: {}, output: '{"changes":[]}', duration_ms: 1.2, error: null },
      { index: 2, tool: "run_sql", arguments: { query: "DELETE" }, output: "error: only SELECT", duration_ms: 0.3, error: "x" },
    ];
    const { container } = render(<ToolTimeline steps={steps} />);
    expect(container.querySelectorAll(".step.error")).toHaveLength(1);
    fireEvent.click(screen.getByText("list_changes"));
    expect(container.querySelector(".step-output")?.textContent).toContain('"changes": []');
  });
});
