"""Historical incident replay harness, trace hashing, and crash injection.

Validates pure convergence decisions against real historical P12-P16.14 incidents.
Proves deterministic replay and trace-hash equality under crash injection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from dev_orchestrator.convergence.evaluator import Decision, DecisionKind, decide
from dev_orchestrator.convergence.evidence import (
    ConflictClaim,
    EvidenceItem,
    EvidenceSnapshot,
    SharedCredentialLease,
)
from dev_orchestrator.convergence.invariants import evaluate_convergence_invariants
from dev_orchestrator.convergence.policy import ProblemBudget, build_policy
from dev_orchestrator.convergence.successor import compute_handoff_idempotency_key
from dev_orchestrator.convergence.work_record import validate_work_record


@dataclass(frozen=True)
class ReplayCase:
    class_id: int
    class_name: str
    cpf_scenarios: tuple[str, ...]
    source_evidence: dict[str, Any]
    legacy_result: str
    expected_v0_decision: str
    expected_invariant_verdicts: dict[str, bool]
    human_intervention_count: int
    attempt_count: int
    duplicate_execution_count: int
    work_record_snapshot: dict[str, Any]
    evidence_snapshot: dict[str, Any]
    policy: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayCase:
        return cls(
            class_id=int(data["class_id"]),
            class_name=str(data["class_name"]),
            cpf_scenarios=tuple(data.get("cpf_scenarios", ())),
            source_evidence=dict(data.get("source_evidence", {})),
            legacy_result=str(data.get("legacy_result", "")),
            expected_v0_decision=str(data["expected_v0_decision"]),
            expected_invariant_verdicts={k: bool(v) for k, v in data.get("expected_invariant_verdicts", {}).items()},
            human_intervention_count=int(data.get("human_intervention_count", 0)),
            attempt_count=int(data.get("attempt_count", 1)),
            duplicate_execution_count=int(data.get("duplicate_execution_count", 0)),
            work_record_snapshot=dict(data["work_record_snapshot"]),
            evidence_snapshot=dict(data.get("evidence_snapshot", {})),
            policy=dict(data.get("policy", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "cpf_scenarios": list(self.cpf_scenarios),
            "source_evidence": self.source_evidence,
            "legacy_result": self.legacy_result,
            "expected_v0_decision": self.expected_v0_decision,
            "expected_invariant_verdicts": self.expected_invariant_verdicts,
            "human_intervention_count": self.human_intervention_count,
            "attempt_count": self.attempt_count,
            "duplicate_execution_count": self.duplicate_execution_count,
            "work_record_snapshot": self.work_record_snapshot,
            "evidence_snapshot": self.evidence_snapshot,
            "policy": self.policy,
        }


@dataclass(frozen=True)
class ReplayCaseResult:
    case: ReplayCase
    passed: bool
    actual_decision: Decision
    decision_matches: bool
    invariant_matches: bool
    invariant_failures: tuple[str, ...]
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.case.class_id,
            "class_name": self.case.class_name,
            "passed": self.passed,
            "actual_decision": self.actual_decision.to_dict(),
            "decision_matches": self.decision_matches,
            "invariant_matches": self.invariant_matches,
            "invariant_failures": list(self.invariant_failures),
            "details": self.details,
        }


@dataclass(frozen=True)
class ReplayReport:
    total_cases: int
    passed_cases: int
    failed_cases: int
    case_results: tuple[ReplayCaseResult, ...]
    decision_trace_hash: str
    aggregate_human_interventions: int
    aggregate_attempts: int
    aggregate_duplicate_executions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "decision_trace_hash": self.decision_trace_hash,
            "aggregate_human_interventions": self.aggregate_human_interventions,
            "aggregate_attempts": self.aggregate_attempts,
            "aggregate_duplicate_executions": self.aggregate_duplicate_executions,
            "results": [r.to_dict() for r in self.case_results],
        }


def decision_trace_hash(trace: Sequence[Decision]) -> str:
    """Compute deterministic canonical sha256 hash over an ordered sequence of Decisions."""
    serializable = [
        {
            "kind": d.kind.value,
            "reason": d.reason,
            "problem_id": d.problem_id,
            "parameters": d.parameters,
            "idempotency_key": d.idempotency_key,
            "invariant_citations": list(d.invariant_citations),
        }
        for d in trace
    ]
    raw = json.dumps(serializable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reconstruct_evidence(ev_dict: Mapping[str, Any]) -> EvidenceSnapshot:
    items = tuple(
        EvidenceItem(
            source=str(it["source"]),
            source_id=str(it.get("source_id", "")),
            timestamp=str(it.get("timestamp", "")),
            anchor=str(it.get("anchor", "")),
            data=dict(it.get("data", {})),
            digest=str(it.get("digest", "")),
            read_status=str(it.get("read_status", "OK")),
        )
        for it in ev_dict.get("items", [])
    )
    conflicts = tuple(
        ConflictClaim(
            source_a=str(c.get("source_a", "")),
            source_b=str(c.get("source_b", "")),
            conflict_type=str(c.get("conflict_type", "")),
            details=str(c.get("details", "")),
            resolved=bool(c.get("resolved", False)),
        )
        for c in ev_dict.get("conflicts", [])
    )
    leases = tuple(
        SharedCredentialLease(
            session_id=str(l.get("session_id", "")),
            credential_id=str(l.get("credential_id", "")),
            owner_role=str(l.get("owner_role", "")),
            lease_until=str(l.get("lease_until", "")),
            acquired_at=str(l.get("acquired_at", "")),
        )
        for l in ev_dict.get("shared_leases", [])
    )
    return EvidenceSnapshot(
        items=items,
        conflicts=conflicts,
        shared_leases=leases,
        exact_anchors={k: str(v) for k, v in ev_dict.get("exact_anchors", {}).items()},
        emergency_pause_asserted=bool(ev_dict.get("emergency_pause_asserted", False)),
        metadata=dict(ev_dict.get("metadata", {})),
    )


class ReplayHarness:
    """Executable harness to replay cases and evaluate convergence behavior."""

    @staticmethod
    def load_case_file(path: str | Path) -> ReplayCase:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ReplayCase.from_dict(data)

    @staticmethod
    def run_case(case: ReplayCase) -> ReplayCaseResult:
        work_record = validate_work_record(case.work_record_snapshot)
        evidence = _reconstruct_evidence(case.evidence_snapshot)

        # Reconstruct policy
        p_raw = case.policy
        budgets: dict[str, ProblemBudget] = {}
        for k, b in p_raw.get("problem_budgets", {}).items():
            budgets[k] = ProblemBudget(
                max_resource_attempts=b.get("max_resource_attempts", 3),
                max_strategy_attempts=b.get("max_strategy_attempts", 2),
                max_capability_escalations=b.get("max_capability_escalations", 2),
                max_total_attempts=b.get("max_total_attempts", 5),
            )
        default_b = p_raw.get("default_budget", {})
        policy = build_policy(
            default_budget=ProblemBudget(
                max_resource_attempts=default_b.get("max_resource_attempts", 3),
                max_strategy_attempts=default_b.get("max_strategy_attempts", 2),
                max_capability_escalations=default_b.get("max_capability_escalations", 2),
                max_total_attempts=default_b.get("max_total_attempts", 5),
            ),
            problem_budgets=budgets,
            quota_reset_rules=p_raw.get("quota_reset_rules", {}),
            wait_bounds=p_raw.get("wait_bounds", {}),
        )

        decision = decide(work_record, evidence, policy)
        invariants = evaluate_convergence_invariants(work_record, evidence, policy)

        # Compare decision
        decision_matches = (decision.kind.value == case.expected_v0_decision)

        # Compare expected invariant verdicts
        inv_failures = []
        for inv_code, expected_holds in case.expected_invariant_verdicts.items():
            actual = invariants.get(inv_code)
            if actual is None or actual.holds != expected_holds:
                inv_failures.append(f"{inv_code}: expected holds={expected_holds}, got {actual.holds if actual else 'missing'}")

        invariant_matches = (len(inv_failures) == 0)
        passed = decision_matches and invariant_matches

        details = "Replay passed" if passed else f"Decision match: {decision_matches}; Invariant failures: {inv_failures}"
        return ReplayCaseResult(
            case=case,
            passed=passed,
            actual_decision=decision,
            decision_matches=decision_matches,
            invariant_matches=invariant_matches,
            invariant_failures=tuple(inv_failures),
            details=details,
        )

    def run_corpus(self, cases: Sequence[ReplayCase]) -> ReplayReport:
        results = [self.run_case(c) for c in cases]
        decisions = [r.actual_decision for r in results]
        trace_hash = decision_trace_hash(decisions)
        passed_count = sum(1 for r in results if r.passed)

        return ReplayReport(
            total_cases=len(cases),
            passed_cases=passed_count,
            failed_cases=len(cases) - passed_count,
            case_results=tuple(results),
            decision_trace_hash=trace_hash,
            aggregate_human_interventions=sum(c.human_intervention_count for c in cases),
            aggregate_attempts=sum(c.attempt_count for c in cases),
            aggregate_duplicate_executions=sum(c.duplicate_execution_count for c in cases),
        )

    def simulate_crash_injection(
        self,
        case: ReplayCase,
        crash_points: Sequence[str] = ("before_write", "during_handoff"),
    ) -> dict[str, Any]:
        """Simulate crashes between durable write boundaries and verify trace-hash convergence.

        Models an ordered sequence of durable writes for the acceptance/successor/handoff path,
        replays prefixes truncated at each crash point followed by resumption,
        and asserts both trace-hash equality and zero duplicate publication via ActuatorGuard idempotency keys.
        """
        baseline_result = self.run_case(case)
        baseline_decision = baseline_result.actual_decision
        baseline_hash = decision_trace_hash([baseline_decision])

        from dev_orchestrator.convergence.actuator_guard import ActuatorGuard

        total_duplicate_publications = 0
        replays_after_crash = []

        for point in crash_points:
            snap = dict(case.work_record_snapshot)
            guard = ActuatorGuard()

            succ = snap.get("successor") or {}
            real_key = succ.get("handoff_idempotency_key")
            if not real_key and succ.get("successor_goal_id"):
                acc_raw = json.dumps(snap.get("acceptance") or {}, sort_keys=True, separators=(",", ":"))
                acc_digest = hashlib.sha256(acc_raw.encode("utf-8")).hexdigest()
                real_key = compute_handoff_idempotency_key(
                    snap.get("project_id", ""),
                    snap.get("goal_id", ""),
                    succ["successor_goal_id"],
                    acc_digest,
                )
            if not real_key:
                real_key = baseline_decision.idempotency_key

            prior_published = False
            if point in ("after_successor_publish", "after_write", "during_handoff", "after_handoff"):
                guard.record_executed(real_key)
                prior_published = True

            resumed_snap = dict(snap)
            if point in ("before_verification",):
                resumed_snap["verification"] = None
                resumed_snap["acceptance"] = {"kind": "NONE"}
                resumed_snap["status"] = "OPEN"
            elif point in ("after_verification", "before_acceptance"):
                resumed_snap["acceptance"] = {"kind": "NONE"}
                resumed_snap["status"] = "OPEN"
            elif point in ("after_successor_publish", "after_write"):
                if resumed_snap.get("successor"):
                    s = dict(resumed_snap["successor"])
                    s["publication_state"] = "PUBLISHED"
                    resumed_snap["successor"] = s
            elif point in ("during_handoff", "after_handoff"):
                if resumed_snap.get("successor"):
                    s = dict(resumed_snap["successor"])
                    s["publication_state"] = "PENDING"
                    resumed_snap["successor"] = s

            resumed_case_dict = case.to_dict()
            resumed_case_dict["work_record_snapshot"] = resumed_snap
            resumed_case = ReplayCase.from_dict(resumed_case_dict)
            c_result = self.run_case(resumed_case)

            c_dec = c_result.actual_decision
            guard_rejected = False
            rejection_code = None
            duplicate_count = 0

            if c_dec.kind == DecisionKind.PUBLISH_SUCCESSOR:
                expected_head = (case.evidence_snapshot.get("exact_anchors") or {}).get("head")
                verdict = guard.validate(
                    c_dec,
                    validate_work_record(resumed_snap),
                    _reconstruct_evidence(case.evidence_snapshot),
                    expected_anchor_head=expected_head,
                )
                if not verdict.accepted:
                    guard_rejected = True
                    rejection_code = verdict.rejection_code
                    duplicate_count = 0
                else:
                    guard.record_executed(c_dec.idempotency_key)
                    if prior_published:
                        duplicate_count += 1

            total_duplicate_publications += duplicate_count
            c_hash = decision_trace_hash([c_dec])

            replays_after_crash.append({
                "crash_point": point,
                "decision": c_dec.kind.value,
                "trace_hash": c_hash,
                "hash_matches_baseline": (c_hash == baseline_hash),
                "duplicate_publications": duplicate_count,
                "guard_rejected": guard_rejected,
                "rejection_code": rejection_code,
            })

        convergent_points = {
            "before_write",
            "before_successor_publish",
            "after_acceptance",
            "during_handoff",
            "after_handoff",
        }
        tested_convergent = [r for r in replays_after_crash if r["crash_point"] in convergent_points]
        all_match = (
            all(r["hash_matches_baseline"] for r in tested_convergent)
            if tested_convergent
            else all(r["hash_matches_baseline"] for r in replays_after_crash)
        ) and (total_duplicate_publications == 0)

        return {
            "baseline_decision": baseline_result.actual_decision.kind.value,
            "baseline_trace_hash": baseline_hash,
            "all_converged": all_match,
            "duplicate_publications": total_duplicate_publications,
            "crash_point_results": replays_after_crash,
        }
