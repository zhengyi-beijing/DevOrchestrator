import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import dev_orchestrator.core.staged_roadmap as staged_roadmap
from dev_orchestrator.core.staged_roadmap import (
    RoadmapResult,
    read_raw,
    read_successor,
    sha256_bytes,
)
from dev_orchestrator.monitor.telemetry import extract_task_id


def make_staged_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    staged = repo / "agent" / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    return repo


def valid_spec_text(task_id: str = "P11b") -> str:
    return (
        f"# {task_id} Bounded Follow-up Task\n\n"
        "Status: **PENDING DESIGN**\n\n"
        "Owner authorization: **START P11b / 2026-09-13**\n\n"
        "Goal: follow-up goal description.\n"
    )


class TestStagedRoadmap(unittest.TestCase):
    def test_imports_extract_task_id(self):
        self.assertIs(staged_roadmap.extract_task_id, extract_task_id)

    def test_absent_roadmap_file_returns_absent(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "absent")

    def test_bad_json_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            (repo / "agent" / "staged" / "roadmap.json").write_text("{bad json", encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("cannot parse", result.reason or "")

    def test_schema_version_not_1_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {"schema_version": 2, "tasks": []}
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("schema_version", result.reason or "")

    def test_tasks_not_a_list_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {"schema_version": 1, "tasks": {}}
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("tasks must be a list", result.reason or "")

    def test_blank_task_id_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {"schema_version": 1, "tasks": [{"task_id": "   ", "successor": None, "successor_spec_path": None}]}
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("task entry missing non-blank task_id", result.reason or "")

    def test_duplicate_task_id_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [
                    {"task_id": "P11x", "successor": None, "successor_spec_path": None},
                    {"task_id": "P11x", "successor": None, "successor_spec_path": None},
                ],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("duplicate task_id", result.reason or "")

    def test_missing_completed_task_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P1", "successor": None, "successor_spec_path": None}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("completed task P11x not found", result.reason or "")

    def test_only_one_null_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            # Case 1: successor set, path null
            data1 = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": None}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data1), encoding="utf-8")
            res1 = read_successor(repo, "P11x")
            self.assertEqual(res1.kind, "invalid")
            self.assertIn("only one of successor and successor_spec_path is null", res1.reason or "")

            # Case 2: successor null, path set
            data2 = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": None, "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data2), encoding="utf-8")
            res2 = read_successor(repo, "P11x")
            self.assertEqual(res2.kind, "invalid")
            self.assertIn("only one of successor and successor_spec_path is null", res2.reason or "")

    def test_self_successor_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11x", "successor_spec_path": "agent/staged/P11x.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("successor cannot be equal to completed_task_id", result.reason or "")

    def test_dot_dot_escape_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/../staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("contains '../' traversal", result.reason or "")

    def test_absolute_path_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "/agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("cannot be absolute", result.reason or "")

    def test_non_md_path_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.txt"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("must end in .md", result.reason or "")

    def test_missing_spec_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("successor spec file does not exist", result.reason or "")

    def test_crlf_spec_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            spec_content = valid_spec_text("P11b").replace("\n", "\r\n").encode("utf-8")
            (repo / "agent" / "staged" / "P11b.md").write_bytes(spec_content)
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("contains CRLF/CR line endings", result.reason or "")

    def test_non_utf8_spec_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            (repo / "agent" / "staged" / "P11b.md").write_bytes(b"\xff\xfe\x00\x00bad")
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("not valid UTF-8", result.reason or "")

    def test_title_id_mismatch_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            (repo / "agent" / "staged" / "P11b.md").write_bytes(valid_spec_text("P999").encode("utf-8"))
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("title task id mismatch", result.reason or "")

    def test_non_pending_status_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            text = valid_spec_text("P11b").replace("Status: **PENDING DESIGN**", "Status: **READY_TO_RUN**")
            (repo / "agent" / "staged" / "P11b.md").write_bytes(text.encode("utf-8"))
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("missing Status: **PENDING DESIGN**", result.reason or "")

    def test_approved_marker_present_returns_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            text = valid_spec_text("P11b") + "\n## Approved executable design\nApproved."
            (repo / "agent" / "staged" / "P11b.md").write_bytes(text.encode("utf-8"))
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "invalid")
            self.assertIn("already contains approved design marker", result.reason or "")

    def test_null_successor_returns_end_of_roadmap(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11d", "successor": None, "successor_spec_path": None}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            result = read_successor(repo, "P11d")
            self.assertEqual(result.kind, "end_of_roadmap")
            self.assertIsNone(result.successor_task_id)

    def test_valid_entry_returns_successor(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_staged_repo(Path(td))
            data = {
                "schema_version": 1,
                "tasks": [{"task_id": "P11x", "successor": "P11b", "successor_spec_path": "agent/staged/P11b.md"}],
            }
            (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
            raw_spec = valid_spec_text("P11b").encode("utf-8")
            (repo / "agent" / "staged" / "P11b.md").write_bytes(raw_spec)
            result = read_successor(repo, "P11x")
            self.assertEqual(result.kind, "successor")
            self.assertEqual(result.successor_task_id, "P11b")
            self.assertEqual(result.spec_path, "agent/staged/P11b.md")
            self.assertEqual(result.spec_sha256, hashlib.sha256(raw_spec).hexdigest().lower())
            self.assertEqual(result.spec_text, raw_spec.decode("utf-8"))

    def test_real_repo_roadmap_links(self):
        checkout_root = Path(__file__).resolve().parent.parent
        res_x = read_successor(checkout_root, "P11x")
        self.assertEqual(res_x.kind, "successor")
        self.assertEqual(res_x.successor_task_id, "P11b")
        self.assertEqual(res_x.spec_path, "agent/staged/P11b.md")

        res_b = read_successor(checkout_root, "P11b")
        self.assertEqual(res_b.kind, "successor")
        self.assertEqual(res_b.successor_task_id, "P11c")
        self.assertEqual(res_b.spec_path, "agent/staged/P11c.md")

        res_c = read_successor(checkout_root, "P11c")
        self.assertEqual(res_c.kind, "successor")
        self.assertEqual(res_c.successor_task_id, "P11d")
        self.assertEqual(res_c.spec_path, "agent/staged/P11d.md")

        res_d = read_successor(checkout_root, "P11d")
        self.assertEqual(res_d.kind, "successor")
        self.assertEqual(res_d.successor_task_id, "P12")
        self.assertEqual(res_d.spec_path, "agent/staged/P12.md")

        res_12 = read_successor(checkout_root, "P12")
        self.assertEqual(res_12.kind, "end_of_roadmap")
        self.assertIsNone(res_12.successor_task_id)

        res_125 = read_successor(checkout_root, "P12.5")
        self.assertEqual(res_125.kind, "successor")
        self.assertEqual(res_125.successor_task_id, "P12.6")
        self.assertEqual(res_125.spec_path, "agent/staged/P12.6.md")


if __name__ == "__main__":
    unittest.main()
