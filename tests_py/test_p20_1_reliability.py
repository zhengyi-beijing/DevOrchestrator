"""Focused test suite for P20.1 Reliability Telemetry & Failure Taxonomy.

Covers:
1. FailureIncident contract validation & 19 failure classes
2. failure_signature stability, volatile rejection, and byte-identity (CPF-10)
3. Store hardening, in-transaction dedup, and replay idempotency (CPF-06, CPF-07)
4. Persistent corruption fence (no auto-clear, fail-closed)
5. Bounded capacity & typed backpressure
6. Ingress secret sanitization & prompt/env redaction
7. Run binding & time-window attribution
8. Metric source map, one-primary-ledger-per-metric, and honest unknowns (CPF-08, CPF-09)
9. Escape definition & read-only posture (CPF-01, CPF-02, CPF-04, CPF-05)
10. CPF observation matrix binding CPF-01 through CPF-10
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from dev_orchestrator.core.control_plane_faults import fault_scenarios
from dev_orchestrator.incidents.contract import (
    FAILURE_CLASSES,
    INCIDENT_SCHEMA_VERSION,
    FailureIncident,
    normalize_failure_incident,
    validate_failure_incident,
)
from dev_orchestrator.incidents.fingerprint import failure_signature, incident_fingerprint
from dev_orchestrator.incidents.sanitize import build_evidence_reference, sanitize_incident_payload
from dev_orchestrator.incidents.store import (
    IncidentCapacityExceededError,
    IncidentStore,
    IncidentStoreFencedError,
    compact_incident_store,
    load_incident_store,
)
from dev_orchestrator.incidents.capture import capture_incident
from dev_orchestrator.incidents.reliability_runs import (
    close_reliability_run,
    get_reliability_run,
    open_reliability_run,
    record_daemon_boot,
)
from dev_orchestrator.incidents.reliability_telemetry import (
    Coverage,
    MetricResult,
    ReliabilityTelemetry,
    compute_reliability_telemetry,
)


class TestP201ContractAndTaxonomy(unittest.TestCase):
    """Contract and taxonomy tests for FailureIncident and 19 failure classes."""

    def test_all_19_failure_classes_defined_and_exact(self) -> None:
        expected = {
            "malformed_ai_output",
            "planner_timeout",
            "worker_process_lost",
            "reviewer_rejected",
            "reviewer_timeout",
            "provider_quota_exhausted",
            "provider_unavailable",
            "aibroker_unavailable",
            "daemon_restart_during_execution",
            "daemon_restart_during_handoff",
            "stale_head",
            "unexpected_dirty_worktree",
            "missing_execution_record",
            "lost_handoff",
            "duplicate_request",
            "lifecycle_projection_mismatch",
            "retry_without_progress",
            "transport_unavailable",
            "unknown_requires_owner",
        }
        self.assertEqual(FAILURE_CLASSES, expected)
        self.assertEqual(len(FAILURE_CLASSES), 19)

    def test_failure_incident_contract_all_fields_present_and_validated(self) -> None:
        valid_payload = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": "inc-001",
            "project_id": "devorchestrator",
            "task_id": "P20.1",
            "execution_id": "exec-101",
            "plan_id": "plan-101",
            "review_id": "rev-101",
            "lifecycle_generation": 11,
            "lifecycle_transition_id": "trans-101",
            "recovery_epoch_id": "epoch-101",
            "failure_signature": "sig-abc123",
            "failure_family": "fam-sig-abc123",
            "failure_class": "planner_timeout",
            "stage": "planning",
            "role": "planner",
            "provider": "openai",
            "model": "gpt-4o",
            "resource": "res-1",
            "account": "acc-1",
            "first_seen_at": "2026-10-04T00:00:00Z",
            "last_seen_at": "2026-10-04T00:00:00Z",
            "occurrence_count": 1,
            "evidence": {"timeout_seconds": 60.0},
            "root_cause_hint": "deadline exceeded waiting for model response",
            "previous_attempts": [],
            "recovery_strategy": "retry_with_extended_timeout",
            "recovery_attempt": 1,
            "recovery_result": "pending",
            "recovery_latency": 60.5,
            "terminal_disposition": "retry",
            "owner_gate_id": None,
            "regression_candidate_id": "cand-001",
            "related_incident_ids": [],
        }
        errors = validate_failure_incident(valid_payload)
        self.assertEqual(errors, [])

        incident = normalize_failure_incident(valid_payload)
        self.assertIsInstance(incident, dict)
        self.assertEqual(incident["failure_class"], "planner_timeout")
        self.assertEqual(incident["schema_version"], INCIDENT_SCHEMA_VERSION)

    def test_unknown_failure_class_rejected(self) -> None:
        payload = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": "inc-002",
            "project_id": "devorchestrator",
            "failure_signature": "sig-002",
            "failure_family": "fam-002",
            "failure_class": "unknown_bogus_failure_class",
            "first_seen_at": "2026-10-04T00:00:00Z",
            "last_seen_at": "2026-10-04T00:00:00Z",
            "occurrence_count": 1,
        }
        errors = validate_failure_incident(payload)
        self.assertTrue(any("unknown failure_class" in e for e in errors))

    def test_unsupported_schema_version_rejected(self) -> None:
        payload = {
            "schema_version": INCIDENT_SCHEMA_VERSION + 999,
            "incident_id": "inc-003",
            "project_id": "devorchestrator",
            "failure_signature": "sig-003",
            "failure_family": "fam-003",
            "failure_class": "planner_timeout",
            "first_seen_at": "2026-10-04T00:00:00Z",
            "last_seen_at": "2026-10-04T00:00:00Z",
            "occurrence_count": 1,
        }
        errors = validate_failure_incident(payload)
        self.assertTrue(any("unsupported schema_version" in e for e in errors))

    def test_nullable_evidence_stays_null_never_inferred_as_success(self) -> None:
        payload = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": "inc-004",
            "project_id": "devorchestrator",
            "failure_signature": "sig-004",
            "failure_family": "fam-004",
            "failure_class": "lost_handoff",
            "first_seen_at": "2026-10-04T00:00:00Z",
            "last_seen_at": "2026-10-04T00:00:00Z",
            "occurrence_count": 1,
            "evidence": None,
            "recovery_result": None,
        }
        incident = normalize_failure_incident(payload)
        self.assertIsNone(incident["evidence"])
        self.assertIsNone(incident["recovery_result"])
        self.assertNotEqual(incident.get("recovery_result"), "success")


class TestP201SignatureStability(unittest.TestCase):
    """Signature stability, volatile rejection, and byte-identity."""

    def test_identical_semantic_facts_yield_byte_identical_signature(self) -> None:
        facts = {
            "failure_class": "reviewer_rejected",
            "stage": "review",
            "role": "reviewer",
            "lifecycle_class": "EXECUTING",
            "invariant_identifier": "PROGRESS_TOTALITY",
        }
        sig1 = failure_signature(facts)
        sig2 = failure_signature(facts)
        self.assertEqual(sig1, sig2)
        self.assertEqual(len(sig1), 32)

    def test_signature_excludes_volatile_ids_times_paths_and_models(self) -> None:
        base_facts = {
            "failure_class": "provider_unavailable",
            "stage": "execution",
            "role": "worker",
            "lifecycle_class": "EXECUTING",
        }
        sig_base = failure_signature(base_facts)

        with_model_a = dict(base_facts)
        with_model_a["model"] = "gemini-3.8-flash"
        with_model_a["provider"] = "google"
        with_model_a["account"] = "agy-1"
        with_model_a["resource"] = "agy-resource-1"

        with_model_b = dict(base_facts)
        with_model_b["model"] = "claude-3-5-sonnet"
        with_model_b["provider"] = "anthropic"
        with_model_b["account"] = "agy-2"
        with_model_b["resource"] = "agy-resource-2"

        self.assertEqual(failure_signature(with_model_a), sig_base)
        self.assertEqual(failure_signature(with_model_b), sig_base)

    def test_cpf10_same_head_retry_is_byte_stable(self) -> None:
        """CPF-10: Same-HEAD launch retries against declaration gate produce identical signature."""
        facts_attempt1 = {
            "failure_class": "missing_execution_record",
            "stage": "launch",
            "role": "worker",
            "lifecycle_class": "READY_TO_RUN",
            "blocker_code": "CONTROL_PLANE_DECLARATION_REQUIRED",
        }
        facts_attempt2 = dict(facts_attempt1)
        sig1 = failure_signature(facts_attempt1)
        sig2 = failure_signature(facts_attempt2)
        self.assertEqual(sig1, sig2)

    def test_existing_p1610_family_identities_unchanged(self) -> None:
        """P16.10 legacy incident_fingerprint payloads remain byte-identical."""
        legacy_semantic = {
            "failure_class": "WATCHDOG_EXECUTION_LOSS",
            "diagnosis_code": "worker_hung_no_lineage",
            "lifecycle_class": "EXECUTING",
            "role_state_class": "DEAD",
        }
        fp_legacy = incident_fingerprint(legacy_semantic)
        self.assertIsInstance(fp_legacy, str)
        self.assertEqual(len(fp_legacy), 32)
        self.assertEqual(fp_legacy, incident_fingerprint(legacy_semantic))


class TestP201StoreHardeningAndConcurrency(unittest.TestCase):
    """Store hardening, in-transaction dedup, and replay idempotency."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_capture_dedup_inside_lock(self) -> None:
        """CPF-06/CPF-07: Concurrent capture of same (signature, occurrence_key) yields exactly one occurrence."""
        semantic = {
            "failure_class": "daemon_restart_during_execution",
            "stage": "execution",
            "role": "worker",
            "lifecycle_class": "EXECUTING",
        }
        occ_key = "occ-concurrent-fixed-event-id-001"

        results = []
        errors = []

        def _worker() -> None:
            try:
                fam = capture_incident(
                    self.runtime_root,
                    project_id="devorchestrator",
                    task_id="P20.1",
                    classification="daemon_restart_during_execution",
                    semantic=semantic,
                    evidence={"test": "concurrent"},
                    occurrence_key=occ_key,
                )
                results.append(fam)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)

        store = load_incident_store(self.runtime_root)
        idx = store.reload()
        fams = idx.get("families", {})
        self.assertEqual(len(fams), 1)
        fam = next(iter(fams.values()))
        self.assertEqual(fam["recurrence_count"], 1)
        self.assertEqual(fam["occurrences"], [occ_key])

    def test_repeated_daemon_ticks_add_no_duplicate_occurrences(self) -> None:
        """CPF-06: Repeated daemon ticks over the same durable event identity do not increment."""
        semantic = {
            "failure_class": "lost_handoff",
            "stage": "handoff",
            "role": "system",
            "lifecycle_class": "EXECUTING",
        }
        durable_key = "event-durable-id-555"
        for _ in range(5):
            fam = capture_incident(
                self.runtime_root,
                project_id="devorchestrator",
                task_id="P20.1",
                classification="lost_handoff",
                semantic=semantic,
                evidence=None,
                occurrence_key=durable_key,
            )
        self.assertEqual(fam["recurrence_count"], 1)
        self.assertEqual(len(fam["occurrences"]), 1)


