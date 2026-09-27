"""Tests for canonical task status parser, editing helpers, and static migration integrity."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from dev_orchestrator.core.task_status import (
    TASK_STATUS_SCHEMA_VERSION,
    TaskStatus,
    TaskStatusError,
    find_status_lines,
    parse_task_status,
    render_status_line,
    require_single_status_line,
)


class TestP168TaskStatus(unittest.TestCase):
    """Test suite for canonical task status grammar and parser."""

    def test_schema_version(self):
        self.assertEqual(TASK_STATUS_SCHEMA_VERSION, 1)

    def test_parse_pending_design_variations(self):
        tokens = [
            "PENDING DESIGN",
            "PENDING_DESIGN",
            "pending design",
            "**PENDING DESIGN**",
            "[PENDING DESIGN]",
            "`PENDING_DESIGN`",
            "**[PENDING DESIGN]**",
            "PENDING DESIGN \u2014 waiting for initial plan review",
            "PENDING_DESIGN - awaiting review",
            "PENDING DESIGN \u2013 owner goal authorized",
        ]
        for token in tokens:
            status = parse_task_status(token)
            self.assertTrue(status.valid, f"Failed for {token}")
            self.assertEqual(status.canonical, "pending_design")
            self.assertTrue(status.is_pending_design())
            self.assertFalse(status.is_ready_to_run())
            self.assertFalse(status.is_completed())

    def test_parse_staged_not_started_as_pending_design(self):
        status = parse_task_status("STAGED / NOT STARTED")
        self.assertTrue(status.valid)
        self.assertTrue(status.is_pending_design())
        self.assertFalse(parse_task_status("STAGED").valid)
        self.assertFalse(parse_task_status("STAGED / COMPLETE").valid)

    def test_parse_ready_to_run_variations(self):
        tokens = [
            "READY_TO_RUN",
            "READY-TO-RUN",
            "READY TO RUN",
            "DESIGN READY",
            "DESIGN_READY",
            "EXECUTABLE",
            "**READY_TO_RUN**",
            "**READY-TO-RUN**",
            "[READY_TO_RUN]",
            "`READY-TO-RUN`",
            "READY-TO-RUN \u2014 verified by plan review",
            "READY_TO_RUN - approved executable design",
        ]
        for token in tokens:
            status = parse_task_status(token)
            self.assertTrue(status.valid, f"Failed for {token}")
            self.assertEqual(status.canonical, "ready_to_run")
            self.assertTrue(status.is_ready_to_run())
            self.assertFalse(status.is_pending_design())
            self.assertFalse(status.is_completed())

    def test_hyphenated_token_preservation(self):
        # READY-TO-RUN must not be split by the annotation delimiter
        status = parse_task_status("READY-TO-RUN \u2014 plan approved")
        self.assertTrue(status.is_ready_to_run())
        self.assertEqual(status.annotation, "plan approved")

        status2 = parse_task_status("Status: **READY-TO-RUN** - plan approved")
        self.assertTrue(status2.is_ready_to_run())
        self.assertEqual(status2.annotation, "plan approved")

    def test_parse_completed_done_variations(self):
        tokens = [
            "DONE",
            "COMPLETED",
            "COMPLETE",
            "ACCEPTED",
            "**DONE**",
            "[DONE]",
            "`DONE`",
            "**COMPLETED** \u2014 all tests passing",
            "DONE - technical review accepted",
        ]
        for token in tokens:
            status = parse_task_status(token)
            self.assertTrue(status.valid, f"Failed for {token}")
            self.assertEqual(status.canonical, "completed")
            self.assertTrue(status.is_completed())
            self.assertFalse(status.is_ready_to_run())

    def test_parse_blocked_variations(self):
        tokens = [
            "BLOCKED",
            "**BLOCKED**",
            "[BLOCKED]",
            "BLOCKED \u2014 waiting on external dependency",
        ]
        for token in tokens:
            status = parse_task_status(token)
            self.assertTrue(status.valid, f"Failed for {token}")
            self.assertEqual(status.canonical, "blocked")
            self.assertTrue(status.is_blocked())

    def test_find_status_lines_and_require_single(self):
        doc_single = "# Task P1\n\nStatus: **PENDING DESIGN**\n\nDescription\n"
        matches = find_status_lines(doc_single)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], 2)
        self.assertEqual(matches[0][1], "**PENDING DESIGN**")

        idx, raw = require_single_status_line(doc_single)
        self.assertEqual(idx, 2)
        self.assertEqual(raw, "**PENDING DESIGN**")

        doc_none = "# Task P1\n\nNo status line here\n"
        self.assertEqual(find_status_lines(doc_none), [])
        with self.assertRaises(TaskStatusError):
            require_single_status_line(doc_none)

        doc_multi = "# Task P1\n\nStatus: PENDING\nStatus: READY\n"
        self.assertEqual(len(find_status_lines(doc_multi)), 2)
        with self.assertRaises(TaskStatusError):
            require_single_status_line(doc_multi)

    def test_render_status_line(self):
        self.assertEqual(render_status_line("ready_to_run"), "Status: **READY_TO_RUN**")
        self.assertEqual(render_status_line("pending_design"), "Status: **PENDING DESIGN**")
        self.assertEqual(render_status_line("completed"), "Status: **DONE**")
        self.assertEqual(render_status_line("blocked"), "Status: **BLOCKED**")

        # With annotation
        self.assertEqual(
            render_status_line("ready_to_run", annotation="approved by planner"),
            "Status: **READY_TO_RUN** \u2014 approved by planner",
        )

    def test_static_scan_no_unparsed_status_checks_in_production(self):
        """Verify production code does not use brittle raw substring checks on next_status."""
        src_dir = Path(__file__).resolve().parent.parent / "src" / "dev_orchestrator"
        forbidden_patterns = [
            re.compile(r'in\s+str\([^)]*next_status[^)]*\)\.upper\(\)'),
            re.compile(r'"PENDING DESIGN"\s+in\s+next_status'),
            re.compile(r'"READY_TO_RUN"\s+in\s+next_status'),
            re.compile(r'"DONE"\s+in\s+next_status'),
        ]

        violations = []
        for py_file in src_dir.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                match = pattern.search(text)
                if match:
                    violations.append(f"{py_file.name}: matches '{match.group(0)}'")

        self.assertEqual(violations, [], f"Brittle status checks detected in production code: {violations}")


if __name__ == "__main__":
    unittest.main()
