import { useState } from "react";
import type { Step } from "../types";
import { compactJson, ms } from "../format";

function pretty(output: string): string {
  try {
    return JSON.stringify(JSON.parse(output), null, 2);
  } catch {
    return output;
  }
}

export function ToolTimeline({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (steps.length === 0) return <p className="empty">No tool calls (non-LLM baseline).</p>;
  return (
    <ol className="steps">
      {steps.map((step) => (
        <li key={step.index} className={step.error ? "step error" : "step"}>
          <button
            type="button"
            className="step-head"
            aria-expanded={open === step.index}
            onClick={() => setOpen(open === step.index ? null : step.index)}
          >
            <span className="step-index">{step.index}</span>
            <span className="step-tool">{step.tool}</span>
            <code className="step-args">{step.tool === "submit_report" ? "" : compactJson(step.arguments)}</code>
            <span className="step-time">{ms(step.duration_ms)}</span>
          </button>
          {open === step.index ? <pre className="step-output">{pretty(step.output)}</pre> : null}
        </li>
      ))}
    </ol>
  );
}
