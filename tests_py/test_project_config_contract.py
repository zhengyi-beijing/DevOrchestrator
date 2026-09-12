import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.config import load_projects_config


class ProjectConfigContractTests(unittest.TestCase):
    def test_duplicate_canonical_project_id_has_explicit_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            path.write_text(json.dumps({"projects": [
                {"project_id": "same", "repo_path": "A"},
                {"project_id": "same", "repo_path": "B"},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate project id"):
                load_projects_config(path)

    def test_project_context_normalization_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            path.write_text(json.dumps({"projects": [
                {"project_id": "p1", "repo_path": "A", "project_context": {"enabled": True}},
            ]}), encoding="utf-8")
            cfg = load_projects_config(path)
            ctx = cfg["projects"][0]["project_context"]
            self.assertTrue(ctx["enabled"])
            self.assertEqual(ctx["document_path"], "agent/project-context.json")
            self.assertIsNone(ctx["supplement_path"])
            self.assertTrue(ctx["require_valid"])
            self.assertEqual(ctx["max_chars"], 6000)
            self.assertEqual(ctx["inject_roles"], ["planner", "worker", "reviewer"])

    def test_project_context_validation_errors(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"

            # Unknown key
            path.write_text(json.dumps({"projects": [
                {"project_id": "p1", "repo_path": "A", "project_context": {"bad_key": 1}},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown keys"):
                load_projects_config(path)

            # Bad role
            path.write_text(json.dumps({"projects": [
                {"project_id": "p1", "repo_path": "A", "project_context": {"inject_roles": ["bad_role"]}},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid role"):
                load_projects_config(path)

            # Empty roles
            path.write_text(json.dumps({"projects": [
                {"project_id": "p1", "repo_path": "A", "project_context": {"inject_roles": []}},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty list"):
                load_projects_config(path)

            # Out of range max_chars (< 500)
            path.write_text(json.dumps({"projects": [
                {"project_id": "p1", "repo_path": "A", "project_context": {"max_chars": 100}},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "between 500 and 20000"):
                load_projects_config(path)

            # Out of range max_chars (> 20000)
            path.write_text(json.dumps({"projects": [
                {"project_id": "p1", "repo_path": "A", "project_context": {"max_chars": 25000}},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "between 500 and 20000"):
                load_projects_config(path)


if __name__ == "__main__":
    unittest.main()
