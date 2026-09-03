import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole
from tests_py.test_daemon import ROOT, free_port, make_repo, run_cli


class MultiProjectReviewerRegressions(unittest.TestCase):
    def test_project_requires_nonempty_repo_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            path.write_text(json.dumps({"projects": [
                {"project_id": "p", "conversation_binding": {
                    "transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "C"
                }}
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "repo path"):
                load_projects_config(path)

    def test_websol_identity_fields_and_task_stage_must_be_nonempty(self):
        base = dict(
            project_id="p", request_id="r", task_id="T1", stage_id="S1",
            branch="main", head="abc", role=WebSolRole.REVIEWER,
            event=WebSolEvent.REVIEW_REQUIRED, nonce="n",
        )
        for field in ("project_id", "request_id", "branch", "head", "nonce"):
            values = dict(base)
            values[field] = ""
            with self.assertRaises(ValueError, msg=field):
                WebSolRequest(**values)
        with self.assertRaises(ValueError):
            WebSolRequest(**(base | {"task_id": "", "stage_id": ""}))
    def test_unified_daemon_is_mutually_exclusive_with_legacy_processes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runtime = base / "runtime"
            repo = base / "repo"
            make_repo(repo)
            config_path = base / "projects.json"
            config_path.write_text(json.dumps({"projects": [{
                "project_id": "p", "repo_path": str(repo),
                "conversation_binding": {
                    "transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "C"
                },
            }]}), encoding="utf-8")
            daemon_port = free_port()
            started = run_cli(
                "start-daemon", "--config", str(config_path), "--runtime-root", str(runtime),
                "--web-root", str(ROOT / "web"), "--listen", "127.0.0.1",
                "--port", str(daemon_port), "--interval", "5",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            try:
                monitor = run_cli("start-monitor", "--config", str(config_path), "--runtime-root", str(runtime), "--interval", "5")
                self.assertNotEqual(monitor.returncode, 0, "legacy monitor must not run beside unified daemon")
                web = run_cli(
                    "start-web", "--runtime-root", str(runtime), "--web-root", str(ROOT / "web"),
                    "--listen", "127.0.0.1", "--port", str(free_port()),
                )
                self.assertNotEqual(web.returncode, 0, "legacy web must not run beside unified daemon")
            finally:
                run_cli("stop-daemon", "--runtime-root", str(runtime))
            legacy_port = free_port()
            legacy = run_cli(
                "start-web", "--runtime-root", str(runtime), "--web-root", str(ROOT / "web"),
                "--listen", "127.0.0.1", "--port", str(legacy_port),
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            try:
                daemon_again = run_cli(
                    "start-daemon", "--config", str(config_path), "--runtime-root", str(runtime),
                    "--web-root", str(ROOT / "web"), "--listen", "127.0.0.1",
                    "--port", str(free_port()), "--interval", "5",
                )
                self.assertNotEqual(daemon_again.returncode, 0, "unified daemon must reject a live legacy web process")
            finally:
                run_cli("stop-web", "--runtime-root", str(runtime))


if __name__ == "__main__":
    unittest.main()
