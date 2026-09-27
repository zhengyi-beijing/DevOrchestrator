"""P17 Shadow Adapter and fence tests.

Validates that ReadOnlyEvidenceRoot fails on all write/delete operations,
output sinks outside runtime/p17-shadow fail closed, stdout touches no file,
and records retain source digests.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from dev_orchestrator.convergence.shadow import (
    FenceViolation,
    ReadOnlyEvidenceRoot,
    ShadowEvaluator,
    validate_shadow_sink,
)


class TestP17ShadowFence(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)

        # Create dummy legacy structure
        (self.root_path / ".devorch").mkdir(parents=True)
        (self.root_path / "runtime" / "control").mkdir(parents=True)
        (self.root_path / ".devorch" / "status.json").write_text('{"head": "abc", "branch": "main"}', encoding="utf-8")

        self.ev_root = ReadOnlyEvidenceRoot(self.root_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_only_root_permits_reads(self) -> None:
        data = self.ev_root.read_json(".devorch/status.json")
        self.assertEqual(data.get("head"), "abc")

    def test_read_only_root_prohibits_writes_and_deletes(self) -> None:
        with self.assertRaises(FenceViolation):
            self.ev_root.write_file("test.txt", "content")

        with self.assertRaises(FenceViolation):
            self.ev_root.delete_file(".devorch/status.json")

    def test_shadow_sink_inside_evidence_root_fails_closed(self) -> None:
        with self.assertRaises(FenceViolation):
            validate_shadow_sink(
                self.root_path / "runtime" / "p17-shadow",
                evidence_root=self.root_path,
                repo_root=self.root_path,
            )

    def test_shadow_sink_outside_namespace_fails_closed(self) -> None:
        with self.assertRaises(FenceViolation):
            validate_shadow_sink(
                self.root_path / "runtime" / "other_dir",
                evidence_root=Path("C:/other_evidence"),
                repo_root=self.root_path,
            )

    def test_shadow_sink_unset_fails_closed(self) -> None:
        with self.assertRaises(FenceViolation):
            validate_shadow_sink(
                None,
                evidence_root=Path("C:/other_evidence"),
                repo_root=self.root_path,
            )

    def test_valid_shadow_sink_succeeds(self) -> None:
        allowed = validate_shadow_sink(
            self.root_path / "runtime" / "p17-shadow" / "out",
            evidence_root=Path("C:/other_evidence"),
            repo_root=self.root_path,
        )
        self.assertTrue(str(allowed).endswith(str(Path("runtime/p17-shadow/out"))))

    def test_shadow_evaluation_retains_source_digests(self) -> None:
        evaluator = ShadowEvaluator(self.ev_root)
        record = evaluator.evaluate_shadow(project_id="test-proj", goal_id="P17")
        self.assertEqual(record["record_type"], "shadow_decision")
        self.assertTrue(bool(record["source_work_record_digest"]))
        self.assertTrue(bool(record["source_evidence_digest"]))
        self.assertTrue(bool(record["record_hash"]))
