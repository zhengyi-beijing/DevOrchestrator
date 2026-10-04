"""Tests for P20.4 Soak Permit and Isolation Boundaries.

Verifies:
- Default-OFF permit loading and validation
- Path isolation (no canonical repo / runtime overlap or escape)
- Fail-closed refusal for missing/mismatched markers, wrong project, invalid source
- Negative proof: no HTTP/MCP/browser-bridge/mobile/control-command paths can enable soak or faults
- Negative proof: faults targeting canonical repo/runtime/processes are refused.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
import unittest

from dev_orchestrator.soak.permit import (
    SOAK_PERMIT_SCHEMA_VERSION,
    SoakPermit,
    create_soak_permit,
    load_soak_permit,
    save_soak_permit,
    validate_soak_permit,
    permit_fingerprint,
    soak_enabled,
)
from dev_orchestrator.soak.faults import (
    assert_target_disposable,
    TargetEscapesDisposableBoundaryError,
)


class TestSoakPermitIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_p20_4_permit_"))
        self.canonical_repo = self.temp_dir / "canonical_repo"
        self.canonical_runtime = self.canonical_repo / "runtime"
        self.canonical_repo.mkdir(parents=True, exist_ok=True)
        self.canonical_runtime.mkdir(parents=True, exist_ok=True)

        self.disposable_dir = self.temp_dir / "disposable_project"
        self.disposable_repo = self.disposable_dir / "repo"
        self.disposable_runtime = self.disposable_dir / "runtime"
        self.disposable_repo.mkdir(parents=True, exist_ok=True)
        self.disposable_runtime.mkdir(parents=True, exist_ok=True)

        # Place disposable marker
        marker_file = self.disposable_repo / ".devorch-disposable-soak"
        marker_file.write_text(json.dumps({
            "run_id": "soak-run-001",
            "project_id": "p20-soak-proj",
        }), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_soak_disabled_by_default_when_permit_absent(self) -> None:
        self.assertFalse(soak_enabled(None))
        ok, reason = validate_soak_permit(None, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertEqual(reason, "permit_absent")

    def test_valid_permit_passes_validation(self) -> None:
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        self.assertTrue(soak_enabled(permit))
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_permit_roundtrip_save_and_load(self) -> None:
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        permit_path = self.disposable_dir / "soak-permit.json"
        save_soak_permit(permit_path, permit)
        loaded = load_soak_permit(permit_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.run_id, permit.run_id)
        self.assertEqual(loaded.fingerprint, permit.fingerprint)

    def test_permit_rejects_unsupported_schema_version(self) -> None:
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        permit.schema_version = 999
        permit.fingerprint = permit_fingerprint(permit)
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertEqual(reason, "unsupported_schema_version")

    def test_permit_rejects_non_local_source(self) -> None:
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        permit.permit_source = "remote_http"
        permit.fingerprint = permit_fingerprint(permit)
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_permit_source")

    def test_permit_rejects_canonical_overlap(self) -> None:
        # Disposable repo inside canonical repo
        overlap_repo = self.canonical_repo / "sub_disposable"
        overlap_repo.mkdir(parents=True, exist_ok=True)
        marker = overlap_repo / ".devorch-disposable-soak"
        marker.write_text(json.dumps({"run_id": "run-01", "project_id": "p20-soak-proj"}), encoding="utf-8")

        permit = create_soak_permit(
            run_id="run-01",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=overlap_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertIn("canonical_overlap", str(reason))

    def test_permit_rejects_missing_marker(self) -> None:
        marker = self.disposable_repo / ".devorch-disposable-soak"
        if marker.exists():
            marker.unlink()
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_disposable_marker")

    def test_permit_rejects_marker_run_id_mismatch(self) -> None:
        marker = self.disposable_repo / ".devorch-disposable-soak"
        marker.write_text(json.dumps({"run_id": "other-run", "project_id": "p20-soak-proj"}), encoding="utf-8")
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertEqual(reason, "marker_run_id_mismatch")

    def test_permit_rejects_tampered_fingerprint(self) -> None:
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        permit.fingerprint = "0" * 64
        ok, reason = validate_soak_permit(permit, self.canonical_repo, self.canonical_runtime)
        self.assertFalse(ok)
        self.assertEqual(reason, "fingerprint_mismatch")

    def test_assert_target_disposable_refuses_canonical_targets(self) -> None:
        permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        # Canonical file
        with self.assertRaises(TargetEscapesDisposableBoundaryError):
            assert_target_disposable(permit, self.canonical_repo / "src" / "dev_orchestrator")

        # Canonical runtime
        with self.assertRaises(TargetEscapesDisposableBoundaryError):
            assert_target_disposable(permit, self.canonical_runtime / "daemon.pid")

        # Disposable targets pass
        assert_target_disposable(permit, self.disposable_repo / "file.py")
        assert_target_disposable(permit, self.disposable_runtime / "test.json")


    def test_remote_surfaces_expose_no_soak_or_fault_actions(self) -> None:
        # 1. MCP tools
        from dev_orchestrator.control.mcp_http import MCP_TOOLS
        mcp_tool_names = [t["name"] for t in MCP_TOOLS]
        self.assertFalse(any("soak" in name for name in mcp_tool_names))
        self.assertFalse(any("fault" in name for name in mcp_tool_names))

        # 2. Control/Web external operation paths
        from dev_orchestrator.web.server import _EXTERNAL_OPERATION_PATHS
        self.assertFalse(any("soak" in p for p in _EXTERNAL_OPERATION_PATHS))
        self.assertFalse(any("fault" in p for p in _EXTERNAL_OPERATION_PATHS))

        # 3. Control commands catalog
        from dev_orchestrator.core.control_commands import ControlCommandCoordinator
        # Verify no soak control commands exist
        self.assertFalse(hasattr(ControlCommandCoordinator, "soak_permit"))


if __name__ == "__main__":
    unittest.main()
