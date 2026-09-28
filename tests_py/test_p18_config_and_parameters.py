"""Tests for P18 config execution policies, parameter validation, and digest derivation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
    compute_execution_policy_digest,
    compute_parameters_digest,
    compute_resolution_digest,
    resolve_execution_policy,
)


class TestP18ConfigAndParameters(unittest.TestCase):
    """Verifies parameter schemas, safe substitution, traversal rejection, and digest stability."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "main.py").write_text("print('hello')", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parameter_schema_validation_enum(self):
        """Enum string parameters enforce allowed_values constraints."""
        cmd = JobCommandConfig(
            argv=["python", "-m", "worker", "--mode", "{mode}"],
            cwd=".",
            effect_class="read_only",
            parameters={
                "mode": {
                    "type": "enum",
                    "allowed_values": ["quick", "full"],
                },
            },
        )
        proj = JobProjectConfig(repo_path=self.repo_dir, commands={"run": cmd})
        cfg = JobsConfig(runtime_root=self.root, projects={"p1": proj})

        # Valid invocation
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "run", parameters={"mode": "quick"})
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.resolved_argv, ["python", "-m", "worker", "--mode", "quick"])

        # Invalid enum value
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "run", parameters={"mode": "invalid_mode"})
        self.assertFalse(ok)
        self.assertIn("not in allowed enum values", str(err))

    def test_parameter_schema_validation_integer_bounds(self):
        """Integer parameters validate min and max bounds."""
        cmd = JobCommandConfig(
            argv=["pytest", "-n", "{concurrency}"],
            cwd=".",
            parameters={
                "concurrency": {
                    "type": "integer",
                    "min": 1,
                    "max": 16,
                }
            },
        )
        proj = JobProjectConfig(repo_path=self.repo_dir, commands={"test": cmd})
        cfg = JobsConfig(runtime_root=self.root, projects={"p1": proj})

        # Out of bounds (below min)
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "test", parameters={"concurrency": 0})
        self.assertFalse(ok)
        self.assertIn("below minimum", str(err))

        # Out of bounds (above max)
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "test", parameters={"concurrency": 32})
        self.assertFalse(ok)
        self.assertIn("exceeds maximum", str(err))

        # Non-integer value
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "test", parameters={"concurrency": "fast"})
        self.assertFalse(ok)
        self.assertIn("must be an integer", str(err))

        # Valid bounds
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "test", parameters={"concurrency": 4})
        self.assertTrue(ok)
        self.assertEqual(resolved.resolved_argv, ["pytest", "-n", "4"])

    def test_parameter_schema_validation_path_containment_and_traversal(self):
        """Path parameters reject directory traversal (..) and uncontained paths."""
        cmd = JobCommandConfig(
            argv=["python", "-m", "lint", "{target_file}"],
            cwd=".",
            parameters={
                "target_file": {
                    "type": "path_within_repo",
                }
            },
        )
        proj = JobProjectConfig(repo_path=self.repo_dir, commands={"lint": cmd})
        cfg = JobsConfig(runtime_root=self.root, projects={"p1": proj})

        # Rejection of explicit .. traversal
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "lint", parameters={"target_file": "../../../etc/passwd"})
        self.assertFalse(ok)
        self.assertIn("contains path traversal '..'", str(err))

        # Rejection of absolute path
        abs_target = str(self.repo_dir / "src" / "main.py")
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "lint", parameters={"target_file": abs_target})
        self.assertFalse(ok)
        self.assertIn("must be a relative path", str(err))

        # Valid path within repo
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "lint", parameters={"target_file": "src/main.py"})
        self.assertTrue(ok)
        self.assertEqual(resolved.resolved_argv, ["python", "-m", "lint", "src/main.py"])

    def test_parameter_unknown_and_missing_rejections(self):
        """Unknown parameters are rejected and missing required parameters raise errors."""
        cmd = JobCommandConfig(
            argv=["run", "{required_arg}"],
            cwd=".",
            parameters={
                "required_arg": {"type": "enum", "allowed_values": ["val1", "val2"]},
            },
        )
        proj = JobProjectConfig(repo_path=self.repo_dir, commands={"run": cmd})
        cfg = JobsConfig(runtime_root=self.root, projects={"p1": proj})

        # Missing required parameter
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "run", parameters={})
        self.assertFalse(ok)
        self.assertIn("missing required parameters", str(err))

        # Unknown parameter passed
        ok, err, resolved = resolve_execution_policy(cfg, "p1", "run", parameters={"required_arg": "val1", "evil_injected": "123"})
        self.assertFalse(ok)
        self.assertIn("unknown parameters", str(err))

    def test_shell_injection_characters_rejected(self):
        """Parameters containing shell metacharacters (&, |, ;, $, `, <, >) fail closed."""
        cmd = JobCommandConfig(
            argv=["echo", "{param}"],
            cwd=".",
            parameters={"param": {"type": "enum", "allowed_values": ["; rm -rf /"]}},
        )
        proj = JobProjectConfig(repo_path=self.repo_dir, commands={"echo": cmd})
        cfg = JobsConfig(runtime_root=self.root, projects={"p1": proj})

        ok, err, resolved = resolve_execution_policy(cfg, "p1", "echo", parameters={"param": "; rm -rf /"})
        self.assertFalse(ok)
        self.assertIn("contains prohibited characters", str(err))

    def test_digests_and_dual_attribute_item_access(self):
        """ResolvedExecutionPolicy computes deterministic digests and supports both attribute and item access."""
        cmd = JobCommandConfig(
            argv=["python", "-c", "print(1)"],
            cwd=".",
            effect_class="read_only",
            duration_class="short",
        )
        proj = JobProjectConfig(repo_path=self.repo_dir, commands={"test": cmd})
        cfg = JobsConfig(runtime_root=self.root, projects={"p1": proj})

        ok, err, resolved = resolve_execution_policy(cfg, "p1", "test", parameters=None)
        self.assertTrue(ok)
        self.assertIsNotNone(resolved)

        # Attribute access
        self.assertEqual(resolved.effect_class, "read_only")
        self.assertTrue(resolved.execution_policy_digest.startswith("sha256:"))
        self.assertTrue(resolved.resolution_digest.startswith("sha256:"))
        self.assertIsNone(resolved.parameters_digest)

        # Dual item access (for compatibility with legacy validate_and_resolve_execution callers)
        self.assertEqual(resolved["effect_class"], "read_only")
        self.assertEqual(resolved["resolved_argv"], ["python", "-c", "print(1)"])
        self.assertEqual(str(resolved["resolved_cwd"]).lower(), str(self.repo_dir).lower())

        # Verify stability of compute_parameters_digest
        p_dig_1 = compute_parameters_digest({"b": 2, "a": "hello"})
        p_dig_2 = compute_parameters_digest({"a": "hello", "b": 2})
        self.assertEqual(p_dig_1, p_dig_2)
        self.assertTrue(p_dig_1.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
