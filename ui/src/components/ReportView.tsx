import type { Investigation } from "../types";
import { DiffView } from "./DiffView";

interface Props {
  inv: Investigation;
  onSelect: (ref: string) => void;
}

function Refs({ refs, onSelect }: { refs: string[]; onSelect: (ref: string) => void }) {
  return (
    <span className="refs">
      {refs.map((ref) => (
        <button type="button" key={ref} className="ref" onClick={() => onSelect(ref)}>
          {ref}
        </button>
      ))}
    </span>
  );
}

export function ReportView({ inv, onSelect }: Props) {
  const report = inv.record.report;
  if (!report) return <p className="empty">No report was submitted ({inv.record.status}).</p>;
  const rc = report.root_cause;
  const v = inv.record.verification;
  return (
    <div className="report">
      <p className="lead">{report.summary}</p>
      <h3>Root cause</h3>
      <dl className="facts">
        <dt>Service</dt>
        <dd>{rc.service}</dd>
        <dt>Category</dt>
        <dd>{rc.category}</dd>
        <dt>Change</dt>
        <dd>{rc.change_id ?? "none (no change caused it)"}</dd>
      </dl>
      <p>{rc.summary}</p>
      <Refs refs={rc.evidence} onSelect={onSelect} />
      <h3>Timeline</h3>
      <ul className="timeline">
        {report.timeline.map((t, i) => (
          <li key={i}>
            <span className="time">{t.time}</span> {t.event} <Refs refs={t.evidence} onSelect={onSelect} />
          </li>
        ))}
      </ul>
      <h3>Hypotheses</h3>
      <ul className="hypotheses">
        {report.hypotheses.map((h, i) => (
          <li key={i}>
            <span className={h.verdict === "confirmed" ? "pill good" : "pill"}>{h.verdict}</span> {h.statement}.{" "}
            {h.reasoning} <Refs refs={h.evidence} onSelect={onSelect} />
          </li>
        ))}
      </ul>
      <h3>Blast radius</h3>
      <p>
        {report.blast_radius.services.join(", ")}: {report.blast_radius.impact}{" "}
        <Refs refs={report.blast_radius.evidence} onSelect={onSelect} />
      </p>
      <h3>Proposed fix</h3>
      <p>
        {report.fix.summary}{" "}
        {v && !v.diff_applies ? <span className="bad">(diff does not apply: {v.diff_error})</span> : null}
      </p>
      <DiffView diff={report.fix.diff} />
    </div>
  );
}