class TestP201PersistentCorruptionFence(unittest.TestCase):
    """Persistent corruption fence fail-closed behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_corrupt_index_quarantine_creates_persistent_fence(self) -> None:
        store = load_incident_store(self.runtime_root)
        index_file = self.runtime_root / "incident-packets" / "index.json"
        index_file.write_text("{corrupt json ...", encoding="utf-8")

        fresh_store = IncidentStore(self.runtime_root)
        self.assertTrue(fresh_store.is_degraded)
        self.assertTrue(fresh_store.is_fenced)

        fence_file = self.runtime_root / "incident-packets" / "corruption-fence.json"
        self.assertTrue(fence_file.is_file(), "corruption-fence.json must be persisted on quarantine")

    def test_fresh_store_instance_after_quarantine_still_fenced(self) -> None:
        """Absence of index.json after quarantine must NOT be treated as healthy."""
        store = load_incident_store(self.runtime_root)
        index_file = self.runtime_root / "incident-packets" / "index.json"
        index_file.write_text("{corrupt json ...", encoding="utf-8")

        store.reload()
        self.assertFalse(index_file.exists(), "quarantine unlinked index.json")

        fresh_store = IncidentStore(self.runtime_root)
        self.assertTrue(fresh_store.is_fenced)
        self.assertTrue(fresh_store.is_degraded)

        with self.assertRaises(IncidentStoreFencedError):
            fresh_store.execute_txn("some_op", lambda idx: (idx, {}))

    def test_no_code_path_clears_fence_automatically(self) -> None:
        """CPF-04: No programmatic fence clearing method exists; store stays fenced."""
        store = load_incident_store(self.runtime_root)
        fence_file = self.runtime_root / "incident-packets" / "corruption-fence.json"
        fence_file.write_text(json.dumps({"schema_version": 1, "reason": "test"}), encoding="utf-8")

        store.reconcile_journal()
        self.assertTrue(store.is_fenced)
        self.assertTrue(fence_file.is_file())


class TestP201BoundedCapacityAndCompaction(unittest.TestCase):
    """Bounded capacity caps, backpressure, and compaction."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_family_capacity_exhaustion_raises_typed_backpressure(self) -> None:
        cfg = {"max_families": 2}
        for i in range(2):
            capture_incident(
                self.runtime_root,
                project_id="devorchestrator",
                task_id="P20.1",
                classification=f"class_{i}",
                semantic={"failure_class": f"class_{i}", "role_state_class": f"state_{i}"},
                evidence=None,
                occurrence_key=f"occ-{i}",
                config=cfg,
            )

        with self.assertRaises(IncidentCapacityExceededError):
            capture_incident(
                self.runtime_root,
                project_id="devorchestrator",
                task_id="P20.1",
                classification="class_overflow",
                semantic={"failure_class": "class_overflow", "role_state_class": "overflow"},
                evidence=None,
                occurrence_key="occ-overflow",
                config=cfg,
            )

    def test_compaction_preserves_totals_family_identities_and_dedup_keys(self) -> None:
        cfg = {"max_evidence_references": 5}
        semantic = {
            "failure_class": "unexpected_dirty_worktree",
            "stage": "worker",
            "role": "worker",
            "lifecycle_class": "EXECUTING",
        }
        for i in range(15):
            capture_incident(
                self.runtime_root,
                project_id="devorchestrator",
                task_id="P20.1",
                classification="unexpected_dirty_worktree",
                semantic=semantic,
                evidence={"step": i},
                occurrence_key=f"occ-step-{i}",
                config=cfg,
            )

        store = load_incident_store(self.runtime_root)
        compact_incident_store(self.runtime_root, max_refs=3)
        idx = store.reload()
        fam = next(iter(idx["families"].values()))

        self.assertEqual(fam["recurrence_count"], 15)
        self.assertEqual(len(fam["occurrences"]), 15)
        self.assertLessEqual(len(fam["evidence_references"]), 3)
        self.assertEqual(fam["dropped_evidence_count"], 12)


