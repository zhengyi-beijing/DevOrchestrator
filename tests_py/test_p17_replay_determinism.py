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
