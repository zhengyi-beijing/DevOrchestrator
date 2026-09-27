"""Standalone CLI runner for P17 replay and shadow evaluation.

Deliberately isolated from src/dev_orchestrator/cli.py and production command dispatch.
Invoked via:
    python -m dev_orchestrator.convergence replay [--corpus PATH] [--json]
    python -m dev_orchestrator.convergence shadow --evidence-root PATH (--stdout | --out DIR) [--json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from dev_orchestrator.convergence.replay import ReplayCase, ReplayHarness
from dev_orchestrator.convergence.shadow import (
    ReadOnlyEvidenceRoot,
    ShadowEvaluator,
    validate_shadow_sink,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_orchestrator.convergence",
        description="Single-Authority Convergence Prototype (Replay & Shadow)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Replay subcommand
    replay_p = subparsers.add_parser("replay", help="Run incident corpus replay harness")
    replay_p.add_argument(
        "--corpus",
        type=str,
        default="tests_py/data/p17_corpus",
        help="Path to corpus directory or single JSON fixture (default: tests_py/data/p17_corpus)",
    )
    replay_p.add_argument("--json", action="store_true", help="Output machine-readable JSON report")

    # Shadow subcommand
    shadow_p = subparsers.add_parser("shadow", help="Run read-only shadow evaluation on legacy evidence")
    shadow_p.add_argument(
        "--evidence-root",
        type=str,
        required=True,
        help="Path to read-only evidence directory",
    )
    sink_group = shadow_p.add_mutually_exclusive_group(required=True)
    sink_group.add_argument("--stdout", action="store_true", help="Print shadow decision to stdout")
    sink_group.add_argument("--out", type=str, help="Directory inside runtime/p17-shadow to write record")
    shadow_p.add_argument("--json", action="store_true", help="Format stdout as JSON")
    shadow_p.add_argument("--project-id", type=str, default="devorchestrator", help="Project ID")
    shadow_p.add_argument("--goal-id", type=str, default="P17", help="Goal ID")

    return parser


def main(args: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(args)

    if parsed.command == "replay":
        corpus_path = Path(parsed.corpus).resolve()
        if not corpus_path.exists():
            sys.stderr.write(f"Corpus path does not exist: {corpus_path}\n")
            return 1

        harness = ReplayHarness()
        cases: list[ReplayCase] = []
        if corpus_path.is_file():
            cases.append(harness.load_case_file(corpus_path))
        else:
            for p in sorted(corpus_path.glob("*.json")):
                cases.append(harness.load_case_file(p))

        if not cases:
            sys.stderr.write(f"No JSON cases found in {corpus_path}\n")
            return 1

        report = harness.run_corpus(cases)
        if parsed.json:
            sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
        else:
            sys.stdout.write(
                f"Replay Results: {report.passed_cases}/{report.total_cases} passed. "
                f"Trace hash: {report.decision_trace_hash[:16]}... "
                f"Failed: {report.failed_cases}\n"
            )
            for res in report.case_results:
                status_str = "PASS" if res.passed else "FAIL"
                sys.stdout.write(f"  [{status_str}] Case {res.case.class_id}: {res.case.class_name} -> {res.actual_decision.kind.value}\n")

        return 0 if report.failed_cases == 0 else 1

    elif parsed.command == "shadow":
        try:
            ev_root = ReadOnlyEvidenceRoot(parsed.evidence_root)
        except Exception as e:
            sys.stderr.write(f"Failed to initialize read-only evidence root: {e}\n")
            return 1

        evaluator = ShadowEvaluator(ev_root)
        try:
            record = evaluator.evaluate_shadow(project_id=parsed.project_id, goal_id=parsed.goal_id)
        except Exception as e:
            sys.stderr.write(f"Shadow evaluation failed: {e}\n")
            return 1

        if parsed.stdout:
            if parsed.json:
                sys.stdout.write(json.dumps(record, indent=2) + "\n")
            else:
                sys.stdout.write(
                    f"Shadow Decision for {parsed.project_id}/{parsed.goal_id}: "
                    f"{record['decision']['kind']} (reason: {record['decision']['reason']})\n"
                )
            return 0

        if parsed.out:
            try:
                # Find repo root
                repo_root = Path.cwd().resolve()
                validated_dir = validate_shadow_sink(
                    parsed.out,
                    evidence_root=ev_root.path,
                    repo_root=repo_root,
                )
                validated_dir.mkdir(parents=True, exist_ok=True)
                out_file = validated_dir / f"shadow-{parsed.goal_id}.json"
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(record, f, indent=2, sort_keys=True)
                sys.stdout.write(f"Shadow decision written to {out_file}\n")
                return 0
            except Exception as e:
                sys.stderr.write(f"Fence violation or write error: {e}\n")
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
