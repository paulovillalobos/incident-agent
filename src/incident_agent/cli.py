"""Command line: ``ia scenarios``, ``ia investigate``, ``ia eval``, ``ia export-ui``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from incident_agent.agent.backends import (
    BASELINES,
    LLM_BACKENDS,
    BackendUnavailableError,
    make_model,
)
from incident_agent.evaluation import harness
from incident_agent.scenarios import model as scenarios


def _names(raw: list[str] | None) -> list[str]:
    all_names = [s.name for s in scenarios.load_all()]
    if not raw:
        return all_names
    unknown = set(raw) - set(all_names)
    if unknown:
        sys.exit(f"unknown scenarios: {', '.join(sorted(unknown))}")
    return raw


def cmd_scenarios(args: argparse.Namespace) -> None:
    if args.action == "list":
        for s in scenarios.load_all():
            print(f"{s.name:22s} {s.ground_truth.category:28s} {s.title}")
        return
    for name in _names(args.names):
        for r in range(1, args.replicates + 1):
            out = harness.make_snapshot(scenarios.by_name(name), r, force=args.force)
            fired = json.loads((out / "alerts.json").read_text())
            first = fired[0]["rule"] if fired else "NO ALERT"
            print(
                f"{name:22s} r{r} -> {out.relative_to(scenarios.REPO_ROOT)} ({first})", flush=True
            )


def cmd_investigate(args: argparse.Namespace) -> None:
    snapshot = Path(args.snapshot)
    model = None if args.backend in BASELINES else make_model(args.backend)
    label = args.backend if model is None else model.name
    record = harness.investigate_snapshot(label, model, snapshot, args.seed)
    text = json.dumps(record, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
    report = record["report"] or {}
    print(
        f"status: {record['status']}  tool calls: {record['tool_calls']}"
        f"  seconds: {record['seconds']}"
    )
    if report:
        rc = report["root_cause"]
        print(f"root cause: {rc['service']} / {rc['category']} / change {rc['change_id']}")
        print(rc["summary"])


def cmd_eval(args: argparse.Namespace) -> None:
    names = _names(args.scenarios)
    replicates = list(range(1, args.replicates + 1))
    if args.stage == "snapshots":
        for name in names:
            for r in replicates:
                harness.make_snapshot(scenarios.by_name(name), r)
                print(f"snapshot {name} r{r}", flush=True)
    elif args.stage == "investigate":
        backends = []
        for b in args.backends.split(","):
            if b in LLM_BACKENDS:
                try:
                    make_model_check(b)
                except BackendUnavailableError as exc:
                    print(f"skipping {b}: {exc}")
                    continue
            backends.append(b)
        harness.investigate_all(backends, names, replicates, log=lambda m: print(m, flush=True))
    elif args.stage == "verify":
        harness.verify_fixes(log=lambda m: print(m, flush=True))
    elif args.stage == "controls":
        rows = harness.run_controls(names, log=lambda m: print(m, flush=True))
        harness.write_results(harness.score_all(), controls=rows)
    elif args.stage == "report":
        controls_path = harness.RESULTS / "controls.json"
        controls = json.loads(controls_path.read_text()) if controls_path.exists() else None
        harness.write_results(harness.score_all(), controls=controls)
        print((harness.RESULTS / "summary.md").read_text())


def make_model_check(backend: str) -> None:
    """Fail fast on a missing key without loading a model."""
    if backend == "anthropic":
        from incident_agent.agent.backends import load_dotenv

        load_dotenv()
        import os

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise BackendUnavailableError("ANTHROPIC_API_KEY is not set in the environment or .env")
    if backend == "mlx":
        try:
            import mlx_lm  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailableError("mlx-lm is not installed (uv sync --extra mlx)") from exc


def cmd_export_ui(args: argparse.Namespace) -> None:
    from incident_agent.evaluation.export import export

    paths = export(Path(args.out))
    print(f"wrote {len(paths)} investigations to {args.out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ia", description="Incident investigation agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scenarios", help="list scenarios or record telemetry snapshots")
    p.add_argument("action", choices=["list", "run"])
    p.add_argument("names", nargs="*")
    p.add_argument("--replicates", type=int, default=1)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_scenarios)

    p = sub.add_parser("investigate", help="investigate one snapshot")
    p.add_argument("snapshot")
    p.add_argument("--backend", default="rules", choices=[*BASELINES, *LLM_BACKENDS])
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_investigate)

    p = sub.add_parser("eval", help="run the evaluation in stages")
    p.add_argument("stage", choices=["snapshots", "investigate", "verify", "controls", "report"])
    p.add_argument("--backends", default="rules,rollback,mlx")
    p.add_argument("--scenarios", nargs="*")
    p.add_argument("--replicates", type=int, default=3)
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("export-ui", help="write investigation JSON for the UI")
    p.add_argument("--out", default="ui/public/data")
    p.set_defaults(fn=cmd_export_ui)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
