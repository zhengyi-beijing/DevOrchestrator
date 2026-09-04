import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.monitor.project import run_monitor_once


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_external_repo(root: Path) -> None:
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "next.md").write_text(
        "# EXT-1\nStatus: ACCEPTED / awaiting\n", encoding="utf-8")
    (root / "agent" / "CURRENT.md").write_text("- EXT NOT STARTED\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "portable@example.invalid")
    git(root, "config", "user.name", "Portable Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")


class PortableIntegrationContractTests(unittest.TestCase):
    def test_relative_repo_path_is_resolved_from_config_directory(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "external-project"
            make_external_repo(repo)
            config_path = base / "projects.json"
            config_path.write_text(json.dumps({"projects": [{
                "project_id": "external",
                "repo_path": "external-project",
            }]}), encoding="utf-8")

            loaded = load_projects_config(config_path)
            project = loaded["projects"][0]
            self.assertEqual(Path(project["repo_path"]), repo.resolve())
            self.assertEqual(project["root"], project["repo_path"])

    def test_external_project_can_be_monitored_by_config_only(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "product-repo"
            make_external_repo(repo)
            config_path = base / "projects.json"
            config_path.write_text(json.dumps({"projects": [{
                "project_id": "product",
                "repo_path": str(repo),
            }]}), encoding="utf-8")
            before = git_status(repo)
            summary = run_monitor_once(config_path, base / "runtime")
            after = git_status(repo)

            self.assertEqual(summary["project_count"], 1)
            self.assertEqual(summary["projects"][0]["project_id"], "product")
            self.assertEqual(summary["projects"][0]["state"], "WAITING_PHASE_GATE")
            self.assertEqual(before, after)

    def test_validate_config_cli_fails_closed_for_bad_repo_or_adapter(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "external"
            make_external_repo(repo)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "src")
            cases = [
                {"project_id": "missing", "repo_path": str(base / "missing")},
                {"project_id": "bad-adapter", "repo_path": str(repo), "adapter": "unknown"},
            ]
            for project in cases:
                with self.subTest(project=project["project_id"]):
                    config_path = base / (project["project_id"] + ".json")
                    config_path.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
                    proc = subprocess.run(
                        [sys.executable, "-m", "dev_orchestrator", "validate-config",
                         "--config", str(config_path)],
                        cwd=ROOT, env=env, text=True, capture_output=True, timeout=10,
                    )
                    self.assertEqual(proc.returncode, 1)
                    self.assertFalse(json.loads(proc.stdout)["valid"])

    def test_validate_config_cli_accepts_monitor_only_external_project(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "external"
            make_external_repo(repo)
            config_path = base / "projects.json"
            config_path.write_text(json.dumps({"projects": [{
                "project_id": "external",
                "repo_path": str(repo),
            }]}), encoding="utf-8")
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "src")
            proc = subprocess.run(
                [sys.executable, "-m", "dev_orchestrator", "validate-config",
                 "--config", str(config_path)],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["project_count"], 1)
            self.assertTrue(payload["projects"][0]["repository_valid"])
            self.assertTrue(payload["projects"][0]["adapter_valid"])
            self.assertFalse(payload["projects"][0]["orchestration_ready"])


def git_status(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True, text=True, capture_output=True,
    ).stdout


if __name__ == "__main__":
    unittest.main()
