import { lineClass } from "../diff";

export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="empty">No diff.</p>;
  return (
    <pre className="diff" aria-label="diff">
      {diff.replace(/\n$/, "").split("\n").map((line, i) => (
        <div key={i} className={lineClass(line)}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}
