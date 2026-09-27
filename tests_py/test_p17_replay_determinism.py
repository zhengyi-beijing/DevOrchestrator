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
        """Finding 7: Durable write prefixes truncated at crash points resume with trace-hash equality and zero duplicate publications."""
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
        for pt_res in report["crash_point_results"]:
            self.assertTrue(pt_res["hash_matches_baseline"])
            self.assertEqual(pt_res["trace_hash"], report["baseline_trace_hash"])
            self.assertEqual(pt_res["duplicate_publications"], 0)

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
