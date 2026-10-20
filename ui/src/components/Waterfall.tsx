import type { Span } from "../types";
import { ms } from "../format";
import { layout } from "../layout";

interface Props {
  spans: Span[];
  cited: Set<string>;
}

export function Waterfall({ spans, cited }: Props) {
  const rows = layout(spans);
  if (rows.length === 0) return <p className="empty">No trace was cited.</p>;
  return (
    <div className="waterfall" role="table" aria-label="trace waterfall">
      {rows.map(({ span, depth, offsetPct, widthPct }) => (
        <div
          key={span.span_id}
          role="row"
          className={`wf-row${cited.has(span.span_id) ? " cited" : ""}`}
          data-span={span.span_id}
        >
          <div className="wf-label" style={{ paddingLeft: depth * 12 }} role="cell">
            <span className="wf-service">{span.instance ?? span.service}</span> {span.name}
          </div>
          <div className="wf-track" role="cell">
            <div
              className={`wf-bar ${span.status === "error" ? "error" : "ok"}`}
              style={{ left: `${offsetPct}%`, width: `${widthPct}%` }}
              title={span.status_message ?? span.name}
            />
          </div>
          <div className="wf-duration" role="cell">
            {ms(span.duration_ms)}
          </div>
        </div>
      ))}
    </div>
  );
}
