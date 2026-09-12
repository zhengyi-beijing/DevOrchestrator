import unittest

from dev_orchestrator.core.watchdog import (
    FINGERPRINT_FIELDS,
    FINGERPRINT_FORBIDDEN,
    build_progress_fingerprint,
    resolve_attempt_key,
    resolve_run_key,
    resolve_run_scope_key,
)


class WatchdogFingerprintTests(unittest.TestCase):
    def test_clock_free_fingerprint_stability(self):
        """Fingerprint is byte-identical across calls and independent of time."""
        payload = {
            "git_head": "abcdef1234567890",
            "last_activity_at": "2026-09-12T10:00:00Z",
            "newest_kind": "agent_file",
            "newest_path": "agent/task.md",
            "sources": {
                "agent_file": {
                    "last_activity_at": "2026-09-12T10:00:00Z",
                    "path": "agent/task.md",
                }
            },
            "changed_entries_considered": 2,
            "role_records": {},
            "progress_entry": None,
        }

        fp1 = build_progress_fingerprint(payload)
        fp2 = build_progress_fingerprint(payload)
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_forbidden_timing_and_provenance_fields_rejected(self):
        """All keys in FINGERPRINT_FORBIDDEN or matching timing patterns raise ValueError."""
        base_payload = {
            "git_head": "head123",
            "last_activity_at": "2026-09-12T10:00:00Z",
            "newest_kind": "agent_file",
            "newest_path": "agent/task.md",
            "sources": {},
            "changed_entries_considered": 0,
            "role_records": {},
            "progress_entry": None,
        }

        for forbidden in FINGERPRINT_FORBIDDEN:
            bad_payload = dict(base_payload)
            bad_payload[forbidden] = "value"
            with self.assertRaises(ValueError, msg=f"Should reject forbidden field: {forbidden}"):
                build_progress_fingerprint(bad_payload)

        # Also reject forbidden timing tokens inside nested sources
        timing_variants = [
            "age_seconds",
            "last_activity_age",
            "elapsed_ms",
            "uptime_sec",
            "now_utc",
            "monotonic_val",
        ]
        for var in timing_variants:
            bad_payload = dict(base_payload)
            bad_payload["sources"] = {var: {"last_activity_at": "ts", "path": "p"}}
            with self.assertRaises(ValueError, msg=f"Should reject nested timing variant: {var}"):
                build_progress_fingerprint(bad_payload)

    def test_unallowlisted_fields_rejected(self):
        """Keys outside FINGERPRINT_FIELDS must raise ValueError."""
        bad = {
            "git_head": "head123",
            "last_activity_at": "2026-09-12T10:00:00Z",
            "newest_kind": "agent_file",
            "newest_path": "agent/task.md",
            "sources": {},
            "changed_entries_considered": 0,
            "role_records": {},
            "progress_entry": None,
            "arbitrary_field": "disallowed",
        }
        with self.assertRaises(ValueError):
            build_progress_fingerprint(bad)

    def test_resolve_run_key_telemetry_and_fallback(self):
        """resolve_run_key uses telemetry.run_id or stable norun:<hash> fallback."""
        snapshot_with_run = {"telemetry": {"run_id": "proj-run-99"}}
        self.assertEqual(resolve_run_key(snapshot_with_run), "proj-run-99")

        # Fallback when telemetry.run_id is missing or empty
        snapshot_no_run = {
            "project_id": "p1",
            "task_id": "P1.0",
            "state": "EXECUTING",
        }
        rk1 = resolve_run_key(snapshot_no_run)
        rk2 = resolve_run_key(snapshot_no_run)
        self.assertTrue(rk1.startswith("norun:"))
        self.assertEqual(rk1, rk2)

        # Different task produces different norun key
        snapshot_other_task = {
            "project_id": "p1",
            "task_id": "P2.0",
            "state": "EXECUTING",
        }
        rk3 = resolve_run_key(snapshot_other_task)
        self.assertNotEqual(rk1, rk3)

    def test_resolve_run_key_prefers_newest_active_executor_record(self):
        snapshot = {"project_id": "p1", "task_id": "P1.0", "state": "EXECUTING"}
        executor_state = {
            "executions": {
                "stale-completed": {
                    "project_id": "p1",
                    "execution_id": "exec-stale",
                    "state": "completed",
                    "updated_at": "2026-09-12T12:00:00Z",
                },
                "older-active": {
                    "project_id": "p1",
                    "execution_id": "exec-old",
                    "state": "running",
                    "updated_at": "2026-09-12T11:00:00Z",
                },
                "newer-active": {
                    "project_id": "p1",
                    "execution_id": "exec-new",
                    "state": "launching",
                    "updated_at": "2026-09-12T12:30:00Z",
                },
            }
        }
        self.assertEqual(resolve_run_key(snapshot, executor_state), "exec-new")

    def test_resolve_run_key_ignores_terminal_executor_record(self):
        snapshot = {"project_id": "p1", "task_id": "P1.0", "state": "EXECUTING"}
        executor_state = {
            "executions": {
                "only-terminal": {
                    "project_id": "p1",
                    "execution_id": "exec-stale",
                    "state": "completed",
                    "updated_at": "2026-09-12T12:00:00Z",
                }
            }
        }
        self.assertTrue(resolve_run_key(snapshot, executor_state).startswith("norun:"))

    def test_resolve_run_scope_and_attempt_keys(self):
        """run_scope_key and attempt_key are deterministic sha256 derivations."""
        r_scope_1 = resolve_run_scope_key("p1", "T1", "EXECUTING", "run-1")
        r_scope_2 = resolve_run_scope_key("p1", "T1", "EXECUTING", "run-1")
        self.assertEqual(r_scope_1, r_scope_2)
        self.assertEqual(len(r_scope_1), 16)

        att_key_1 = resolve_attempt_key(r_scope_1, "fp1234567890abcd")
        att_key_2 = resolve_attempt_key(r_scope_1, "fp1234567890abcd")
        self.assertEqual(att_key_1, att_key_2)
        self.assertEqual(len(att_key_1), 16)

        # Different fingerprint gives different attempt key
        att_key_3 = resolve_attempt_key(r_scope_1, "fpDifferent00000")
        self.assertNotEqual(att_key_1, att_key_3)


if __name__ == "__main__":
    unittest.main()
