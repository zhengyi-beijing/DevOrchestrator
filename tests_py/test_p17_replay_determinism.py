"""P17 Replay determinism and crash injection tests."""
from __future__ import annotations

from pathlib import Path
import unittest

from dev_orchestrator.convergence.replay import ReplayCase, ReplayHarness


class TestP17ReplayDeterminism(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus_dir = Path("tests_py/data/p17_corpus")
        self.harness = ReplayHarness()
        self.cases: list[ReplayCase] = []
        for p in sorted(self.corpus_dir.glob("*.json")):
            self.cases.append(self.harness.load_case_file(p))

    def test_repeated_corpus_runs_yield_identical_trace_hash(self) -> None:
        report1 = self.harness.run_corpus(self.cases)
        report2 = self.harness.run_corpus(self.cases)
        self.assertEqual(report1.decision_trace_hash, report2.decision_trace_hash)

    def test_crash_injection_between_durable_writes_converges(self) -> None:
        case26 = [c for c in self.cases if c.class_id == 26][0]
        crash_report = self.harness.simulate_crash_injection(case26)
        self.assertTrue(crash_report["all_converged"])
        for pt_res in crash_report["crash_point_results"]:
            self.assertTrue(pt_res["hash_matches_baseline"])
            self.assertEqual(pt_res["trace_hash"], crash_report["baseline_trace_hash"])

    def test_truncated_and_repeated_durable_write_sequences_yield_one_publication(self) -> None:
        """Finding 1 / Finding 7: Durable write prefixes truncated at crash points resume honestly with zero duplicate publications."""
        case26 = [c for c in self.cases if c.class_id == 26][0]
        crash_points = (
            "before_verification",
            "after_verification",
            "before_acceptance",
            "after_acceptance",
            "before_successor_publish",
            "after_successor_publish",
            "after_handoff",
        )
        report = self.harness.simulate_crash_injection(case26, crash_points=crash_points)
        self.assertTrue(report["all_converged"])
        self.assertEqual(report["duplicate_publications"], 0)
        self.assertEqual(len(report["crash_point_results"]), len(crash_points))

        by_point = {r["crash_point"]: r for r in report["crash_point_results"]}

        # Truncated prefixes diverge honestly
        self.assertEqual(by_point["before_verification"]["decision"], "EXECUTE")
        self.assertFalse(by_point["before_verification"]["hash_matches_baseline"])

        self.assertEqual(by_point["after_verification"]["decision"], "EXECUTE")
        self.assertFalse(by_point["after_verification"]["hash_matches_baseline"])

        self.assertEqual(by_point["before_acceptance"]["decision"], "EXECUTE")
        self.assertFalse(by_point["before_acceptance"]["hash_matches_baseline"])

        # Fully published prefix transitions to NOOP_ACTIVE
        self.assertEqual(by_point["after_successor_publish"]["decision"], "NOOP_ACTIVE")
        self.assertFalse(by_point["after_successor_publish"]["hash_matches_baseline"])

        # Prepared / resumption points converge to PUBLISH_SUCCESSOR
        self.assertEqual(by_point["after_acceptance"]["decision"], "PUBLISH_SUCCESSOR")
        self.assertTrue(by_point["after_acceptance"]["hash_matches_baseline"])

        self.assertEqual(by_point["before_successor_publish"]["decision"], "PUBLISH_SUCCESSOR")
        self.assertTrue(by_point["before_successor_publish"]["hash_matches_baseline"])

        # after_handoff attempts duplicate publish but ActuatorGuard rejects it via stable idempotency key
        self.assertEqual(by_point["after_handoff"]["decision"], "PUBLISH_SUCCESSOR")
        self.assertTrue(by_point["after_handoff"]["hash_matches_baseline"])
        self.assertTrue(by_point["after_handoff"]["guard_rejected"])
        self.assertEqual(by_point["after_handoff"]["rejection_code"], "DUPLICATE_IDEMPOTENCY_KEY")
        self.assertEqual(by_point["after_handoff"]["duplicate_publications"], 0)

    def test_publish_successor_idempotency_key_stable_across_evidence_and_revision_churn(self) -> None:
        """Finding 3: PUBLISH_SUCCESSOR idempotency key is invariant across revision bump and evidence churn."""
        from dev_orchestrator.convergence.actuator_guard import ActuatorGuard
        from dev_orchestrator.convergence.evaluator import DecisionKind, decide
        from dev_orchestrator.convergence.evidence import EvidenceItem, EvidenceSnapshot
        from dev_orchestrator.convergence.policy import build_policy
        from dev_orchestrator.convergence.work_record import validate_work_record

        case26 = [c for c in self.cases if c.class_id == 26][0]
        work_rec = validate_work_record(case26.work_record_snapshot)
        evidence1 = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="repo_truth",
                    source_id="item-1",
                    timestamp="2026-09-27T12:00:00Z",
                    anchor="head-p17-final",
                    data={"state": "clean"},
                    digest="d1",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-p17-final"},
            emergency_pause_asserted=False,
            metadata={"run": 1},
        )
        policy = build_policy()

        # 1. Baseline decision
        dec1 = decide(work_rec, evidence1, policy)
        self.assertEqual(dec1.kind, DecisionKind.PUBLISH_SUCCESSOR)
        key1 = dec1.idempotency_key
        self.assertTrue(key1)

        # 2. Resumed decision with authority revision bump and evidence churn
        churned_dict = work_rec.to_dict()
        churned_dict["authority_revision"] = "rev-999-new"
        churned_rec = validate_work_record(churned_dict)

        evidence2 = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="repo_truth",
                    source_id="item-1",
                    timestamp="2026-09-27T12:05:00Z",
                    anchor="head-p17-final",
                    data={"state": "clean"},
                    digest="d1-churned",
                    read_status="OK",
                ),
                EvidenceItem(
                    source="broker_telemetry",
                    source_id="item-2",
                    timestamp="2026-09-27T12:05:01Z",
                    anchor="head-p17-final",
                    data={"latency_ms": 42},
                    digest="d2-new",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-p17-final"},
            emergency_pause_asserted=False,
            metadata={"run": 2, "churn": "true"},
        )

        dec2 = decide(churned_rec, evidence2, policy)
        self.assertEqual(dec2.kind, DecisionKind.PUBLISH_SUCCESSOR)
        self.assertEqual(dec2.idempotency_key, key1, "Idempotency key must remain identical despite churn")

        # 3. Guard pre-seeded with key1 rejects dec2 as duplicate
        guard = ActuatorGuard()
        guard.record_executed(key1)

        verdict = guard.validate(
            dec2,
            churned_rec,
            evidence2,
            expected_anchor_head="head-p17-final",
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.rejection_code, "DUPLICATE_IDEMPOTENCY_KEY")


    def test_shadow_delete_and_rebuild_differential_trace_hash(self) -> None:
        """Finding 7: Deleting and rebuilding shadow evaluation namespace yields byte-identical trace hash."""
        import tempfile
        from dev_orchestrator.convergence.shadow import ReadOnlyEvidenceRoot, ShadowEvaluator

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            devorch = tmp_root / ".devorch"
            devorch.mkdir(parents=True)
            (devorch / "status.json").write_text('{"head": "head-shadow-1", "branch": "main"}', encoding="utf-8")

            ev_root = ReadOnlyEvidenceRoot(tmp_root)
            evaluator1 = ShadowEvaluator(ev_root)
            record1 = evaluator1.evaluate_shadow(project_id="test-shadow", goal_id="P17")

            # Simulate shadow namespace wipe and rebuild
            shadow_dir = tmp_root / "runtime" / "p17-shadow"
            shadow_dir.mkdir(parents=True, exist_ok=True)
            (shadow_dir / "out.json").write_text("test", encoding="utf-8")
            import shutil
            shutil.rmtree(shadow_dir)

            evaluator2 = ShadowEvaluator(ev_root)
            record2 = evaluator2.evaluate_shadow(project_id="test-shadow", goal_id="P17")

            self.assertEqual(record1["record_hash"], record2["record_hash"])
            self.assertEqual(record1["source_evidence_digest"], record2["source_evidence_digest"])
            self.assertEqual(record1["decision"], record2["decision"])

    def test_corpus_replay_is_wall_clock_independent(self) -> None:
        """Finding 1: Replay results and trace hashes are identical regardless of when executed."""
        report1 = self.harness.run_corpus(self.cases)

        future_cases = [
            ReplayCase.from_dict({**c.to_dict(), "now": "2026-09-27T01:00:00Z"})
            for c in self.cases
        ]
        report2 = self.harness.run_corpus(future_cases)
        self.assertEqual(report1.passed_cases, 26)
        self.assertEqual(report2.passed_cases, 26)
        self.assertEqual(report1.decision_trace_hash, report2.decision_trace_hash)
        self.assertEqual(report1.aggregate_duplicate_executions, 0)
        self.assertEqual(report2.aggregate_duplicate_executions, 0)

    def test_execute_replay_after_guard_restart_is_rejected_from_durable_state(self) -> None:
        """Finding 1: Replay of EXECUTE after guard restart is rejected by ActuatorGuard from durable state."""
        from dev_orchestrator.convergence.actuator_guard import ActuatorGuard
        from dev_orchestrator.convergence.evaluator import DecisionKind, decide
        from dev_orchestrator.convergence.policy import build_policy
        from dev_orchestrator.convergence.problems import ProblemBudget
        from dev_orchestrator.convergence.replay import _reconstruct_evidence
        from dev_orchestrator.convergence.work_record import (
            model_durable_decision_transition,
            validate_work_record,
        )

        case24 = [c for c in self.cases if c.class_id == 24][0]
        work_rec = validate_work_record(case24.work_record_snapshot)
        evidence = _reconstruct_evidence(case24.evidence_snapshot)

        p_raw = case24.policy
        default_b = p_raw.get("default_budget", {})
        policy = build_policy(
            default_budget=ProblemBudget(
                max_resource_attempts=default_b.get("max_resource_attempts", 3),
                max_strategy_attempts=default_b.get("max_strategy_attempts", 2),
                max_capability_escalations=default_b.get("max_capability_escalations", 2),
                max_total_attempts=default_b.get("max_total_attempts", 5),
            ),
            quota_reset_rules=p_raw.get("quota_reset_rules", {}),
            wait_bounds=p_raw.get("wait_bounds", {}),
        )

        # 1. Initial decide emits EXECUTE
        dec = decide(work_rec, evidence, policy, now=case24.now)
        self.assertEqual(dec.kind, DecisionKind.EXECUTE)
        self.assertTrue(bool(dec.idempotency_key))

        # 2. Fresh guard admits initial EXECUTE
        guard1 = ActuatorGuard(work_record=work_rec)
        head = (case24.evidence_snapshot.get("exact_anchors") or {}).get("head")
        v1 = guard1.validate(
            dec,
            work_rec,
            evidence,
            expected_anchor_head=head,
            expected_goal_id=work_rec.goal_id,
            expected_authority_revision=work_rec.authority_revision,
        )
        self.assertTrue(v1.accepted, f"Initial EXECUTE was not accepted: {v1.reason}")

        # 3. Model durable intent / authority transition
        durable_rec = model_durable_decision_transition(work_rec, dec, now=case24.now)
        self.assertIsNotNone(durable_rec.active_lease)
        self.assertEqual(durable_rec.active_lease.get("idempotency_key"), dec.idempotency_key)
        self.assertIn(dec.idempotency_key, durable_rec.consumed_idempotency_keys)
        self.assertNotEqual(durable_rec.authority_revision, work_rec.authority_revision)

        # 4. Restart: instantiate brand new ActuatorGuard seeded ONLY from durable_rec
        restarted_guard = ActuatorGuard(work_record=durable_rec)
        self.assertIn(dec.idempotency_key, restarted_guard._executed_keys)

        # 5. Replaying the identical EXECUTE decision is firmly rejected as duplicate
        v_replay = restarted_guard.validate(
            dec,
            durable_rec,
            evidence,
            expected_anchor_head=head,
            expected_goal_id=durable_rec.goal_id,
            expected_authority_revision=durable_rec.authority_revision,
        )
        self.assertFalse(v_replay.accepted)
        self.assertEqual(v_replay.rejection_code, "DUPLICATE_IDEMPOTENCY_KEY")

        # 6. Re-evaluating from durable state does NOT emit an admitted duplicate EXECUTE
        dec2 = decide(durable_rec, evidence, policy, now=case24.now)
        self.assertNotEqual(dec2.kind, DecisionKind.EXECUTE)
        v2 = restarted_guard.validate(
            dec2,
            durable_rec,
            evidence,
            expected_anchor_head=head,
            expected_goal_id=durable_rec.goal_id,
            expected_authority_revision=durable_rec.authority_revision,
        )
        self.assertNotEqual(v2.rejection_code, "DUPLICATE_IDEMPOTENCY_KEY")
