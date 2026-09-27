"""P17 closure defect: git metadata paths must resolve from a linked worktree.

The self-hosted controller runs from a linked worktree where ``.git`` is a
*file* (``gitdir: <common>/.git/worktrees/<name>``), not a directory.  Any code
that joins onto ``repo / ".git"`` either builds a path underneath a regular file
- which raises ``FileExistsError`` (WinError 183) on ``mkdir(parents=True)`` -
or silently reads the wrong location.  These tests pin the authoritative
resolution and its fail-closed behaviour.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.core.git_paths import resolve_git_dir, resolve_git_path
from dev_orchestrator.incidents.candidate import generate_candidate, materialize_candidate
from dev_orchestrator.incidents.regression_owner import resolve_regression_owner


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _owner_primary(repo: Path) -> Path:
    """A committed repository shaped like the DevOrchestrator regression owner."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@test.com")
    (repo / "pyproject.toml").write_text('[project]\nname = "dev-orchestrator"\n', encoding="utf-8")
    src_pkg = repo / "src" / "dev_orchestrator"
    src_pkg.mkdir(parents=True, exist_ok=True)
    (src_pkg / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests_py").mkdir(parents=True, exist_ok=True)
    (repo / "tests_py" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _linked_worktree(root: Path) -> Path:
    """An owner-shaped linked worktree, as the self-hosted controller runs from."""
    primary = _owner_primary(root / "primary")
    worktree = root / "worktree"
    subprocess.run(
        ["git", "-C", str(primary), "-c", "core.autocrlf=false",
         "worktree", "add", "-b", "wt-main", str(worktree)],
        check=True, capture_output=True, text=True,
    )
    return worktree


class GitPathResolutionTests(unittest.TestCase):
    def test_linked_worktree_dot_git_is_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = _linked_worktree(Path(td))
            self.assertTrue((worktree / ".git").is_file(), "fixture is not a linked worktree")

    def test_info_exclude_resolves_into_the_common_git_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = _linked_worktree(root)
            exclude = resolve_git_path(worktree, "info/exclude")
            self.assertIsNotNone(exclude)
            common = Path(_git(worktree, "rev-parse", "--git-common-dir")).resolve()
            self.assertEqual(exclude.resolve(), (common / "info" / "exclude").resolve())
            # The shared exclude lives under the primary checkout, not the worktree.
            self.assertEqual(common, (root / "primary" / ".git").resolve())
            # Writing it must not trip over ``.git`` being a regular file.
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write("\nprobe/\n")
            self.assertIn("probe/", exclude.read_text(encoding="utf-8"))

    def test_git_dir_resolves_to_the_per_worktree_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = _linked_worktree(root)
            git_dir = resolve_git_dir(worktree)
            self.assertIsNotNone(git_dir)
            self.assertTrue(git_dir.is_dir())
            self.assertEqual(git_dir.name, "worktree")
            self.assertEqual(git_dir.parent.name, "worktrees")

    def test_primary_checkout_still_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            primary = _owner_primary(Path(td) / "primary")
            self.assertTrue((primary / ".git").is_dir())
            self.assertEqual(
                resolve_git_dir(primary).resolve(), (primary / ".git").resolve()
            )
            self.assertEqual(
                resolve_git_path(primary, "info/exclude").resolve(),
                (primary / ".git" / "info" / "exclude").resolve(),
            )

    def test_non_repository_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "plain"
            plain.mkdir()
            self.assertIsNone(resolve_git_dir(plain))
            self.assertIsNone(resolve_git_path(plain, "info/exclude"))

    def test_bogus_dot_git_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake"
            fake.mkdir()
            (fake / ".git").write_text("gitdir: nowhere-at-all\n", encoding="utf-8")
            self.assertIsNone(resolve_git_dir(fake))


class LinkedWorktreeRegressionOwnerTests(unittest.TestCase):
    def _config(self, root: Path, repo_path: Path) -> Path:
        config_path = root / "projects.json"
        config_path.write_text(json.dumps({
            "projects": [
                {
                    "project_id": "dev_orchestrator",
                    "repo_path": str(repo_path),
                    "regression_owner": True,
                }
            ]
        }), encoding="utf-8")
        return config_path

    def test_owner_verifies_when_repo_is_a_linked_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = _linked_worktree(root)
            owner = resolve_regression_owner(
                json.loads(self._config(root, worktree).read_text(encoding="utf-8"))
            )
            self.assertTrue(owner["verified"], owner["reason"])
            self.assertEqual(owner["repo_path"].resolve(), worktree.resolve())

    def test_owner_with_unresolvable_git_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "fake"
            (fake / "src" / "dev_orchestrator").mkdir(parents=True)
            (fake / "src" / "dev_orchestrator" / "__init__.py").write_text("", encoding="utf-8")
            (fake / "pyproject.toml").write_text('name = "dev-orchestrator"\n', encoding="utf-8")
            (fake / "tests_py").mkdir()
            # A ``.git`` file that git cannot follow must not pass verification.
            (fake / ".git").write_text("gitdir: nowhere-at-all\n", encoding="utf-8")
            owner = resolve_regression_owner(
                json.loads(self._config(root, fake).read_text(encoding="utf-8"))
            )
            self.assertFalse(owner["verified"])
            self.assertIn("git cannot resolve", owner["reason"])

    def test_materialize_candidate_from_linked_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = _linked_worktree(root)
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            config_path = self._config(runtime_root, worktree)

            generate_candidate(runtime_root, candidate_id="cand-wt", generated_by="alice")
            res = materialize_candidate(runtime_root, "cand-wt", config_path=config_path)
            self.assertTrue(res["materialized"], res.get("reason"))

            staging = worktree / "tests_candidate"
            self.assertTrue((staging / "conftest.py").is_file())
            self.assertTrue((staging / "test_candidate_cand-wt.py").is_file())

            # The exclude entry lands in the common git dir and actually hides the
            # staging directory from this worktree's status.
            exclude = Path(_git(worktree, "rev-parse", "--git-common-dir")) / "info" / "exclude"
            self.assertIn("tests_candidate", exclude.read_text(encoding="utf-8"))
            self.assertEqual(_git(worktree, "status", "--porcelain"), "")

    def test_materialize_candidate_fails_closed_when_git_path_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = _linked_worktree(root)
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            config_path = self._config(runtime_root, worktree)

            generate_candidate(runtime_root, candidate_id="cand-closed", generated_by="alice")
            with patch(
                "dev_orchestrator.incidents.candidate.resolve_git_path", return_value=None
            ):
                res = materialize_candidate(runtime_root, "cand-closed", config_path=config_path)
            self.assertFalse(res["materialized"])
            self.assertIn("failed_to_resolve_git_exclude_path", res["reason"])
            self.assertFalse((worktree / "tests_candidate").exists())


if __name__ == "__main__":
    unittest.main()
