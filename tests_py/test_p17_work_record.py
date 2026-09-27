"""P17 Work Record model, validation, canonical serialization, and CAS tests."""
from __future__ import annotations

import json
import unittest

from dev_orchestrator.convergence.work_record import (
    CASConflictError,
    WorkRecord,
    WorkRecordValidationError,
    apply_work_record_cas,
    canonical_json,
    validate_work_record,
    work_record_digest,
)


class TestP17WorkRecord(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_payload = {
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec-digest-1",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {
                "repo_path": "C:\\work\\github\\DevOrchestrator-dev",
                "branch": "main",
            },
            "status": "OPEN",
            "active_lease": None,
            "current_problem": None,
            "attempts": [],
            "acceptance": {"kind": "NONE"},
            "wait": None,
            "verification": None,
            "successor": None,
            "human_request": None,
            "handoff": None,
            "authority_revision": "rev-1",
            "created_at": "2026-09-27T00:00:00Z",
            "updated_at": "2026-09-27T00:00:00Z",
        }

    def test_validate_valid_work_record(self) -> None:
        record = validate_work_record(self.valid_payload)
        self.assertEqual(record.project_id, "devorchestrator")
        self.assertEqual(record.goal_id, "P17")
        self.assertEqual(record.status, "OPEN")
        self.assertEqual(record.acceptance["kind"], "NONE")

    def test_validate_missing_required_fields_fails(self) -> None:
        bad_payload = dict(self.valid_payload)
        del bad_payload["goal_id"]
        with self.assertRaises(WorkRecordValidationError):
            validate_work_record(bad_payload)

    def test_validate_invalid_status_fails(self) -> None:
        bad_payload = dict(self.valid_payload)
        bad_payload["status"] = "INVALID_STATUS"
        with self.assertRaises(WorkRecordValidationError):
            validate_work_record(bad_payload)

    def test_canonical_json_and_digest_stability(self) -> None:
        record1 = validate_work_record(self.valid_payload)
        record2 = validate_work_record(self.valid_payload)
        c_json1 = canonical_json(record1)
        c_json2 = canonical_json(record2)
        self.assertEqual(c_json1, c_json2)
        self.assertEqual(work_record_digest(record1), work_record_digest(record2))

    def test_apply_work_record_cas_success(self) -> None:
        record = validate_work_record(self.valid_payload)
        new_record = apply_work_record_cas(
            record,
            expected_revision="rev-1",
            updates={"status": "NEEDS_HUMAN"},
            now="2026-09-27T01:00:00Z",
        )
        self.assertEqual(new_record.status, "NEEDS_HUMAN")
        self.assertEqual(new_record.authority_revision, "rev-2")
        self.assertEqual(new_record.updated_at, "2026-09-27T01:00:00Z")

    def test_apply_work_record_cas_conflict(self) -> None:
        record = validate_work_record(self.valid_payload)
        with self.assertRaises(CASConflictError):
            apply_work_record_cas(
                record,
                expected_revision="rev-999",
                updates={"status": "DONE"},
            )

    def test_immutable_identity_fields_cannot_mutate(self) -> None:
        record = validate_work_record(self.valid_payload)
        with self.assertRaises(WorkRecordValidationError):
            apply_work_record_cas(
                record,
                expected_revision="rev-1",
                updates={"goal_id": "DIFFERENT_GOAL"},
            )
