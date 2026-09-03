import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.monitor.project import git_changed_activity_utc


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class GitActivityRegressionTests(unittest.TestCase):
    def test_tracked_worktree_change_contributes_activity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git(root, "init")
            git(root, "config", "user.email", "test@example.invalid")
            git(root, "config", "user.name", "Test")
            target = root / "alpha.txt"
            target.write_text("one\n", encoding="utf-8")
            git(root, "add", "alpha.txt")
            git(root, "commit", "-m", "fixture")
            target.write_text("two\n", encoding="utf-8")
            activity = git_changed_activity_utc(root)
            self.assertIsNotNone(activity, "tracked porcelain path must not be truncated")


if __name__ == "__main__":
    unittest.main()
