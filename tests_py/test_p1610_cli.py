import json
import os
import shutil
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.cli import main
from dev_orchestrator.incidents.capture import capture_incident
from dev_orchestrator.incidents.candidate import generate_candidate


class TestIncidentCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.owner_repo = Path(self.temp_dir) / "owner"
        self.owner_repo.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init", str(self.owner_repo)], check=True, capture_output=True)
        (self.owner_repo / "pyproject.toml").write_text('[project]\nname = "dev-orchestrator"\n', encoding="utf-8")
        src_pkg = self.owner_repo / "src" / "dev_orchestrator"
        src_pkg.mkdir(parents=True, exist_ok=True)
        (src_pkg / "__init__.py").write_text("", encoding="utf-8")
        (self.owner_repo / "tests_py").mkdir(parents=True, exist_ok=True)
        (self.owner_repo / "tests_py" / "__init__.py").write_text("", encoding="utf-8")

        subprocess.run(["git", "-C", str(self.owner_repo), "config", "user.name", "Tester"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.owner_repo), "config", "user.email", "tester@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.owner_repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.owner_repo), "commit", "-m", "init"], check=True, capture_output=True)

        self.config_path = self.runtime_root / "projects.json"
        self.config_path.write_text(json.dumps({
            "projects": [
                {
                    "project_id": "p-cli",
                    "repo_path": str(self.owner_repo),
                    "regression_owner": True,
                }
            ]
        }), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cli_incident_and_candidate_lifecycle(self):
        # 1. Capture incident
        fam = capture_incident(
            self.runtime_root,
            project_id="p-cli",
            task_id="t-cli",
            classification="WATCHDOG_RECOVERY",
            semantic={"failure_class": "stall", "diagnosis_code": "hung"},
            evidence={"info": "sample"},
            occurrence_key="cli-occ-1",
        )
        family_id = fam["family_id"]

        # incident-list
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["incident-list", "--runtime-root", str(self.runtime_root)])
            self.assertEqual(rc, 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["family_id"], family_id)

        # incident-show
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["incident-show", family_id, "--runtime-root", str(self.runtime_root)])
            self.assertEqual(rc, 0)
            show_data = json.loads(out.getvalue())
            self.assertEqual(show_data["family"]["family_id"], family_id)
            self.assertIsNotNone(show_data["packet"])

        # 2. Generate candidate
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-cli-1",
            generated_by="auto_agent",
        )

        # candidate-list
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["candidate-list", "--runtime-root", str(self.runtime_root)])
            self.assertEqual(rc, 0)
            c_rows = json.loads(out.getvalue())
            c_cand_ids = [c["candidate_id"] for c in c_rows]
            self.assertIn("cand-cli-1", c_cand_ids)

        # candidate-evaluate
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["candidate-evaluate", "cand-cli-1", "--runtime-root", str(self.runtime_root)])
            self.assertEqual(rc, 0)
            eval_data = json.loads(out.getvalue())
            self.assertTrue(eval_data["pre_review_digest"])
            self.assertFalse(eval_data["promotable"])

        # candidate-review (accepted by independent reviewer)
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main([
                "candidate-review", "cand-cli-1",
                "--reviewer-id", "human_reviewer_42",
                "--verdict", "ACCEPTED",
                "--notes", "verified regression candidate",
                "--runtime-root", str(self.runtime_root),
            ])
            self.assertEqual(rc, 0)
            rev_data = json.loads(out.getvalue())
            self.assertEqual(rev_data["verdict"], "accepted")

        # candidate-materialize
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main([
                "candidate-materialize", "cand-cli-1",
                "--config", str(self.config_path),
                "--runtime-root", str(self.runtime_root),
            ])
            self.assertEqual(rc, 0)
            mat_data = json.loads(out.getvalue())
            self.assertTrue(mat_data["materialized"])

        # candidate-promote
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main([
                "candidate-promote", "cand-cli-1",
                "--config", str(self.config_path),
                "--runtime-root", str(self.runtime_root),
            ])
            self.assertEqual(rc, 0)
            promo_data = json.loads(out.getvalue())
            self.assertTrue(promo_data["promoted"])
            self.assertEqual(promo_data["code"], "promoted")


if __name__ == "__main__":
    unittest.main()
