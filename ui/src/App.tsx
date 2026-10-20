import { useEffect, useMemo, useState } from "react";
import type { IndexEntry, Investigation } from "./types";
import { clock } from "./format";
import { EvidenceList } from "./components/EvidenceList";
import { ReportView } from "./components/ReportView";
import { ToolTimeline } from "./components/ToolTimeline";
import { Waterfall } from "./components/Waterfall";

const DATA = "./data";

function Verdict({ inv }: { inv: Investigation }) {
  const s = inv.score;
  return (
    <div className="verdicts">
      <span className={inv.record.status === "accepted" ? "pill good" : "pill bad-pill"}>
        report {inv.record.status}
      </span>
      <span className={s?.root_cause_correct ? "pill good" : "pill bad-pill"}>
        root cause {s?.root_cause_correct ? "correct" : "wrong"}
      </span>
      <span className={s?.fix_verified ? "pill good" : "pill"}>
        fix {s?.fix_verified ? "verified by re-run" : s?.fix_verified === false ? "failed re-run" : "not verified"}
      </span>
    </div>
  );
}

export function App() {
  const [index, setIndex] = useState<IndexEntry[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [inv, setInv] = useState<Investigation | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${DATA}/index.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`index.json: ${r.status}`))))
      .then((entries: IndexEntry[]) => {
        setIndex(entries);
        const hash = window.location.hash.slice(1);
        setCurrent(entries.find((e) => e.id === hash)?.id ?? entries[0]?.id ?? null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!current) return;
    window.location.hash = current;
    setSelected(null);
    fetch(`${DATA}/${current}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${current}: ${r.status}`))))
      .then((doc: Investigation) => setInv(doc))
      .catch((e: Error) => setError(e.message));
  }, [current]);

  const scenarios = useMemo(() => [...new Set(index.map((e) => e.scenario))], [index]);
  const cited = useMemo(
    () => new Set((inv?.evidence ?? []).flatMap((e) => (e.span ? [e.span.span_id] : []))),
    [inv],
  );

  if (error) return <main className="page"><p className="bad">Could not load data: {error}</p></main>;

  return (
    <div className="layout">
      <nav className="sidebar" aria-label="investigations">
        <h1>incident-agent</h1>
        {scenarios.map((name) => (
          <section key={name}>
            <h2>{name}</h2>
            {index
              .filter((e) => e.scenario === name)
              .map((e) => (
                <button
                  type="button"
                  key={e.id}
                  className={`nav-item${e.id === current ? " active" : ""}`}
                  onClick={() => setCurrent(e.id)}
                >
                  <span>{e.backend}</span>
                  <span className={e.root_cause_correct ? "good-text" : "bad"}>
                    {e.root_cause_correct ? "correct" : e.status === "accepted" ? "wrong" : e.status}
                  </span>
                </button>
              ))}
          </section>
        ))}
      </nav>
      <main className="page">
        {inv ? (
          <>
            <header className="page-head">
              <p className="eyebrow">
                {inv.record.scenario} · replicate {inv.record.replicate} · {inv.record.backend}
              </p>
              <h2>
                PAGE {inv.record.alert.rule} on {inv.record.alert.service}
              </h2>
              <p>
                {inv.record.alert.summary} · fired {clock(inv.record.alert.fired_ns)} UTC ·{" "}
                {inv.record.tool_calls} tool calls · {inv.record.seconds.toFixed(1)} s
                {inv.record.usage.input_tokens ? ` · ${inv.record.usage.input_tokens.toLocaleString()} tokens in` : ""}
              </p>
              <Verdict inv={inv} />
            </header>
            <div className="grid">
              <section className="card">
                <h2>Report</h2>
                <ReportView inv={inv} onSelect={setSelected} />
              </section>
              <div className="column">
                <section className="card">
                  <h2>Tool calls</h2>
                  <ToolTimeline steps={inv.record.steps} />
                </section>
                <section className="card">
                  <h2>Evidence</h2>
                  <EvidenceList
                    evidence={inv.evidence}
                    verification={inv.record.verification}
                    selected={selected}
                    onSelect={setSelected}
                  />
                </section>
                <section className="card">
                  <h2>Cited trace {inv.trace.trace_id ? <code>{inv.trace.trace_id}</code> : null}</h2>
                  <Waterfall spans={inv.trace.spans} cited={cited} />
                </section>
                <section className="card">
                  <h2>Ground truth</h2>
                  <p>
                    {inv.ground_truth.service} / {inv.ground_truth.category} /{" "}
                    {inv.ground_truth.change_ids.join(", ") || "no change"}
                  </p>
                  <p>{inv.ground_truth.explanation}</p>
                </section>
              </div>
            </div>
          </>
        ) : (
          <p>Loading…</p>
        )}
      </main>
    </div>
  );
}
