import type { Span } from "./types";

export interface Row {
  span: Span;
  depth: number;
  offsetPct: number;
  widthPct: number;
}

export function layout(spans: Span[]): Row[] {
  if (spans.length === 0) return [];
  const start = Math.min(...spans.map((s) => s.start_ns));
  const end = Math.max(...spans.map((s) => s.start_ns + s.duration_ms * 1e6));
  const total = Math.max(end - start, 1);
  const byId = new Map(spans.map((s) => [s.span_id, s]));
  const depthOf = (s: Span, seen = 0): number => {
    const parent = s.parent_span_id ? byId.get(s.parent_span_id) : undefined;
    return parent && seen < 50 ? depthOf(parent, seen + 1) + 1 : 0;
  };
  return spans.map((span) => ({
    span,
    depth: depthOf(span),
    offsetPct: ((span.start_ns - start) / total) * 100,
    widthPct: Math.max((span.duration_ms * 1e6 * 100) / total, 0.4),
  }));
}
