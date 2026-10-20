// Shapes written by `ia export-ui` (src/incident_agent/evaluation/export.py).

export interface IndexEntry {
  id: string;
  backend: string;
  scenario: string;
  replicate: number;
  status: string;
  root_cause_correct: boolean;
  fix_verified: boolean | null;
  tool_calls: number;
  seconds: number;
}

export interface Step {
  index: number;
  tool: string;
  arguments: Record<string, unknown>;
  output: string;
  duration_ms: number;
  error: string | null;
}

export interface Report {
  summary: string;
  timeline: { time: string; event: string; evidence: string[] }[];
  hypotheses: { statement: string; verdict: "confirmed" | "rejected"; reasoning: string; evidence: string[] }[];
  root_cause: { service: string; category: string; change_id: string | null; summary: string; evidence: string[] };
  blast_radius: { services: string[]; impact: string; evidence: string[] };
  fix: { summary: string; diff: string };
}

export interface Verification {
  accepted: boolean;
  valid: string[];
  invalid: [string, string][];
  errors: string[];
  diff_applies: boolean;
  diff_error: string | null;
}

export interface Span {
  span_id: string;
  trace_id: string;
  parent_span_id: string | null;
  service: string;
  instance: string | null;
  version: string | null;
  name: string;
  kind: string;
  start_ns: number;
  duration_ms: number;
  status: string;
  status_message: string | null;
  http_status: number | null;
}

export interface Evidence {
  ref: string;
  kind: string;
  found: boolean;
  span?: Span;
  log?: { log_id: number; ts_ns: number; service: string; instance: string | null; severity: string; body: string; trace_id: string | null };
  change?: { change_id: string; ts_ns: number; service: string; kind: string; author: string; title: string; diff: string };
  span_count?: number;
  points?: number;
  lines?: number;
}

export interface Investigation {
  id: string;
  record: {
    backend: string;
    scenario: string;
    replicate: number;
    status: string;
    steps: Step[];
    report: Report | null;
    verification: Verification | null;
    submissions: number;
    turns: number;
    tool_calls: number;
    usage: { input_tokens: number; output_tokens: number };
    seconds: number;
    error: string | null;
    alert: { rule: string; service: string; summary: string; started_ns: number; fired_ns: number };
  };
  score: {
    root_cause_correct: boolean;
    service_ok: boolean;
    category_ok: boolean;
    change_ok: boolean;
    fix_verified: boolean | null;
    evidence_relevance: number | null;
  } | null;
  ground_truth: { service: string; category: string; change_ids: string[]; explanation: string };
  run: { start_ns: number; end_ns: number };
  changes: { change_id: string; ts_ns: number; service: string; kind: string; author: string; title: string }[];
  evidence: Evidence[];
  trace: { trace_id: string | null; spans: Span[] };
}
