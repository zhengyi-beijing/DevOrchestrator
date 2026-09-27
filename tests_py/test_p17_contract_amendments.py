"""P17 Contract amendments, legacy classifications, and runtime root fixture tests.

Validates:
1. SAFETY vs POLICY classifications for conflicting legacy assertions loaded from document/module.
2. AST resolution of all named covering tests in the amendment matrix.
3. Retirement disposition of duplicated budget paths without claiming production deletion.
4. Fail-closed resolution of runtime root and state-root ownership lock using convergence.roots.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dev_orchestrator.convergence.amendments import (
    LEGACY_CONTRACT_AMENDMENTS,
    RETIREMENT_DISPOSITIONS,
    load_legacy_contract_amendments,
    load_retirement_dispositions,
    validate_amendment_test_references,
)
from dev_orchestrator.convergence.roots import (
    acquire_state_root_lock,
    resolve_runtime_root,
)


class TestP17ContractAmendments(unittest.TestCase):
    def setUp(self) -> None:
        self.doc_path = Path("docs/P17_LEGACY_CONTRACT_AMENDMENTS.md")

    def test_legacy_contract_amendments_structure_and_document_sync(self) -> None:
        loaded = load_legacy_contract_amendments(self.doc_path)
        self.assertGreaterEqual(len(loaded), 5)
        for entry in loaded:
            self.assertIn(entry["classification"], ("SAFETY", "POLICY"))
            self.assertTrue(len(entry["covering_tests"]) > 0)
            self.assertTrue(bool(entry["reason"]))

    def test_all_amendment_covering_tests_resolve_via_ast(self) -> None:
        """Finding 8: Programmatically verify all covering test references via AST."""
        loaded = load_legacy_contract_amendments(self.doc_path)
        errors = validate_amendment_test_references(".", loaded)
        self.assertEqual(errors, [], f"Covering tests failed AST validation: {errors}")

    def test_retirement_dispositions_do_not_claim_p17_deletion(self) -> None:
        dispositions = load_retirement_dispositions(self.doc_path)
        for target, info in dispositions.items():
            self.assertEqual(info["disposition"], "RETIRE_AT_M9")
            self.assertFalse(info["deleted_in_p17"])

    def test_isolated_fixture_runtime_root_fail_closed_and_ownership_lock(self) -> None:
        """Finding 8: Isolated fixture testing convergence.roots fail-closed resolution and state-root lock."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_controller = tmp_path / "controller"
            fake_workspace = tmp_path / "workspace"
            fake_state = tmp_path / "canonical_state"

            fake_controller.mkdir()
            fake_workspace.mkdir()
            fake_state.mkdir()

            # 1. Fail-closed on missing explicit canonical state root
            with self.assertRaises(RuntimeError) as ctx:
                resolve_runtime_root(None)
            self.assertIn("FAIL_CLOSED", str(ctx.exception))

            non_existent = tmp_path / "non_existent"
            with self.assertRaises(RuntimeError) as ctx:
                resolve_runtime_root(non_existent)
            self.assertIn("FAIL_CLOSED", str(ctx.exception))

            resolved = resolve_runtime_root(fake_state)
            self.assertEqual(resolved, fake_state)

            # 2. Acquire lock records host, pid, and controller identity
            lock_res = acquire_state_root_lock(fake_state, fake_controller, 99999, host="ZXZ-PC")
            self.assertTrue(lock_res)

            lock_file = fake_state / "state_root_owner.lock"
            self.assertTrue(lock_file.exists())
            lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
            self.assertEqual(lock_data["host"], "ZXZ-PC")
            self.assertEqual(lock_data["pid"], 99999)
            self.assertEqual(lock_data["controller_root"], str(fake_controller))

            # 3. Same controller re-verifying lock succeeds
            same_res = acquire_state_root_lock(fake_state, fake_controller, 99999, host="ZXZ-PC")
            self.assertTrue(same_res)

            # 4. A second daemon with different controller root must fail closed
            with self.assertRaises(RuntimeError) as ctx:
                acquire_state_root_lock(fake_state, fake_workspace, 88888)
            self.assertIn("LOCKED", str(ctx.exception))
