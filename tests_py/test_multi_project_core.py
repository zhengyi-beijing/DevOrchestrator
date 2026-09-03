import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.monitor.project import run_monitor_once


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def make_repo(root: Path) -> None:
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "next.md").write_text("# T1\nStatus: ACCEPTED / awaiting\n", encoding="utf-8")
    (root / "agent" / "CURRENT.md").write_text("- S2 NOT STARTED\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")


def bound_project(project_id: str, root: Path, binding_id: str) -> dict:
    return {
        "project_id": project_id,
        "repo_path": str(root),
        "worker_runtime": "tmp/worker-dsh",
        "adapter": "agent_files",
        "conversation_binding": {
            "transport": "browser_bridge",
            "adapter": "chatgpt_web",
            "binding_id": binding_id,
        },
    }
class MultiProjectCoreTests(unittest.TestCase):
    def test_two_projects_are_isolated_and_bound_independently(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo_a, repo_b = base / "repo-a", base / "repo-b"
            make_repo(repo_a)
            make_repo(repo_b)
            config = {
                "projects": [
                    bound_project("xray-hw-platform", repo_a, "conversation-A"),
                    bound_project("labdemo", repo_b, "conversation-B"),
                ]
            }
            path = base / "projects.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_projects_config(path)
            self.assertEqual([p["project_id"] for p in loaded["projects"]], ["xray-hw-platform", "labdemo"])

            summary = run_monitor_once(path, base / "runtime")
            self.assertEqual(summary["project_count"], 2)
            snaps = {item["id"]: item for item in summary["projects"]}
            self.assertEqual(snaps["xray-hw-platform"]["conversation_binding"]["binding_id"], "conversation-A")
            self.assertEqual(snaps["labdemo"]["conversation_binding"]["binding_id"], "conversation-B")
            self.assertTrue(snaps["xray-hw-platform"]["orchestration_ready"])
            self.assertTrue(snaps["labdemo"]["orchestration_ready"])
            self.assertEqual(snaps["xray-hw-platform"]["adapter"], "agent_files")
    def test_legacy_project_is_monitorable_but_not_orchestration_ready(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "legacy"
            make_repo(repo)
            path = base / "projects.json"
            path.write_text(json.dumps({"projects": [{"id": "legacy", "root": str(repo)}]}), encoding="utf-8")
            loaded = load_projects_config(path)
            project = loaded["projects"][0]
            self.assertEqual(project["project_id"], "legacy")
            self.assertEqual(project["repo_path"], str(repo))
            self.assertFalse(project["orchestration_ready"])

    def test_duplicate_project_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            path.write_text(json.dumps({"projects": [
                {"project_id": "same", "repo_path": "A"},
                {"project_id": "same", "repo_path": "B"},
            ]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_projects_config(path)


if __name__ == "__main__":
    unittest.main()

# ProjectAdapter selection is configuration-driven; unknown adapters fail closed.
def _adapter_fail_closed_case(testcase: unittest.TestCase) -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "repo"
        make_repo(repo)
        config = {"projects": [bound_project("p", repo, "C") | {"adapter": "unknown"}]}
        path = base / "projects.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        summary = run_monitor_once(path, base / "runtime")
        snap = summary["projects"][0]
        testcase.assertEqual(snap["state"], "MONITOR_ERROR")
        testcase.assertIn("unknown project adapter", snap["error"].lower())

MultiProjectCoreTests.test_unknown_adapter_fails_closed = _adapter_fail_closed_case

