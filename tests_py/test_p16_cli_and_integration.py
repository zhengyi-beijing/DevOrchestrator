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


if __name__ == "__main__":
    unittest.main()
