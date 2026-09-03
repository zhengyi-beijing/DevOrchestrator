import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.config import load_projects_config


class BridgeBindingConfigTests(unittest.TestCase):
    def test_duplicate_ready_conversation_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            binding = {"transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-same"}
            data = {"projects": [
                {"project_id": "a", "repo_path": "A", "conversation_binding": binding},
                {"project_id": "b", "repo_path": "B", "conversation_binding": binding},
            ]}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate conversation binding"):
                load_projects_config(path)
    def test_distinct_bindings_remain_independent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            data = {"projects": [
                {"project_id": "a", "repo_path": "A", "conversation_binding": {"transport":"browser_bridge","adapter":"chatgpt_web","binding_id":"conv-A"}},
                {"project_id": "b", "repo_path": "B", "conversation_binding": {"transport":"browser_bridge","adapter":"chatgpt_web","binding_id":"conv-B"}},
            ]}
            path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_projects_config(path)
            self.assertTrue(all(p["orchestration_ready"] for p in cfg["projects"]))
            self.assertEqual(cfg["projects"][0]["conversation_binding"]["binding_id"], "conv-A")
            self.assertEqual(cfg["projects"][1]["conversation_binding"]["binding_id"], "conv-B")


if __name__ == "__main__":
    unittest.main()
