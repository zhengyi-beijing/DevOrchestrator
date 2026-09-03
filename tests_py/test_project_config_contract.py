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


if __name__ == "__main__":
    unittest.main()
