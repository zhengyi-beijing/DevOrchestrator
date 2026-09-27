"""P17 Corpus replay test suite.

Validates all 26 required historical replay classes pass with expected decisions,
invariant verdicts, and zero duplicate executions.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from dev_orchestrator.convergence.replay import ReplayCase, ReplayHarness


class TestP17ReplayCorpus(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus_dir = Path("tests_py/data/p17_corpus")
        self.harness = ReplayHarness()
        self.cases: list[ReplayCase] = []
        for p in sorted(self.corpus_dir.glob("*.json")):
            self.cases.append(self.harness.load_case_file(p))

    def test_corpus_contains_all_26_classes(self) -> None:
        self.assertEqual(len(self.cases), 26, f"Expected 26 replay cases, found {len(self.cases)}")
        class_ids = {c.class_id for c in self.cases}
        self.assertEqual(class_ids, set(range(1, 27)))

    def test_all_26_cases_pass_replay(self) -> None:
        report = self.harness.run_corpus(self.cases)
        failed_messages = [
            f"Case {r.case.class_id} ({r.case.class_name}): {r.details}"
            for r in report.case_results if not r.passed
        ]
        self.assertEqual(
            report.failed_cases,
            0,
            f"{report.failed_cases} cases failed replay:\n" + "\n".join(failed_messages),
        )
        self.assertEqual(report.passed_cases, 26)
        self.assertEqual(report.aggregate_duplicate_executions, 0)
        self.assertTrue(bool(report.decision_trace_hash))

    def test_bootstrap_owner_override_fixture_integrity(self) -> None:
        override_file = Path("agent/evidence/P17_BOOTSTRAP_OWNER_OVERRIDE.json")
        self.assertTrue(override_file.exists(), "P17_BOOTSTRAP_OWNER_OVERRIDE.json missing")
