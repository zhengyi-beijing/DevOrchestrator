import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.broker_client import BrokerBenchmarkClient
from aibench.contracts import (
    ResourceSnapshot,
    TRACK_A,
    TRACK_B,
)
from aibench.corpus import generate_synthetic_corpus
from aibench.prompts import get_canonical_tasks
from aibench.report import build_run_summary, generate_report_markdown
from aibench.runner import BenchmarkRunner, build_trial_plan
from aibench.workspace import cleanup_workspace, materialize_workspace
from aibench.zvec import ZvecAdapter
from tests_py.test_p16_broker_client import MockExecutionPort


class TestP16RunnerAndSummary(unittest.TestCase):
    def test_build_trial_plan_requirements(self):
        # < 3 resources raises ValueError
        with self.assertRaises(ValueError):
            build_trial_plan(selected_resources=["res_1", "res_2"])

        # >= 3 resources and all 4 roles succeeds
        resources = ["res_1", "res_2", "res_3"]
        plan = build_trial_plan(selected_resources=resources, repeats=1, seed=42)
        self.assertEqual(len(plan.selected_resources), 3)
        # 4 tasks * 3 resources * 2 tracks = 24 cells
        self.assertEqual(len(plan.cells), 24)

        # Reproducibility with same seed
        plan2 = build_trial_plan(selected_resources=resources, repeats=1, seed=42)
        self.assertEqual(plan.cells, plan2.cells)

        # Pair order is randomized (contains both A-first and B-first pairs)
        order_0_tracks = {c.track for c in plan.cells if c.pair_order == 0}
        self.assertEqual(order_0_tracks, {TRACK_A, TRACK_B})

    def test_materialized_workspace_is_clean_git_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            corpus_dir = base / "corpus"
            generate_synthetic_corpus(corpus_dir)
            resources = ["res_1", "res_2", "res_3"]
            plan = build_trial_plan(
                selected_resources=resources,
                tasks=(get_canonical_tasks()[0],),
                repeats=1,
                seed=42,
                require_all_roles=False,
            )
            cell = next(c for c in plan.cells if c.track == TRACK_A)
            workspace, retrieval_applied = materialize_workspace(
                base / "scratch",
                cell,
                corpus_dir,
                ZvecAdapter(),
            )
            try:
                self.assertFalse(retrieval_applied)
                top = subprocess.run(
                    ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                self.assertEqual(Path(top).resolve(), workspace.resolve())
                status = subprocess.run(
                    ["git", "-C", str(workspace), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                self.assertEqual(status, "")
                exclude = (workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8")
                self.assertIn(".aibench/", exclude)
            finally:
                cleanup_workspace(workspace)

    def test_runner_execution_and_resumption(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            corpus_dir = base / "corpus"
            generate_synthetic_corpus(corpus_dir)
            scratch_dir = base / "scratch"
            results_file = base / "results.jsonl"

            port = MockExecutionPort()
            client = BrokerBenchmarkClient(port)
            zvec = ZvecAdapter()
            runner = BenchmarkRunner(client, zvec, scratch_dir, results_file)

            resources = ["res_1", "res_2", "res_3"]
            plan = build_trial_plan(selected_resources=resources, repeats=1, seed=42)
            snapshot = ResourceSnapshot("snap1", "test", "dig1", "now", tuple({"resource_id": r, "enabled": True} for r in resources))

            # Run 2 cells out of 24
            small_plan = build_trial_plan(selected_resources=resources, tasks=(get_canonical_tasks()[0],), repeats=1, seed=42, require_all_roles=False)
            # 1 task * 3 resources * 2 tracks = 6 cells
            self.assertEqual(len(small_plan.cells), 6)

            records1 = runner.run_plan(small_plan, corpus_dir, snapshot)
            self.assertEqual(len(records1), 6)
            self.assertTrue(results_file.is_file())
            self.assertTrue(port.recorded_requests)
            self.assertTrue(
                all(req.metadata.get("managed_worktree") is True for req in port.recorded_requests)
            )

            # Second execution of same plan should resume and not re-dispatch
            initial_dispatches = len(port.recorded_requests)
            records2 = runner.run_plan(small_plan, corpus_dir, snapshot)
            self.assertEqual(len(records2), 6)
            self.assertEqual(len(port.recorded_requests), initial_dispatches)

            # Build summary
            summary = build_run_summary(records1, small_plan)
            self.assertEqual(summary.total_trials, 6)
            self.assertEqual(summary.completed_trials, 6)
            self.assertIn("correctness_mean", summary.track_a_summary)
            self.assertIn("correctness_mean", summary.track_b_summary)
            self.assertEqual(len(summary.resource_breakdown), 3)

            # Generate markdown report
            md = generate_report_markdown(summary)
            self.assertIn("AI Capability Benchmark Report", md)
            self.assertIn("Retrieval A/B Track Comparison", md)
            self.assertIn("Resource Breakdown", md)


if __name__ == "__main__":
    unittest.main()
