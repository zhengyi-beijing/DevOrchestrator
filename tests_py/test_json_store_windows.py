import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_orchestrator.storage.json_store as json_store


@unittest.skipUnless(os.name == "nt", "Windows-specific atomic replace tests")
class JsonStoreWindowsTests(unittest.TestCase):
    @staticmethod
    def sharing_error():
        exc = PermissionError(13, "sharing violation")
        exc.winerror = 5
        return exc

    def test_write_json_replaces_existing_target(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            target.write_text('{"old":1}', encoding="utf-8")
            json_store.write_json(target, {"new": 2}, indent=2)
            self.assertEqual(json_store.read_json(target), {"new": 2})

    def test_atomic_replace_uses_replacefile_fallback_on_sharing_violation(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source.json"
            target = Path(td) / "target.json"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(json_store.os, "replace", side_effect=self.sharing_error()) as replace_mock, mock.patch.object(json_store, "_windows_replace_file", return_value=True) as win_mock:
                json_store._atomic_replace(source, target)
            replace_mock.assert_called_once()
            win_mock.assert_called_once_with(source, target)

    def test_atomic_replace_bounds_persistent_sharing_violation(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source.json"
            target = Path(td) / "target.json"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(json_store.os, "replace", side_effect=self.sharing_error()) as replace_mock, mock.patch.object(json_store, "_windows_replace_file", return_value=False), mock.patch.object(json_store.time, "sleep") as sleep_mock:
                with self.assertRaises(PermissionError):
                    json_store._atomic_replace(source, target)
            self.assertEqual(replace_mock.call_count, 5)
            self.assertEqual(sleep_mock.call_count, 4)


if __name__ == "__main__":
    unittest.main()
