import os
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.watchdog import (
    canonical_path,
    is_watchdog_owned_path,
    path_contains,
)


class WatchdogOwnedPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir(parents=True)
        # Inside runtime (self-hosted layout)
        self.internal_runtime = self.repo_dir / ".devorch"
        self.internal_runtime.mkdir(parents=True)
        # External runtime layout
        self.external_runtime = self.root / "external_runtime"
        self.external_runtime.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_canonical_path_properties(self):
        """canonical_path applies normcase, realpath, and abspath."""
        # Redundant components and relative resolution
        p1 = self.repo_dir / "foo" / ".." / "bar" / "."
        p2 = self.repo_dir / "bar"
        self.assertEqual(canonical_path(p1), canonical_path(p2))

        # Trailing separators
        p3 = str(self.repo_dir) + os.sep
        p4 = str(self.repo_dir)
        self.assertEqual(canonical_path(p3), canonical_path(p4))

        # Case normalization (on Windows, normcase lowercases)
        c = canonical_path(self.repo_dir)
        self.assertEqual(c, c.lower() if os.name == "nt" else c)

    def test_path_contains_hierarchy(self):
        """path_contains uses commonpath to strictly verify subpath hierarchy."""
        # Exact match
        self.assertTrue(path_contains(self.repo_dir, self.repo_dir))

        # Child and deeply nested child
        child = self.repo_dir / "agent" / "next.md"
        self.assertTrue(path_contains(self.repo_dir, child))

        # Parent directory is not contained
        self.assertFalse(path_contains(child, self.repo_dir))

        # Sibling directory with shared prefix is NOT contained
        sibling = self.root / "repo_sibling"
        sibling.mkdir()
        self.assertFalse(path_contains(self.repo_dir, sibling))

        # Cross-drive or completely disjoint path returns False without crashing
        self.assertFalse(path_contains("C:\\some\\drive\\root", "D:\\other\\drive\\root"))

    def test_is_watchdog_owned_path_repo_patterns(self):
        """Verify repo-owned status mirrors and watchdog files under .devorch are recognized."""
        # Status mirror
        status_file = self.repo_dir / ".devorch" / "status.json"
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, status_file))
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, ".devorch/status.json"))

        # Repo-owned watchdog files under .devorch/
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, self.repo_dir / ".devorch" / "watchdog.json"))
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, self.repo_dir / ".devorch" / "watchdog.json.corrupt-12345"))

        # Regular worker files are NOT watchdog-owned
        self.assertFalse(is_watchdog_owned_path(self.repo_dir, self.repo_dir / "agent" / "CURRENT.md"))
        self.assertFalse(is_watchdog_owned_path(self.repo_dir, self.repo_dir / "agent" / "next.md"))
        self.assertFalse(is_watchdog_owned_path(self.repo_dir, self.repo_dir / "src" / "main.py"))
        self.assertFalse(
            is_watchdog_owned_path(
                self.repo_dir,
                self.repo_dir / ".devorch" / "watchdog" / "nested.json",
            )
        )

    def test_is_watchdog_owned_path_internal_runtime_patterns(self):
        """Verify runtime-owned watchdog files are recognized when runtime is inside repo."""
        # Watchdog-generated control command files in inbox and history
        wd_inbox = self.internal_runtime / "control" / "inbox" / "wd-att12345.json"
        wd_history = self.internal_runtime / "control" / "history" / "wd-att12345.json"
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, wd_inbox, runtime_root=self.internal_runtime))
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, wd_history, runtime_root=self.internal_runtime))

        # Non-watchdog user commands in inbox/history are NOT watchdog-owned
        user_inbox = self.internal_runtime / "control" / "inbox" / "cmd-user999.json"
        self.assertFalse(is_watchdog_owned_path(self.repo_dir, user_inbox, runtime_root=self.internal_runtime))

        # Runtime state files
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, self.internal_runtime / "watchdog.json", runtime_root=self.internal_runtime))
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, self.internal_runtime / "watchdog.json.corrupt-abc", runtime_root=self.internal_runtime))
        self.assertFalse(
            is_watchdog_owned_path(
                self.repo_dir,
                self.internal_runtime / "control" / "history" / "wd-att12345" / "nested.json",
                runtime_root=self.internal_runtime,
            )
        )

    def test_external_runtime_isolation(self):
        """When runtime_root is outside repo_root, runtime-owned rules remain inert."""
        wd_inbox = self.external_runtime / "control" / "inbox" / "wd-att12345.json"
        # Inert because external_runtime is not within repo_dir
        self.assertFalse(is_watchdog_owned_path(self.repo_dir, wd_inbox, runtime_root=self.external_runtime))

    def test_candidate_outside_roots_is_not_owned(self):
        """Candidates completely outside repo and runtime roots are rejected."""
        outside = self.root / "outside" / "something.json"
        self.assertFalse(is_watchdog_owned_path(self.repo_dir, outside, runtime_root=self.internal_runtime))


if __name__ == "__main__":
    unittest.main()
