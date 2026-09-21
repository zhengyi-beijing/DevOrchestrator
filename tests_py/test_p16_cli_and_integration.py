import json
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.cli import main


class TestP16CliAndIntegration(unittest.TestCase):
    def test_cli_corpus_verify(self):
        with tempfile.TemporaryDirectory() as td:
            corpus_dir = Path(td) / "corpus"
            sys.argv = ["aibench", "corpus-verify", "--corpus-path", str(corpus_dir)]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)
            self.assertTrue((corpus_dir / "manifest.json").is_file())

    def test_cli_containment_audit(self):
        with tempfile.TemporaryDirectory() as td:
            reg_file = Path(td) / "resources.yaml"
            reg_file.write_text("resources: []", encoding="utf-8")
            prot_dir = Path(td) / "protected_repo"
            prot_dir.mkdir()
            config_file = Path(td) / "config.json"
            cfg_data = {
                "scratch_root": str(Path(td) / "scratch"),
                "queue_root": str(Path(td) / "queue"),
                "dedicated_sid": "",
                "scheduled_task_name": "TestTask",
                "broker_config_path": str(reg_file),
                "zvec_path": "",
                "firewall_rule_name": "TestRule",
                "protected_roots": [str(prot_dir)],
            }
            config_file.write_text(json.dumps(cfg_data), encoding="utf-8")

            sys.argv = [
                "aibench",
                "containment-audit",
                "--config", str(config_file),
                "--allow-mock-sid",
                "--allow-dev-roots",
            ]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)

    def test_cli_zvec_probe(self):
        sys.argv = ["aibench", "zvec-probe", "--zvec-path", "C:\\nonexistent\\zvec.exe"]
        with self.assertRaises(SystemExit) as ctx:
            main()
        # Nonexistent returns 1
        self.assertEqual(ctx.exception.code, 1)

    def test_cli_end_to_end_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            corpus_dir = base / "corpus"
            plan_file = base / "trial_plan.json"
            output_dir = base / "output"
            summary_file = base / "summary.json"
            report_file = base / "report.md"
            decision_file = base / "decision.json"
            queue_dir = base / "queue"

            # 1. plan-freeze
            sys.argv = [
                "aibench",
                "plan-freeze",
                "--corpus-path", str(corpus_dir),
                "--output", str(plan_file),
                "--repeats", "1",
                "--seed", "42",
                "--resources", "res_1,res_2,res_3",
            ]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)
            self.assertTrue(plan_file.is_file())

            # 2. submit
            sys.argv = ["aibench", "submit", "--plan", str(plan_file), "--queue", str(queue_dir)]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)

            # 3. run (mock broker)
            sys.argv = [
                "aibench",
                "run",
                "--plan", str(plan_file),
                "--output-dir", str(output_dir),
                "--corpus-path", str(corpus_dir),
                "--mock-broker",
            ]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)

            results_files = list(output_dir.glob("results_*.jsonl"))
            self.assertEqual(len(results_files), 1)
            results_file = results_files[0]

            # 4. report
            sys.argv = [
                "aibench",
                "report",
                "--results", str(results_file),
                "--plan", str(plan_file),
                "--output", str(summary_file),
                "--markdown", str(report_file),
            ]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)
            self.assertTrue(summary_file.is_file())
            self.assertTrue(report_file.is_file())

            # 5. decide
            sys.argv = [
                "aibench",
                "decide",
                "--summary", str(summary_file),
                "--plan", str(plan_file),
                "--output", str(decision_file),
            ]
            with self.assertRaises(SystemExit) as ctx:
                main()
            self.assertEqual(ctx.exception.code, 0)
            self.assertTrue(decision_file.is_file())

            decision_data = json.loads(decision_file.read_text(encoding="utf-8"))
            self.assertEqual(decision_data["decision"], "NO_PROMOTE")
            self.assertIn("capability_unsupported", decision_data["reasons"])

    def test_committed_evidence_consistency(self):
        from aibench.contracts import TrialPlan, TrialRecord
        from aibench.prompts import get_canonical_tasks
        from aibench.report import build_run_summary

        repo_root = Path(__file__).resolve().parents[1]
        ev_dir = repo_root / "benchmark" / "evidence" / "p16_baseline_20260921"

        plan_file = ev_dir / "trial_plan.json"
        results_file = ev_dir / "results.jsonl"
        summary_file = ev_dir / "summary.json"
        report_file = ev_dir / "report.md"
        decision_file = ev_dir / "promotion_decision.json"

        self.assertTrue(plan_file.is_file(), f"{plan_file} must exist")
        self.assertTrue(results_file.is_file(), f"{results_file} must exist")
        self.assertTrue(summary_file.is_file(), f"{summary_file} must exist")
        self.assertTrue(report_file.is_file(), f"{report_file} must exist")
        self.assertTrue(decision_file.is_file(), f"{decision_file} must exist")

        plan = TrialPlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
        self.assertEqual(len(plan.cells), 24)
        self.assertEqual(plan.execution_source, "pipeline_self_test")

        records: list[TrialRecord] = []
        with open(results_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(TrialRecord.from_dict(json.loads(line.strip())))

        self.assertEqual(len(records), 24)
        plan_cell_ids = [c.trial_id for c in plan.cells]
        record_trial_ids = [r.trial_id for r in records]
        self.assertEqual(record_trial_ids, plan_cell_ids)

        timestamps: list[str] = []
        for r in records:
            self.assertEqual(r.status, "completed")
            self.assertEqual(r.broker_attempt.status, "succeeded")
            self.assertEqual(r.execution_source, "pipeline_self_test")
            self.assertEqual(r.broker_attempt.provider, "mock")
            self.assertIsNone(r.broker_attempt.dispatch_id)
            self.assertIsNone(r.broker_attempt.execution_id)
            self.assertGreaterEqual(r.score.correctness, 0.0)
            self.assertGreaterEqual(r.score.required_findings_total, 0)
            self.assertGreaterEqual(r.score.false_findings, 0)
            self.assertGreaterEqual(r.metrics.observed_shell_tool_calls, 0)

            # Details must not be empty and citations must be list if present
            self.assertIsInstance(r.score.details, dict)
            if "citation_details" in r.score.details:
                self.assertIsInstance(r.score.details["citation_details"], list)

            # Worker role must have valid patch fields if evaluated
            if r.cell.role == "worker" and r.score.patch_valid is not None:
                self.assertIsInstance(r.score.patch_valid, bool)

            timestamps.append(r.finished_at)

        # Timestamps must not all be identical
        self.assertGreater(len(set(timestamps)), 1)

        # Re-aggregated summary matches summary.json
        summary = build_run_summary(records, plan)
        saved_summary = json.loads(summary_file.read_text(encoding="utf-8"))
        self.assertEqual(summary.total_trials, saved_summary["total_trials"])
        self.assertEqual(summary.completed_trials, saved_summary["completed_trials"])
        self.assertEqual(summary.execution_source, "pipeline_self_test")
        self.assertFalse(summary.has_real_broker_evidence)
        self.assertEqual(saved_summary["execution_source"], "pipeline_self_test")
        self.assertFalse(saved_summary["has_real_broker_evidence"])
        self.assertAlmostEqual(summary.track_a_summary["correctness_mean"], saved_summary["track_a_summary"]["correctness_mean"])
        self.assertAlmostEqual(summary.track_b_summary["correctness_mean"], saved_summary["track_b_summary"]["correctness_mean"])

        # Promotion decision check: fails closed on evidence_gate and capability_gate
        decision_data = json.loads(decision_file.read_text(encoding="utf-8"))
        self.assertEqual(decision_data["decision"], "NO_PROMOTE")
        self.assertEqual(decision_data["execution_source"], "pipeline_self_test")
        self.assertIn("capability_gate", decision_data["failed_gates"])
        self.assertIn("evidence_gate", decision_data["failed_gates"])
        self.assertIn("capability_unsupported", decision_data["reasons"])
        self.assertIn("evidence_gate_failed", decision_data["reasons"])
        self.assertFalse(decision_data["gate_results"]["evidence_gate"]["passed"])
        self.assertEqual(decision_data["gate_results"]["evidence_gate"]["status"], "failed")
        self.assertTrue(decision_data["gate_results"]["fallback_gate"]["passed"])


if __name__ == "__main__":
    unittest.main()
