import type { Evidence, Verification } from "../types";
import { clock, ms } from "../format";
import { DiffView } from "./DiffView";

interface Props {
  evidence: Evidence[];
  verification: Verification | null;
  selected: string | null;
  onSelect: (ref: string) => void;
}

function Detail({ item }: { item: Evidence }) {
  if (item.span) {
    const s = item.span;
    return (
      <span>
        {clock(s.start_ns)} · {s.instance ?? s.service} · {s.name} · {ms(s.duration_ms)} ·{" "}
        <span className={s.status === "error" ? "bad" : undefined}>{s.status}</span>
        {s.status_message ? ` · ${s.status_message}` : ""}
      </span>
    );
  }
  if (item.log) {
    const l = item.log;
    return (
      <span>
        {clock(l.ts_ns)} · {l.instance ?? l.service} · {l.severity} · <code>{l.body}</code>
      </span>
    );
  }
  if (item.change) {
    return (
      <span>
        {clock(item.change.ts_ns)} · {item.change.service} · {item.change.title} ({item.change.author})
      </span>
    );
  }
  if (item.span_count !== undefined) return <span>{item.span_count} spans</span>;
  if (item.points !== undefined) return <span>{item.points} metric points</span>;
  if (item.lines !== undefined) return <span>{item.lines} lines</span>;
  return null;
}

export function EvidenceList({ evidence, verification, selected, onSelect }: Props) {
  const reasons = new Map(verification?.invalid ?? []);
  return (
    <ul className="evidence">
      {evidence.map((item) => (
        <li
          key={item.ref}
          className={`${item.found ? "found" : "missing"}${selected === item.ref ? " selected" : ""}`}
        >
          <button type="button" className="ref" onClick={() => onSelect(item.ref)}>
            {item.ref}
          </button>
          <span className={item.found ? "ok-mark" : "bad"}>
            {item.found ? "verified" : `rejected: ${reasons.get(item.ref) ?? "not found"}`}
          </span>
          <div className="evidence-detail">
            <Detail item={item} />
          </div>
          {item.change && selected === item.ref ? <DiffView diff={item.change.diff} /> : null}
        </li>
      ))}
    </ul>
  );
}
