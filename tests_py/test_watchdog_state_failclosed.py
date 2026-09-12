import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.watchdog import (
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

        # Emitted OWNER_GATE notification
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][0], "__controller__")
        self.assertEqual(gate_events[0][2]["details"]["gate"], "corrupt-state")

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


if __name__ == "__main__":
    unittest.main()
