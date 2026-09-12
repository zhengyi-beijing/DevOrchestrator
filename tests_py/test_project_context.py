import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.project_context import (
    CONTEXT_DOMAINS,
    FORBIDDEN_SECRET_MARKERS,
    PROJECT_CONTEXT_SCHEMA_VERSION,
    context_prompt_block,
    context_status,
    load_declared_document,
    load_supplement,
    render_context_block,
    resolve_document_path,
    resolve_project_context,
    validate_context_document,
)


def _make_valid_payload(project_id="test-proj"):
    return {
        "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
        "project_id": project_id,
        "updated_at": "2026-09-12T00:00:00+00:00",
        "goals": ["Ship reliable local orchestration."],
        "architecture": ["Core modules under src/dev_orchestrator/core."],
        "protected_scope": ["Production infrastructure."],
        "safety_constraints": ["Always fail closed."],
        "validation_commands": ["python -m unittest"],
        "runtime_assumptions": ["Python 3.11+"],
        "key_decisions": ["Unified daemon per host."],
    }


class ProjectContextUnitTests(unittest.TestCase):
    def test_valid_document_resolution_and_stable_digest(self):
        payload = _make_valid_payload()
        doc1, err1 = validate_context_document(payload, expected_project_id="test-proj")
        self.assertIsNotNone(doc1)
        self.assertEqual(err1, "")
        self.assertEqual(len(doc1.digest), 16)

        # Determinism check across repeated validations
        doc2, err2 = validate_context_document(payload, expected_project_id="test-proj")
        self.assertEqual(doc1.digest, doc2.digest)
        self.assertEqual(doc1.domains, doc2.domains)

    def test_unknown_top_level_key_rejected(self):
        payload = _make_valid_payload()
        payload["unexpected_extra"] = 123
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("unknown top-level keys", err)

    def test_unsupported_schema_version_rejected(self):
        payload = _make_valid_payload()
        payload["schema_version"] = 2
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertEqual(err, "unsupported project context schema_version")

        payload["schema_version"] = "1"
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("schema_version must be an integer", err)

    def test_missing_required_domains_rejected(self):
        payload = _make_valid_payload()
        del payload["goals"]
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("missing required context domains", err)

    def test_non_list_domain_rejected(self):
        payload = _make_valid_payload()
        payload["goals"] = "not a list"
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("must be a list", err)

    def test_oversized_domain_and_entry_rejection(self):
        # > 40 entries
        payload = _make_valid_payload()
        payload["goals"] = [f"Goal {i}" for i in range(41)]
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("exceeds 40 entries", err)

        # > 600 chars entry
        payload = _make_valid_payload()
        payload["goals"] = ["A" * 601]
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("exceeds 600 characters", err)

    def test_blank_and_non_string_entry_rejection(self):
        payload = _make_valid_payload()
        payload["goals"] = ["   "]
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("non-blank string", err)

        payload["goals"] = [123]
        doc, err = validate_context_document(payload)
        self.assertIsNone(doc)
        self.assertIn("non-blank string", err)

    def test_secret_marker_rejection(self):
        for marker in FORBIDDEN_SECRET_MARKERS:
            payload = _make_valid_payload()
            payload["goals"] = [f"Use my {marker} here"]
            doc, err = validate_context_document(payload)
            self.assertIsNone(doc)
            self.assertIn("contains forbidden secret marker", err)

    def test_project_id_mismatch_rejected(self):
        payload = _make_valid_payload(project_id="actual-proj")
        doc, err = validate_context_document(payload, expected_project_id="other-proj")
        self.assertIsNone(doc)
        self.assertIn("project_id mismatch", err)

        # Matching passes
        doc_ok, err_ok = validate_context_document(payload, expected_project_id="actual-proj")
        self.assertIsNotNone(doc_ok)
        self.assertEqual(err_ok, "")

    def test_document_path_escaping_repository_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            path, err = resolve_document_path(repo, "../outside.json")
            self.assertIsNone(path)
            self.assertIn("escapes repository root", err)

            path, err = resolve_document_path(repo, "/absolute/path.json")
            self.assertIsNone(path)
            self.assertIn("must be relative to repository", err)

            path, err = resolve_document_path(repo, "C:\\absolute\\path.json")
            self.assertIsNone(path)

            path, err = resolve_document_path(repo, "valid/sub/path.json")
            self.assertIsNotNone(path)
            self.assertEqual(err, "")
            self.assertEqual(path, (repo / "valid" / "sub" / "path.json").resolve())

    def test_absent_declaration_yields_state_absent(self):
        project = {"project_id": "p1", "repo_path": "C:\\fake"}
        res = resolve_project_context(project)
        self.assertEqual(res.state, "absent")
        self.assertIsNone(res.document)
        prompt, block_res = context_prompt_block(project, "worker")
        self.assertEqual(prompt, "")
        self.assertEqual(block_res.state, "absent")

        project_disabled = {"project_id": "p1", "repo_path": "C:\\fake", "project_context": {"enabled": False}}
        res2 = resolve_project_context(project_disabled)
        self.assertEqual(res2.state, "absent")

    def test_supplement_fills_empty_domains_and_never_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            declared_file = repo / "agent" / "project-context.json"
            declared_file.parent.mkdir(parents=True)
            supplement_file = repo / "agent" / "generated.json"

            declared_payload = _make_valid_payload()
            declared_payload["architecture"] = []  # Empty domain in declared
            declared_file.write_text(json.dumps(declared_payload), encoding="utf-8")

            supplement_payload = {
                "schema_version": 1,
                "goals": ["Supplemental goal that should NOT override declared"],
                "architecture": ["Supplemental architecture that SHOULD fill empty declared"],
            }
            supplement_file.write_text(json.dumps(supplement_payload), encoding="utf-8")

            project = {
                "project_id": "test-proj",
                "repo_path": str(repo),
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "supplement_path": "agent/generated.json",
                },
            }
            res = resolve_project_context(project)
            self.assertEqual(res.state, "ready")
            self.assertTrue(res.status["supplement_used"])
            self.assertEqual(res.document.provenance["goals"], "declared")
            self.assertEqual(res.document.domains["goals"], ("Ship reliable local orchestration.",))
            self.assertEqual(res.document.provenance["architecture"], "generated")
            self.assertEqual(
                res.document.domains["architecture"],
                ("Supplemental architecture that SHOULD fill empty declared",),
            )

    def test_missing_supplement_is_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            declared_file = repo / "agent" / "project-context.json"
            declared_file.parent.mkdir(parents=True)
            declared_file.write_text(json.dumps(_make_valid_payload()), encoding="utf-8")

            project = {
                "project_id": "test-proj",
                "repo_path": str(repo),
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "supplement_path": "agent/missing-supplement.json",
                },
            }
            res = resolve_project_context(project)
            self.assertEqual(res.state, "ready")
            self.assertFalse(res.status["supplement_used"])
            self.assertIsNotNone(res.document)

    def test_rendering_determinism_delimiters_and_truncation(self):
        payload = _make_valid_payload()
        doc, _ = validate_context_document(payload)
        rendered1 = render_context_block(doc, max_chars=6000)
        rendered2 = render_context_block(doc, max_chars=6000)
        self.assertEqual(rendered1, rendered2)
        self.assertTrue(rendered1.startswith("[PROJECT_CONTEXT_BEGIN]\n"))
        self.assertTrue(rendered1.endswith("\n[PROJECT_CONTEXT_END]"))
        self.assertIn("schema_version=1", rendered1)
        self.assertIn("digest=", rendered1)
        self.assertIn("## goals\n- Ship reliable local orchestration.", rendered1)
        self.assertNotIn("[PROJECT_CONTEXT_TRUNCATED]", rendered1)

        # Truncation test with tight budget
        truncated = render_context_block(doc, max_chars=500)
        self.assertIn("[PROJECT_CONTEXT_TRUNCATED]", truncated)
        self.assertTrue(truncated.endswith("\n[PROJECT_CONTEXT_TRUNCATED]\n[PROJECT_CONTEXT_END]"))
        self.assertLessEqual(len(truncated), 500)

    def test_role_filtering_in_context_prompt_block(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            declared_file = repo / "agent" / "project-context.json"
            declared_file.parent.mkdir(parents=True)
            declared_file.write_text(json.dumps(_make_valid_payload()), encoding="utf-8")

            project = {
                "project_id": "test-proj",
                "repo_path": str(repo),
                "project_context": {
                    "enabled": True,
                    "inject_roles": ["planner", "worker"],
                },
            }
            worker_block, w_res = context_prompt_block(project, "worker")
            self.assertEqual(w_res.state, "ready")
            self.assertIn("[PROJECT_CONTEXT_BEGIN]", worker_block)

            reviewer_block, r_res = context_prompt_block(project, "reviewer")
            self.assertEqual(r_res.state, "ready")
            self.assertEqual(reviewer_block, "")  # role not in inject_roles

    def test_context_status_metadata_has_no_raw_document_content(self):
        payload = _make_valid_payload()
        doc, _ = validate_context_document(payload)
        from dev_orchestrator.core.project_context import ProjectContextResolution
        res = ProjectContextResolution(
            state="ready",
            reason="context resolved",
            document=doc,
            sources={"document_path": "agent/project-context.json", "supplement_path": None},
            status={
                "schema_version": 1,
                "state": "ready",
                "reason": "context resolved",
                "digest": doc.digest,
                "updated_at": doc.updated_at,
                "document_path": "agent/project-context.json",
                "supplement_used": False,
                "domains": {d: len(doc.domains[d]) for d in CONTEXT_DOMAINS},
            },
        )
        status = context_status(res)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["digest"], doc.digest)
        self.assertIn("domains", status)
        # Check no domain entry text is leaked in status
        for d in CONTEXT_DOMAINS:
            self.assertIsInstance(status["domains"][d], int)
        raw_json = json.dumps(status)
        self.assertNotIn("Ship reliable local orchestration.", raw_json)


if __name__ == "__main__":
    unittest.main()