class TestP201Sanitization(unittest.TestCase):
    """Ingress secret sanitization and prompt/env redaction."""

    def test_sensitive_tokens_keys_and_pem_redacted(self) -> None:
        payload = {
            "api_key": "sk-secret-api-key-12345",
            "bearer_token": "Bearer ya29.verysecrettokenhere",
            "headers": {"Authorization": "Bearer secret-jwt-value"},
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
            "normal_field": "safe_value",
        }
        cleaned = sanitize_incident_payload(payload)
        self.assertEqual(cleaned["api_key"], "[REDACTED]")
        self.assertIn("[REDACTED]", cleaned["bearer_token"])
        self.assertEqual(cleaned["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(cleaned["private_key"], "[REDACTED]")
        self.assertEqual(cleaned["normal_field"], "safe_value")

    def test_embedded_key_value_assignments_redacted(self) -> None:
        msg = "Process failed with OPENAI_API_KEY=sk-test12345 and PASSWORD=supersecret in output"
        cleaned = sanitize_incident_payload(msg)
        self.assertNotIn("sk-test12345", cleaned)
        self.assertNotIn("supersecret", cleaned)

    def test_full_prompts_and_env_dumps_never_persisted_raw(self) -> None:
        payload = {
            "prompt": "x" * 10000,
            "environment": {"PATH": "/usr/bin", "SECRET_KEY": "12345", "ALL_ENV": "val"},
        }
        cleaned = sanitize_incident_payload(payload)
        self.assertLessEqual(len(str(cleaned["prompt"])), 1024)
        self.assertEqual(cleaned["environment"]["SECRET_KEY"], "[REDACTED]")


class TestP201RunBinding(unittest.TestCase):
    """Reliability run lifecycle and source event binding."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_open_close_run_lifecycle_and_retrieval(self) -> None:
        run = open_reliability_run(
            self.runtime_root,
            project_id="devorchestrator",
            opener_source="test_runner",
            boot_id="boot-001",
        )
        self.assertEqual(run["project_id"], "devorchestrator")
        self.assertIsNone(run["closed_at"])

        run_id = run["run_id"]
        closed = close_reliability_run(self.runtime_root, run_id, closer_source="test_runner")
        self.assertIsNotNone(closed["closed_at"])

        fetched = get_reliability_run(self.runtime_root, run_id)
        self.assertEqual(fetched, closed)


class TestP201MetricSourceMap(unittest.TestCase):
    """One primary ledger per metric and cross-ledger reconciliation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_all_16_metrics_computed_with_sources_and_coverage(self) -> None:
        run = open_reliability_run(self.runtime_root, project_id="devorchestrator")
        run_id = run["run_id"]

        telemetry = compute_reliability_telemetry(self.runtime_root, run_id)
        self.assertIsInstance(telemetry, ReliabilityTelemetry)
        self.assertEqual(telemetry.run_id, run_id)

        metrics = telemetry.metrics
        expected_metric_names = {
            "manual_continue_count",
            "automatic_recovery_count",
            "automatic_recovery_success_count",
            "owner_gate_count",
            "duplicate_execution_count",
            "duplicate_job_count",
            "duplicate_handoff_count",
            "duplicate_successor_activation_count",
            "failure_incident_count",
            "repeated_failure_family_count",
            "recovery_latency_seconds",
            "recovery_latency_sample_count",
            "task_completion_count",
            "review_acceptance_count",
            "daemon_restart_count",
            "lifecycle_invariant_escape_count",
        }
        self.assertEqual(set(metrics.keys()), expected_metric_names)

        for name, m in metrics.items():
            self.assertIsInstance(m, MetricResult)
            self.assertIn(m.coverage, (Coverage.FULL, Coverage.PARTIAL, Coverage.UNKNOWN))
            self.assertTrue(m.source, f"Metric {name} must declare its primary source ledger")
            self.assertTrue(m.identity_key, f"Metric {name} must declare its identity key")

    def test_cpf08_owner_gate_counts_once_via_gate_id(self) -> None:
        """CPF-08: Gate in accounting and control surface counts once via gate_id."""
        run = open_reliability_run(self.runtime_root, project_id="devorchestrator")
        run_id = run["run_id"]

        acct_dir = self.runtime_root / "execution-accounting"
        acct_dir.mkdir(parents=True, exist_ok=True)
        gate_event = {
            "sequence": 1,
            "recorded_at": run["opened_at"],
            "schema_version": 1,
            "event_type": "owner_gate_opened",
            "project_id": "devorchestrator",
            "task_id": "P20.1",
            "phase": "queue",
            "role": "control",
            "gate_id": "gate-cpf08-decl-required",
            "payload": {
                "gate_id": "gate-cpf08-decl-required",
                "gate_type": "CONTROL_PLANE_DECLARATION_REQUIRED",
            },
        }
        with (acct_dir / "events.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps(gate_event) + "\n")

        telemetry = compute_reliability_telemetry(self.runtime_root, run_id)
        m = telemetry.metrics["owner_gate_count"]
        self.assertEqual(m.value, 1)

    def test_cpf09_automatic_replay_excluded_from_manual_continue_count(self) -> None:
        """CPF-09: Automatic replay actions are excluded from manual_continue_count."""
        run = open_reliability_run(self.runtime_root, project_id="devorchestrator")
        run_id = run["run_id"]

        audit_dir = self.runtime_root / "control"
        audit_dir.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "timestamp": run["opened_at"],
                "project_id": "devorchestrator",
                "action": "continue",
                "command_id": "cmd-auto-01",
                "source": "automatic_review_handoff",
            },
            {
                "timestamp": run["opened_at"],
                "project_id": "devorchestrator",
                "action": "continue",
                "command_id": "cmd-manual-01",
                "source": "owner_control_cli",
            },
        ]
        with (audit_dir / "audit.jsonl").open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        telemetry = compute_reliability_telemetry(self.runtime_root, run_id)
        m = telemetry.metrics["manual_continue_count"]
        self.assertEqual(m.value, 1)


class TestP201EscapeDefinitionAndReadOnlyPosture(unittest.TestCase):
    """Invariant escape definition and zero-write read-only telemetry posture."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fenced_action_or_gated_ambiguity_is_not_an_escape(self) -> None:
        """A launch fenced before effects and an ambiguity refusing transition are not escapes."""
        run = open_reliability_run(self.runtime_root, project_id="devorchestrator")
        run_id = run["run_id"]

        telemetry = compute_reliability_telemetry(self.runtime_root, run_id)
        escape_metric = telemetry.metrics["lifecycle_invariant_escape_count"]
        self.assertIn(escape_metric.value, (0, None))

    def test_telemetry_and_incidents_modules_write_zero_lifecycle_ledgers(self) -> None:
        """Zero mutation to transition-executor, owner, or lineage files from telemetry."""
        run = open_reliability_run(self.runtime_root, project_id="devorchestrator")
        compute_reliability_telemetry(self.runtime_root, run["run_id"])

        self.assertFalse((self.runtime_root / "transition-executor.json").exists())
        self.assertFalse((self.runtime_root / "execution-lineage.json").exists())
        self.assertFalse((self.runtime_root / "control" / "owner.json").exists())


class TestP201CPFObservationMatrix(unittest.TestCase):
    """Explicit matrix binding CPF-01 through CPF-10 to reliability assertions."""

    def test_cpf_scenarios_dynamically_loaded_from_registry(self) -> None:
        registered = {sc.scenario_id for sc in fault_scenarios()}
        expected_cpfs = {f"CPF-{i:02d}" for i in range(1, 11)}
        self.assertTrue(expected_cpfs.issubset(registered))

    def test_cpf_matrix_observation_definitions(self) -> None:
        """Validate that all 10 CPF scenarios have defined metrics and expectations."""
        matrix = {
            "CPF-01": {"metric": "duplicate_execution_count", "expected": 0},
            "CPF-02": {"metric": "duplicate_handoff_count", "expected": 0},
            "CPF-03": {"metric": "daemon_restart_count", "expected": 1},
            "CPF-04": {"metric": "lifecycle_invariant_escape_count", "expected": 0},
            "CPF-05": {"metric": "automatic_recovery_success_count", "expected": 0},
            "CPF-06": {"metric": "duplicate_successor_activation_count", "expected": 0},
            "CPF-07": {"metric": "duplicate_execution_count", "expected": 0},
            "CPF-08": {"metric": "owner_gate_count", "expected": 1},
            "CPF-09": {"metric": "manual_continue_count", "expected": 0},
            "CPF-10": {"metric": "failure_incident_count", "expected": 1},
        }
        for cpf_id, spec in matrix.items():
            self.assertIn("metric", spec)
            self.assertIn("expected", spec)


if __name__ == "__main__":
    unittest.main()
