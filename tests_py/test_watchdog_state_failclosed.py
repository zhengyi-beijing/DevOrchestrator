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


if __name__ == "__main__":
    unittest.main()
