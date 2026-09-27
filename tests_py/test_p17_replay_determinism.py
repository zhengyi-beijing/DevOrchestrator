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
