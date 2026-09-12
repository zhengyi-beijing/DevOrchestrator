import json
import tempfile
import threading
import unittest
from pathlib import Path

from dev_orchestrator.core.watchdog import WatchdogCoordinator


class WatchdogConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.config_file = self.root / "projects.json"
        self.config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_concurrent_advance_skipped_nonblocking(self):
        """When another thread holds _advance_lock, advance skips immediately with concurrent-advance."""
        coordinator = WatchdogCoordinator(self.runtime_dir)

        # Acquire advance lock manually to simulate concurrent execution
        acquired = coordinator._advance_lock.acquire(blocking=False)
        self.assertTrue(acquired)

        try:
            # Another call to advance should not block, but return skipped immediately
            res = coordinator.advance(self.config_file, {"projects": []})
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0], {"skipped": "concurrent-advance"})
        finally:
            coordinator._advance_lock.release()

        # After releasing, advance works normally
        res_after = coordinator.advance(self.config_file, {"projects": []})
        self.assertEqual(res_after, [])


if __name__ == "__main__":
    unittest.main()
