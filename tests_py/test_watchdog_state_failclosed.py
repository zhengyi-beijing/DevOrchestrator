import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.core.watchdog import (
    MAX_TERMINAL_ATTEMPTS_PER_PROJECT,
    WATCHDOG_SCHEMA_VERSION,
    WatchdogCoordinator,
)


class DummyProgressChannel:
    def __init__(self):
        self.events = []

    def emit(self, project_id, milestone, **kwargs):
        self.events.append((project_id, milestone, kwargs))


class WatchdogStateFailclosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.state_file = self.runtime_dir / "watchdog.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_missing_state_file_loads_clean_default(self):
        """Missing watchdog.json initializes clean default state."""
        coordinator = WatchdogCoordinator(self.runtime_dir)
        st = coordinator.state()
        self.assertEqual(st["version"], WATCHDOG_SCHEMA_VERSION)
        self.assertFalse(st["degraded"])
        self.assertIsNone(st["degraded_reason"])
        self.assertEqual(st["projects"], {})
        self.assertEqual(st["quarantined_projects"], {})

    def test_corrupt_json_quarantined_and_emits_gate(self):
        """Unreadable/corrupt JSON is quarantined, degraded mode set, and OWNER_GATE emitted."""
        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        st = coordinator.state()
        self.assertTrue(st["degraded"])
        self.assertIn("unreadable state JSON", st["degraded_reason"])

        # Corrupt file must be preserved as watchdog.json.corrupt-*
        corrupt_files = list(self.runtime_dir.glob("watchdog.json.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)
        self.assertEqual(corrupt_files[0].read_bytes(), b"NOT VALID JSON {[[")
        self.assertEqual(self.state_file.read_bytes(), b"NOT VALID JSON {[[")

        # Emitted OWNER_GATE notification
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][0], "__controller__")
        self.assertEqual(gate_events[0][2]["details"]["gate"], "corrupt-state")

        coordinator.clear_degraded()
        repaired = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertFalse(repaired["degraded"])
        self.assertIsNone(repaired["degraded_reason"])

    def test_oserror_during_read_quarantines_via_copy_without_zero_byte_fabrication(self):
        raw = b'{"version": "oops", "projects": {}}'
        self.state_file.write_bytes(raw)
        original_read_bytes = Path.read_bytes

        def failing_read_bytes(path_self: Path):
            if path_self == self.state_file:
                raise OSError("permission denied")
            return original_read_bytes(path_self)

        with patch("pathlib.Path.read_bytes", new=failing_read_bytes):
            coordinator = WatchdogCoordinator(self.runtime_dir)

        self.assertTrue(coordinator.state()["degraded"])
        corrupt_files = list(self.runtime_dir.glob("watchdog.json.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)
        self.assertEqual(corrupt_files[0].read_bytes(), raw)
        self.assertEqual(self.state_file.read_bytes(), raw)

    def test_future_schema_version_quarantined(self):
        """Future schema version is quarantined and activates degraded mode."""
        self.state_file.write_text(json.dumps({"version": 999, "projects": {}}), encoding="utf-8")
        coordinator = WatchdogCoordinator(self.runtime_dir)

        st = coordinator.state()
        self.assertTrue(st["degraded"])
        self.assertIn("unsupported schema version 999", st["degraded_reason"])

        corrupt_files = list(self.runtime_dir.glob("watchdog.json.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)

    def test_malformed_project_row_quarantine(self):
        """A single malformed project row is quarantined without degrading the whole watchdog."""
        data = {
            "version": WATCHDOG_SCHEMA_VERSION,
            "degraded": False,
            "projects": {
                "p_valid": {
                    "attempts": {},
                    "last_checked_at": None,
                },
                "p_bad": {
                    "attempts": "not-a-dict",  # malformed!
                },
            },
        }
        self.state_file.write_text(json.dumps(data), encoding="utf-8")
        coordinator = WatchdogCoordinator(self.runtime_dir)

        st = coordinator.state()
        self.assertFalse(st["degraded"])
        self.assertIn("p_valid", st["projects"])
        self.assertNotIn("p_bad", st["projects"])
        self.assertIn("p_bad", st["quarantined_projects"])

    def test_recover_interrupted_attempts_on_restart(self):
        """In-flight attempts are marked interrupted upon coordinator restart."""
        data = {
            "version": WATCHDOG_SCHEMA_VERSION,
            "degraded": False,
            "projects": {
                "p1": {
                    "attempts": {
                        "att-1": {
                            "attempt_key": "att-1",
                            "state": "running",
                            "started_at": "2026-09-12T10:00:00Z",
                        },
                        "att-2": {
                            "attempt_key": "att-2",
                            "state": "completed",
                            "diagnosis": "agent_stalled",
                        },
                    }
                }
            },
        }
        self.state_file.write_text(json.dumps(data), encoding="utf-8")
        coordinator = WatchdogCoordinator(self.runtime_dir)

        st = coordinator.state()
        p1_attempts = st["projects"]["p1"]["attempts"]
        self.assertEqual(p1_attempts["att-1"]["state"], "interrupted")
        self.assertIn("restarted", p1_attempts["att-1"]["reason"])
        self.assertIsNotNone(p1_attempts["att-1"]["completed_at"])
        # Completed attempt remains untouched
        self.assertEqual(p1_attempts["att-2"]["state"], "completed")

    def test_save_state_prunes_terminal_attempts_but_keeps_nonterminal(self):
        coordinator = WatchdogCoordinator(self.runtime_dir)
        prow = {
            "attempts": {},
            "attempt_counts": {},
            "recovery_slots": {},
            "stall": {"run_scope_key": "rscope-running"},
        }
        for index in range(MAX_TERMINAL_ATTEMPTS_PER_PROJECT + 5):
            key = f"att-term-{index:02d}"
            prow["attempts"][key] = {
                "attempt_key": key,
                "run_scope_key": f"rscope-{index:02d}",
                "state": "completed",
                "completed_at": f"2026-09-12T10:{index:02d}:00Z",
            }
            prow["attempt_counts"][f"rscope-{index:02d}"] = index
            prow["recovery_slots"][f"rscope-{index:02d}"] = f"wd-{index:02d}"
        prow["attempts"]["att-running"] = {
            "attempt_key": "att-running",
            "run_scope_key": "rscope-running",
            "state": "running",
            "started_at": "2026-09-12T11:00:00Z",
        }
        prow["attempt_counts"]["rscope-running"] = 99
        prow["recovery_slots"]["rscope-running"] = "wd-running"
        coordinator._cached_state["projects"]["p1"] = prow

        coordinator._save_state(coordinator._cached_state)

        saved = coordinator.state()["projects"]["p1"]
        self.assertIn("att-running", saved["attempts"])
        self.assertEqual(len(saved["attempts"]), MAX_TERMINAL_ATTEMPTS_PER_PROJECT + 1)
        self.assertIn("att-term-24", saved["attempts"])
        self.assertNotIn("att-term-00", saved["attempts"])
        self.assertIn("rscope-running", saved["attempt_counts"])
        self.assertNotIn("rscope-00", saved["attempt_counts"])

    # -----------------------------------------------------------------------
    # P10-FR-3 regressions: clear_degraded quarantine identity verification
    # -----------------------------------------------------------------------

    def test_fr3_clear_degraded_blocked_no_quarantine_file(self):
        """FR-3: clear_degraded must fail when no quarantine file matching corrupt_identity exists."""
        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        self.assertTrue(coordinator.state()["degraded"])

        # Remove the quarantine file that was just created
        for f in self.runtime_dir.glob("watchdog.json.corrupt-*"):
            f.unlink()

        channel.events.clear()
        coordinator.clear_degraded()

        # Still degraded — no quarantine file
        self.assertTrue(coordinator.state()["degraded"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "no_matching_quarantine_artifact")

    def test_fr3_clear_degraded_blocked_wrong_hash_quarantine_file(self):
        """FR-3: clear_degraded must fail when only an unrelated quarantine file exists."""
        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        self.assertTrue(coordinator.state()["degraded"])

        # Replace the correct quarantine file with one whose name contains a wrong hash
        for f in self.runtime_dir.glob("watchdog.json.corrupt-*"):
            f.unlink()
        (self.runtime_dir / "watchdog.json.corrupt-2026-01-01-wronghash0000").write_bytes(b"irrelevant")

        channel.events.clear()
        coordinator.clear_degraded()

        self.assertTrue(coordinator.state()["degraded"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "no_matching_quarantine_artifact")

    def test_fr3_clear_degraded_blocked_empty_quarantine_file(self):
        """FR-3: clear_degraded must fail when the matching quarantine file is empty (0 bytes)."""
        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        self.assertTrue(coordinator.state()["degraded"])

        corrupt_identity = coordinator._cached_state.get("corrupt_identity")
        self.assertIsNotNone(corrupt_identity, "corrupt_identity must be set in degraded state")

        # Overwrite the quarantine file with 0 bytes
        for f in self.runtime_dir.glob("watchdog.json.corrupt-*"):
            f.write_bytes(b"")

        channel.events.clear()
        coordinator.clear_degraded()

        self.assertTrue(coordinator.state()["degraded"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "quarantine_artifact_unreadable")

    def test_fr3_clear_degraded_blocked_unreadable_quarantine_file(self):
        """FR-3: clear_degraded must fail when reading the matching quarantine file raises IOError."""
        from unittest.mock import patch

        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        self.assertTrue(coordinator.state()["degraded"])

        corrupt_identity = coordinator._cached_state.get("corrupt_identity")
        self.assertIsNotNone(corrupt_identity)

        # Patch Path.read_bytes so that reading the quarantine file raises OSError.
        original_read_bytes = Path.read_bytes

        def ioerror_for_corrupt_files(path_self: Path):
            if "watchdog.json.corrupt-" in path_self.name:
                raise OSError("simulated IOError on quarantine read")
            return original_read_bytes(path_self)

        channel.events.clear()
        with patch.object(Path, "read_bytes", ioerror_for_corrupt_files):
            coordinator.clear_degraded()

        self.assertTrue(coordinator.state()["degraded"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "quarantine_artifact_unreadable")

    def test_fr3_corrupt_identity_stored_in_degraded_state(self):
        """FR-3: corrupt_identity is stored in _cached_state when degraded, and matches
        the hash in the quarantine filename."""
        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        coordinator = WatchdogCoordinator(self.runtime_dir)

        corrupt_identity = coordinator._cached_state.get("corrupt_identity")
        self.assertIsNotNone(corrupt_identity, "corrupt_identity must be set when degraded")
        self.assertEqual(len(corrupt_identity), 16, "corrupt_identity must be a 16-char hash")

        corrupt_files = list(self.runtime_dir.glob("watchdog.json.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)
        self.assertIn(corrupt_identity, corrupt_files[0].name)

    # -----------------------------------------------------------------------
    # P10-FR-3 round-5: additional cryptographic binding tests
    # -----------------------------------------------------------------------

    def test_fr3_clear_degraded_blocked_when_corrupt_identity_absent(self):
        """FR-3: clear_degraded must fail closed when corrupt_identity is absent/None.
        Without a stored identity there is no way to verify the artifact."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        # Manually inject degraded state without a corrupt_identity
        coordinator._cached_state["degraded"] = True
        coordinator._cached_state["degraded_reason"] = "injected"
        coordinator._cached_state["corrupt_identity"] = None

        coordinator.clear_degraded()

        self.assertTrue(coordinator.state()["degraded"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "corrupt_identity_absent")

    def test_fr3_clear_degraded_blocked_content_hash_mismatch(self):
        """FR-3: clear_degraded must fail when a quarantine file has the correct name segment
        but its content SHA-256 does not match the stored corrupt_identity (wrong-content attack)."""
        import hashlib
        self.state_file.write_bytes(b"NOT VALID JSON {[[")
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        self.assertTrue(coordinator.state()["degraded"])

        corrupt_identity = coordinator._cached_state.get("corrupt_identity")
        self.assertIsNotNone(corrupt_identity)

        # Replace the correct quarantine file content with different bytes while keeping
        # the filename (which embeds the original hash) — simulates content tampering.
        for f in self.runtime_dir.glob("watchdog.json.corrupt-*"):
            f.write_bytes(b"TAMPERED CONTENT")
        # Sanity check: tampered content has a different SHA-256
        tampered_hash = hashlib.sha256(b"TAMPERED CONTENT").hexdigest()[:16]
        self.assertNotEqual(tampered_hash, corrupt_identity)

        channel.events.clear()
        coordinator.clear_degraded()

        self.assertTrue(coordinator.state()["degraded"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "quarantine_artifact_content_mismatch")

    def test_fr3_clear_degraded_succeeds_with_correct_content(self):
        """FR-3: clear_degraded must succeed when the quarantine file content SHA-256 matches
        the stored corrupt_identity (correct, unmodified artifact)."""
        import hashlib
        raw = b"NOT VALID JSON {[["
        expected_hash = hashlib.sha256(raw).hexdigest()[:16]

        self.state_file.write_bytes(raw)
        coordinator = WatchdogCoordinator(self.runtime_dir)
        self.assertTrue(coordinator.state()["degraded"])
        self.assertEqual(coordinator._cached_state["corrupt_identity"], expected_hash)

        coordinator.clear_degraded()

        self.assertFalse(coordinator.state()["degraded"])

    def test_fr3_copy_semantics_identity_derived_from_copied_artifact_bytes(self):
        """FR-3: when raw bytes are unavailable (OSError during read), the quarantine copy
        is made via shutil.copy2 and corrupt_identity is derived from the copied bytes.
        A subsequent clear_degraded should succeed when the artifact content matches."""
        import hashlib
        raw = b'{"version": 999, "projects": {}}'  # will trigger unsupported version
        self.state_file.write_bytes(raw)

        original_read_bytes = Path.read_bytes

        def failing_read_bytes(path_self: Path):
            if path_self == self.state_file:
                raise OSError("permission denied")
            return original_read_bytes(path_self)

        with patch("pathlib.Path.read_bytes", new=failing_read_bytes):
            coordinator = WatchdogCoordinator(self.runtime_dir)

        self.assertTrue(coordinator.state()["degraded"])

        # corrupt_identity must be the SHA-256[:16] of the raw file content (from copied bytes)
        corrupt_identity = coordinator._cached_state.get("corrupt_identity")
        self.assertIsNotNone(corrupt_identity, "corrupt_identity must be set even for OSError copy path")
        expected_identity = hashlib.sha256(raw).hexdigest()[:16]
        self.assertEqual(corrupt_identity, expected_identity,
                         "corrupt_identity must be derived from copied artifact bytes, not from reason string")

        # The quarantine filename must embed the content-derived hash
        corrupt_files = list(self.runtime_dir.glob("watchdog.json.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)
        self.assertTrue(corrupt_files[0].name.endswith(f"-{corrupt_identity}"),
                        f"Quarantine filename must end with '-{corrupt_identity}'")

        # clear_degraded must now succeed (correct artifact, matching identity)
        coordinator.clear_degraded()
        self.assertFalse(coordinator.state()["degraded"],
                         "clear_degraded must succeed when copied artifact content matches identity")


if __name__ == "__main__":
    unittest.main()
