"""P16.14 Invariant-driven control-plane evidence projector.

Reads durable lifecycle authority, transition journal, executions, planner,
reviewer, and decision ledgers to emit read-only convergence and traversal
evidence projections, plus validates the fault scenario registry.

Usage:
    python ops/p1614_evidence.py [--runtime-root <dir>] [--project-id <id>] [--json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, "src")

from dev_orchestrator.core.control_plane_contract import (
    convergence_evidence,
    traversal_evidence,
)
from dev_orchestrator.core.control_plane_faults import (
    fault_scenarios,
    validate_fault_registry,
)


def load_json_file(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="P16.14 Control-Plane Invariant Evidence Projector")
    parser.add_argument("--runtime-root", default=".dev_orchestrator", help="Runtime directory containing ledgers")
    parser.add_argument("--project-id", default="devorchestrator", help="Project ID to project evidence for")
    parser.add_argument("--json", action="store_true", help="Output pure JSON")
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root)
    repo_root = Path.cwd()

    # Load durable ledgers
    executor_state = load_json_file(runtime_root / "transition-executor.json")
    if not executor_state:
        executor_state = load_json_file(runtime_root / "execution-port.json")
    planner_state = load_json_file(runtime_root / "ai-planner.json")
    reviewer_state = load_json_file(runtime_root / "ai-reviewer.json")
    decisions_state = load_json_file(runtime_root / "review-decisions.json")

    # Read-only projections
    conv = convergence_evidence(
        executor_state=executor_state,
        planner_state=planner_state,
        reviewer_state=reviewer_state,
        decisions_state=decisions_state,
        project_id=args.project_id,
    )

    trav = traversal_evidence(
        executor_state=executor_state,
        planner_state=planner_state,
        reviewer_state=reviewer_state,
        decisions_state=decisions_state,
        project_id=args.project_id,
    )

    registry_errors = validate_fault_registry(repo_root)

    evidence_payload = {
        "status": "PASS" if not registry_errors else "FAIL",
        "project_id": args.project_id,
        "runtime_root": str(runtime_root),
        "convergence_evidence": conv,
        "traversal_evidence": trav,
        "fault_scenarios_count": len(fault_scenarios()),
        "fault_registry_validation_errors": registry_errors,
    }

    if args.json:
        print(json.dumps(evidence_payload, indent=2))
    else:
        print("=== P16.14 Invariant-Driven Control-Plane Evidence ===")
        print(f"Project ID: {args.project_id}")
        print(f"Lifecycle state: {conv.get('lifecycle_state')}")
        print(f"Current task: {conv.get('current_task_id')}")
        print(f"Active owner: {conv.get('active_owner')}")
        print(f"Active gate: {conv.get('active_gate')}")
        print(f"Generation: {conv.get('generation')}")
        print(f"Resolved gates count: {conv.get('resolved_gates_count')}")
        print(f"Handoffs count: {conv.get('handoffs_count')}")
        print(f"Transitions count: {conv.get('transitions_count')}")
        print(f"Plan frozen: {trav.get('plan_frozen')}")
        print(f"Plan reviewed: {trav.get('plan_reviewed')}")
        print(f"Worker launched: {trav.get('worker_launched')}")
        print(f"Review settled: {trav.get('review_settled')}")
        print(f"Fault scenarios registered: {len(fault_scenarios())}")
        if registry_errors:
            print("Fault registry errors:")
            for err in registry_errors:
                print(f"  - {err}")
        else:
            print("Fault registry: VALID")
        print("=====================================================")

    return 0 if not registry_errors else 1


if __name__ == "__main__":
    sys.exit(main())
