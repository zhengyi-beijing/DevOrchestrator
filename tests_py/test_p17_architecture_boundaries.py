"""P17 Architecture Boundaries and import isolation tests.

Proves:
1. No production module in src/dev_orchestrator/ (except convergence itself) imports from convergence.
2. src/dev_orchestrator/cli.py does NOT register any convergence subcommands.
3. Convergence modules do NOT import production write paths (daemon, transition-executor write paths, or control mutators).
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


class TestP17ArchitectureBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        self.src_root = Path("src/dev_orchestrator")
        self.conv_root = self.src_root / "convergence"

    def test_no_production_module_imports_convergence(self) -> None:
        """Scan all .py files in src/dev_orchestrator outside convergence/."""
        offending_imports = []
        for p in self.src_root.rglob("*.py"):
            # Skip convergence itself
            if str(p).startswith(str(self.conv_root)):
                continue

            try:
                tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "convergence" in alias.name:
                            offending_imports.append((str(p), alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.module and "convergence" in node.module:
                        offending_imports.append((str(p), node.module))

        self.assertEqual(
            offending_imports,
            [],
            f"Production modules imported convergence package: {offending_imports}",
        )

    def test_production_cli_has_no_convergence_registration(self) -> None:
        """Verify cli.py has no convergence commands registered."""
        cli_content = (self.src_root / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("dev_orchestrator.convergence", cli_content)
        self.assertNotIn("p17-shadow", cli_content)
        self.assertNotIn("p17_shadow", cli_content)

    def test_convergence_modules_do_not_import_production_write_paths(self) -> None:
        """Scan all .py files in convergence/ to ensure no imports of daemon or mutators."""
        prohibited_modules = {
            "dev_orchestrator.daemon",
            "dev_orchestrator.core.transition_executor",
            "dev_orchestrator.control.coordinator",
            "dev_orchestrator.core.lifecycle_authority",
        }
        offenders = []
        for p in self.conv_root.glob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in prohibited_modules:
                            offenders.append((p.name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.module in prohibited_modules:
                        offenders.append((p.name, node.module))

        self.assertEqual(
            offenders,
            [],
            f"Convergence modules imported prohibited production modules: {offenders}",
        )
