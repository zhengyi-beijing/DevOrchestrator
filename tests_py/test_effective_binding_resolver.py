import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.control.binding_resolver import (
    EffectiveBindingConflictError,
    resolve_effective_projects,
)
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.monitor.project import run_monitor_once


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def make_repo(root: Path) -> None:
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "next.md").write_text(
        "# P1\nStatus: DESIGN READY / EXECUTABLE\n", encoding="utf-8"
    )
    (root / "agent" / "CURRENT.md").write_text("- P1 NOT STARTED\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")


def static_project(project_id: str, binding_id: str | None) -> dict:
    project = {
        "project_id": project_id,
        "repo_path": "C:/repo/" + project_id,
        "adapter": "agent_files",
        "orchestration_ready": binding_id is not None,
    }
    if binding_id is not None:
        project["conversation_binding"] = {
            "transport": "browser_bridge",
            "adapter": "chatgpt_web",
            "binding_id": binding_id,
        }
    return project


class EffectiveBindingResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name) / "runtime"
        self.store = ConversationControlStore(self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def discover_and_bind(self, project_id: str, binding_id: str) -> None:
        self.store.heartbeat(
            "chatgpt_web", binding_id,
            title=project_id,
            url="https://chatgpt.com/c/" + binding_id,
            tab_instance_id="tab-" + binding_id,
        )
        self.store.bind(project_id, "chatgpt_web", binding_id)

    def test_runtime_binding_overrides_static_binding(self):
        self.discover_and_bind("labdemo", "runtime-conv")
        [project] = resolve_effective_projects(
            [static_project("labdemo", "static-conv")], self.store
        )
        self.assertEqual(project["conversation_binding"]["binding_id"], "runtime-conv")
        self.assertEqual(project["conversation_binding_source"], "runtime")
        self.assertTrue(project["orchestration_ready"])

    def test_static_binding_is_migration_fallback(self):
        [project] = resolve_effective_projects(
            [static_project("labdemo", "static-conv")], self.store
        )
        self.assertEqual(project["conversation_binding"]["binding_id"], "static-conv")
        self.assertEqual(project["conversation_binding_source"], "static")
        self.assertTrue(project["orchestration_ready"])

    def test_missing_binding_stays_monitor_only(self):
        [project] = resolve_effective_projects(
            [static_project("labdemo", None)], self.store
        )
        self.assertIsNone(project.get("conversation_binding"))
        self.assertEqual(project["conversation_binding_source"], "none")
        self.assertFalse(project["orchestration_ready"])

    def test_explicit_runtime_unbind_suppresses_static_fallback(self):
        self.store.unbind("labdemo")
        [project] = resolve_effective_projects(
            [static_project("labdemo", "static-conv")], self.store
        )
        self.assertIsNone(project.get("conversation_binding"))
        self.assertEqual(project["conversation_binding_source"], "runtime_unbound")
        self.assertFalse(project["orchestration_ready"])

    def test_malformed_runtime_record_suppresses_static_fallback(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "conversation-bindings.json").write_text(
            json.dumps({"labdemo": {"project_id": "labdemo", "adapter": ""}}),
            encoding="utf-8",
        )
        [project] = resolve_effective_projects(
            [static_project("labdemo", "static-conv")], self.store
        )
        self.assertIsNone(project.get("conversation_binding"))
        self.assertEqual(project["conversation_binding_source"], "runtime_invalid")
        self.assertFalse(project["orchestration_ready"])

    def test_effective_routes_reject_runtime_static_collision(self):
        self.discover_and_bind("labdemo", "shared-conv")
        projects = [
            static_project("labdemo", "old-labdemo"),
            static_project("xray-hw-platform", "shared-conv"),
        ]
        with self.assertRaises(EffectiveBindingConflictError):
            resolve_effective_projects(projects, self.store)

    def test_monitor_snapshot_uses_runtime_binding(self):
        base = Path(self.tmp.name)
        repo = base / "repo"
        make_repo(repo)
        config_path = base / "projects.json"
        config_path.write_text(json.dumps({"projects": [{
            "project_id": "labdemo",
            "repo_path": str(repo),
            "worker_runtime": "tmp/worker-dsh",
            "adapter": "agent_files",
            "conversation_binding": {
                "transport": "browser_bridge",
                "adapter": "chatgpt_web",
                "binding_id": "static-conv",
            },
        }]}), encoding="utf-8")
        store = ConversationControlStore(base / "runtime")
        store.heartbeat(
            "chatgpt_web", "runtime-conv", title="LabDemo",
            url="https://chatgpt.com/c/runtime-conv", tab_instance_id="tab-1",
        )
        store.bind("labdemo", "chatgpt_web", "runtime-conv")
        summary = run_monitor_once(config_path, base / "runtime")
        snapshot = summary["projects"][0]
        self.assertEqual(snapshot["conversation_binding"]["binding_id"], "runtime-conv")
        self.assertEqual(snapshot["conversation_binding_source"], "runtime")
        self.assertTrue(snapshot["orchestration_ready"])


if __name__ == "__main__":
    unittest.main()
