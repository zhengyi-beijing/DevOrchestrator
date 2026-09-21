import json
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.contracts import BenchmarkTask, BrokerAttempt
from aibench.corpus import generate_synthetic_corpus
from aibench.prompts import get_canonical_tasks
from aibench.scoring import (
    evaluate_findings,
    score_trial,
    verify_citations,
)


class TestP16ScoringAndRubrics(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.workspace = Path(self.td.name) / "workspace"
        generate_synthetic_corpus(self.workspace)
        self.tasks = {t.task_id: t for t in get_canonical_tasks()}

    def tearDown(self):
        self.td.cleanup()

    def test_verify_citations(self):
        citations = [
            {"path": "src/synth_app/core/auth.py", "start_line": 1, "end_line": 10},
            {"path": "docs/architecture.md", "start_line": 5, "end_line": 15},
            {"path": "nonexistent.py", "start_line": 1, "end_line": 5},
            {"path": "../../secret.txt", "start_line": 1, "end_line": 5},
            {"path": "src/synth_app/core/auth.py", "start_line": 500, "end_line": 600},  # out of bounds
        ]
        valid, invalid, details = verify_citations(citations, self.workspace)
        self.assertEqual(valid, 2)
        self.assertEqual(invalid, 3)

    def test_evaluate_findings(self):
        content = "We found Security Infrastructure Team and Platform Performance Team, but also Payments Team."
        required = ["Security Infrastructure Team", "Platform Performance Team", "Data Layer Team"]
        prohibited = ["Payments Team"]
        met, total, false_count = evaluate_findings(content, required, prohibited)
        self.assertEqual(met, 2)
        self.assertEqual(total, 3)
        self.assertEqual(false_count, 1)

    def test_score_trial_failed_attempt(self):
        task = self.tasks["task_arch_ownership"]
        attempt = BrokerAttempt(request_id="r1", status="failed", error="timeout")
        score = score_trial(task, attempt, self.workspace, "trial_1")
        self.assertEqual(score.correctness, 0.0)
        self.assertEqual(score.cited_spans_valid, 0)
        self.assertEqual(score.required_findings_met, 0)

    def test_score_trial_invalid_json_output(self):
        task = self.tasks["task_arch_ownership"]
        attempt = BrokerAttempt(request_id="r1", status="succeeded", output="This is not valid json")
        score = score_trial(task, attempt, self.workspace, "trial_1")
        self.assertEqual(score.correctness, 0.0)
        self.assertEqual(score.details.get("error"), "invalid_json_output")

    def test_score_trial_planner_golden(self):
        task = self.tasks["task_arch_ownership"]
        payload = {
            "summary": "Architecture ownership summary",
            "ownership_matrix": [
                {"domain": "Security", "team": "Security Infrastructure Team", "file": "src/synth_app/core/auth.py"},
                {"domain": "Performance", "team": "Platform Performance Team", "file": "src/synth_app/core/cache.py"},
                {"domain": "Billing", "team": "Core Business Logic Team", "file": "src/synth_app/services/billing.py"},
                {"domain": "Data", "team": "Data Layer Team", "file": "src/synth_app/repository/account_repo.py"},
                {"domain": "Gateway", "team": "Application API Gateway Team", "file": "src/synth_app/handlers/api.py"},
            ],
            "dependency_flow": "handlers -> billing -> account_repo",
            "citations": [
                {"path": "src/synth_app/core/auth.py", "start_line": 1, "end_line": 10},
                {"path": "docs/architecture.md", "start_line": 1, "end_line": 15},
            ],
        }
        attempt = BrokerAttempt(request_id="r1", status="succeeded", output=json.dumps(payload))
        score = score_trial(task, attempt, self.workspace, "trial_planner_1")
        self.assertGreater(score.correctness, 0.8)
        self.assertEqual(score.cited_spans_valid, 2)
        self.assertEqual(score.cited_spans_invalid, 0)
        self.assertEqual(score.required_findings_met, 5)
        self.assertEqual(score.false_findings, 0)

    def test_score_trial_worker_golden(self):
        task = self.tasks["task_cache_worker"]
        # Implementation of get_or_set
        patch = """--- a/src/synth_app/core/cache.py
+++ b/src/synth_app/core/cache.py
@@ -60,0 +61,7 @@
+    def get_or_set(self, key: str, default_fn: Any, ttl: float | None = None) -> Any:
+        val = self.get(key)
+        if val is not None:
+            return val
+        computed = default_fn()
+        self.set(key, computed, ttl)
+        return computed
"""
        payload = {
            "summary": "Implemented LRUCache.get_or_set",
            "patch": patch,
            "citations": [
                {"path": "src/synth_app/core/cache.py", "start_line": 50, "end_line": 70},
            ],
        }
        attempt = BrokerAttempt(request_id="r1", status="succeeded", output=json.dumps(payload))
        score = score_trial(task, attempt, self.workspace, "trial_worker_1")
        self.assertTrue(score.patch_valid)
        self.assertGreaterEqual(score.tests_passed or 0, 3)
        self.assertEqual(score.tests_failed, 0)
        self.assertGreater(score.correctness, 0.8)

    def test_generic_diff_patch_fallback_arbitrary_files(self):
        from aibench.scoring import _apply_simple_patch_fallback
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            f1 = ws / "pkg" / "module_a.py"
            f1.parent.mkdir(parents=True)
            f1.write_text("def old_func():\n    return 42\n\ndef helper():\n    pass\n", encoding="utf-8")

            f2 = ws / "pkg" / "module_b.py"
            f2.write_text("class MyService:\n    def run(self):\n        return False\n", encoding="utf-8")

            patch = (
                "--- a/pkg/module_a.py\n"
                "+++ b/pkg/module_a.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-def old_func():\n"
                "-    return 42\n"
                "+def new_func(x: int) -> int:\n"
                "+    return x * 2\n"
                "--- a/pkg/module_b.py\n"
                "+++ b/pkg/module_b.py\n"
                "@@ -2,2 +2,2 @@\n"
                "-    def run(self):\n"
                "-        return False\n"
                "+    def run(self):\n"
                "+        return True\n"
            )

            res = _apply_simple_patch_fallback(patch, ws)
            self.assertTrue(res)
            self.assertIn("def new_func(x: int) -> int:", f1.read_text(encoding="utf-8"))
            self.assertIn("return True", f2.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
