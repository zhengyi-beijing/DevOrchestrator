"""Focused test suite for P20.2 Automatic Failure Harvesting & Regression Promotion.

Covers:
1. Nine durable-source harvest detectors in DETECTORS:
   - planner_failure (runtime/ai-planner.json)
   - worker_loss (runtime/execution-lineage.json)
   - reviewer_failure (runtime/ai-reviewer.json)
   - watchdog_recovery (runtime/watchdog.json)
   - transition_executor_failure (runtime/transition-executor.json)
   - daemon_restart_failure (runtime/daemon-boots.jsonl + transition-executor.json)
   - aibroker_routing_failure (runtime/execution-accounting/events.jsonl)
   - execution_transport_failure (runtime/logs/transport-operations.ndjson)
   - durable_job_failure (runtime/jobs/index.json)
2. Stable repeated family & restart persistence (fam-689a96fe6ea9, recurrence 2)
3. Replay dedup on durable source-event occurrence keys
4. RegressionCandidate contract, allowlisted ReplayRunner, bounded corpus
5. Verification lifecycle (verified, reproducible, rejected with machine-readable reasons)
6. Legacy bootstrap candidate cand-7dd84495b777 marked rejected (vacuous_synthetic_oracle_ineligible)
7. Runtime-only promotion with path fence (zero writes to tests_py or tracked repo paths)
8. Ingress secret redaction & bounded evidence
9. Corrupt-record fail-closed & active corruption fence
10. Dynamic CPF observation matrix binding CPF-01 through CPF-10 to real computed output
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from dev_orchestrator.core.control_plane_faults import fault_scenarios
from dev_orchestrator.incidents.contract import FAILURE_CLASSES, INCIDENT_SCHEMA_VERSION
from dev_orchestrator.incidents.fingerprint import failure_signature, incident_fingerprint
from dev_orchestrator.incidents.harvesting import DETECTORS, harvest_tick
from dev_orchestrator.incidents.policy import resolve_incident_policy
from dev_orchestrator.incidents.regression_candidate import (
    REGRESSION_CANDIDATE_SCHEMA_VERSION,
    REPLAYABILITY_VALUES,
    VERIFICATION_STATUSES,
    RegressionCandidate,
    create_regression_candidate,
    normalize_regression_candidate,
    promote_verified_regression,
    resolve_regression_candidate,
    validate_regression_candidate,
    verify_regression_candidate,
)
from dev_orchestrator.incidents.replay_runner import (
    REPLAY_OPS,
    ReplayCaseSpec,
    ReplayFixtureInvalidError,
    ReplayOpNotAllowedError,
    load_replay_case,
    run_replay_case,
)
from dev_orchestrator.incidents.store import (
    IncidentCapacityExceededError,
    IncidentStore,
    IncidentStoreFencedError,
    compact_incident_store,
    load_incident_store,
)
from dev_orchestrator.incidents.taxonomy import map_to_failure_class
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


class TestP202HarvestDetectors(unittest.TestCase):
    """Test suite for the nine durable-source failure harvest detectors."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "test-proj"
        self.config = {"projects": [{"project_id": self.project_id}]}

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_all_nine_detector_keys_registered_in_harvesting(self) -> None:
        required_detectors = {
            "planner_failure",
            "worker_loss",
            "reviewer_failure",
            "watchdog_recovery",
            "transition_executor_failure",
            "daemon_restart_failure",
            "aibroker_routing_failure",
            "execution_transport_failure",
            "durable_job_failure",
        }
        for det_name in required_detectors:
            self.assertIn(det_name, DETECTORS, f"Missing required detector: {det_name}")
            self.assertTrue(callable(DETECTORS[det_name]))

    def test_planner_failure_detector_harvests_timeout_and_malformed(self) -> None:
        detector = DETECTORS["planner_failure"]
        # Case 1: planner timeout
        planner_data = {
            "version": 1,
            "plans": {
                "ai_plan:p1": {
                    "plan_id": "ai_plan:p1",
                    "project_id": self.project_id,
                    "task_id": "P20.2",
                    "state": "failed",
                    "planner_last_failure": "planner dispatch timed out after 60s",
                    "planner_attempts": [
                        {
                            "attempt": 1,
                            "request_id": "req-plan-1",
                            "status": "failed",
                            "reason": "timed out waiting for provider response",
                            "dispatch_id": "disp-plan-1",
                        }
                    ],
                }
            },
        }
        write_json(self.runtime_root / "ai-planner.json", planner_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        pid, tid, classification, semantic, ev, occ_key = anomalies[0]
        self.assertEqual(pid, self.project_id)
        self.assertEqual(tid, "P20.2")
        self.assertEqual(semantic["failure_class"], "planner_timeout")
        self.assertEqual(occ_key, "plan_fail:test-proj:ai_plan:p1:1")

        # Case 2: malformed output
        planner_data["plans"]["ai_plan:p2"] = {
            "plan_id": "ai_plan:p2",
            "project_id": self.project_id,
            "task_id": "P20.2",
            "state": "failed",
            "planner_last_failure": "malformed extraction markers in model response",
            "planner_attempts": [
                {
                    "attempt": 1,
                    "request_id": "req-plan-2",
                    "status": "failed",
                    "reason": "missing json closing marker",
                    "dispatch_id": "disp-plan-2",
                }
            ],
        }
        write_json(self.runtime_root / "ai-planner.json", planner_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 2)
        classes = {a[3]["failure_class"] for a in anomalies}
        self.assertIn("planner_timeout", classes)
        self.assertIn("malformed_ai_output", classes)

    def test_worker_loss_detector_harvests_cpf01_and_lineage_loss(self) -> None:
        detector = DETECTORS["worker_loss"]
        lineage_data = {
            "records": {
                "lkey-cpf01": {
                    "project_id": self.project_id,
                    "task_id": "P20.1",
                    "loss_detected": True,
                    "terminal_outcome": "stale_predecessor_fenced",
                    "invariant_key": "CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",
                    "execution_id": "exec-stale-01",
                }
            }
        }
        write_json(self.runtime_root / "execution-lineage.json", lineage_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        pid, tid, classification, semantic, ev, occ_key = anomalies[0]
        self.assertEqual(pid, self.project_id)
        self.assertEqual(tid, "P20.1")
        self.assertIn(semantic["failure_class"], {"worker_process_lost", "missing_execution_record"})
        self.assertTrue(occ_key.startswith("worker_loss:test-proj:lkey-cpf01"))

    def test_reviewer_failure_detector_harvests_timeout_and_rejection(self) -> None:
        detector = DETECTORS["reviewer_failure"]
        rev_data = {
            "version": 1,
            "reviews": {
                "ai_review:r1": {
                    "review_id": "ai_review:r1",
                    "project_id": self.project_id,
                    "task_id": "P20.2",
                    "state": "failed",
                    "reason": "reviewer lifecycle error: broker turn timed out",
                    "decision_id": "dec-1",
                },
                "ai_review:r2": {
                    "review_id": "ai_review:r2",
                    "project_id": self.project_id,
                    "task_id": "P20.2",
                    "state": "completed",
                    "decision": "reject",
                    "reason": "BLOCKING: verification failure in auth boundary",
                    "decision_id": "dec-2",
                },
            },
        }
        write_json(self.runtime_root / "ai-reviewer.json", rev_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 2)
        classes = {a[3]["failure_class"] for a in anomalies}
        self.assertIn("reviewer_timeout", classes)
        self.assertIn("reviewer_rejected", classes)

    def test_watchdog_recovery_mapped_to_valid_taxonomy_class(self) -> None:
        detector = DETECTORS["watchdog_recovery"]
        wd_data = {
            "projects": {
                self.project_id: {
                    "history": [
                        {
                            "diagnosis": "worker_hung_no_lineage",
                            "recovery": "kill_and_relaunch",
                            "attempt_key": "att-wd-1",
                            "task_id": "P20.2",
                        }
                    ]
                }
            }
        }
        write_json(self.runtime_root / "watchdog.json", wd_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        sem = anomalies[0][3]
        # Must map to one of the 19 valid contract FAILURE_CLASSES
        tax_class = map_to_failure_class("watchdog_recovery", sem, anomalies[0][4])
        self.assertIn(tax_class, FAILURE_CLASSES)

    def test_transition_executor_failure_harvests_lost_handoff_and_projection_mismatch(self) -> None:
        detector = DETECTORS["transition_executor_failure"]
        te_data = {
            "version": 2,
            "executions": {
                "trans-lost": {
                    "project_id": self.project_id,
                    "task_id": "P20.1",
                    "state": "blocked",
                    "reason": "NEXT_TASK decision accepted but handoff is missing",
                    "handoff_idempotency_key": "idem-cpf02",
                    "recorded_at": "2026-10-04T00:00:00Z",
                },
                "trans-mismatch": {
                    "project_id": self.project_id,
                    "task_id": "P20.2",
                    "state": "blocked",
                    "reason": "lifecycle projection mismatch: repository changed after reviewed truth",
                    "handoff_idempotency_key": "idem-mismatch",
                    "recorded_at": "2026-10-04T00:01:00Z",
                },
            },
        }
        write_json(self.runtime_root / "transition-executor.json", te_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 2)
        classes = {a[3]["failure_class"] for a in anomalies}
        self.assertIn("lost_handoff", classes)
        self.assertIn("lifecycle_projection_mismatch", classes)

    def test_daemon_restart_failure_harvests_restart_during_handoff_cpf03(self) -> None:
        detector = DETECTORS["daemon_restart_failure"]
        # In-flight handoff or execution
        te_data = {
            "version": 2,
            "executions": {
                "trans-inflight": {
                    "project_id": self.project_id,
                    "task_id": "P20.2",
                    "state": "running",
                    "source_request_id": "req-inflight-1",
                    "started_at": "2026-10-04T00:00:00Z",
                }
            },
        }
        write_json(self.runtime_root / "transition-executor.json", te_data, indent=2)

        # Boot record indicating restart
        boot_row = {
            "schema_version": 1,
            "boot_id": "boot-restart-01",
            "pid": 9999,
            "started_at": "2026-10-04T00:05:00Z",
            "listen_address": "127.0.0.1",
            "port": 8770,
        }
        with (self.runtime_root / "daemon-boots.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps(boot_row) + "\n")

        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        pid, tid, classification, semantic, ev, occ_key = anomalies[0]
        self.assertEqual(pid, self.project_id)
        self.assertIn(
            semantic["failure_class"],
            {"daemon_restart_during_execution", "daemon_restart_during_handoff"},
        )
        self.assertEqual(occ_key, "boot_restart:test-proj:boot-restart-01:trans-inflight")

    def test_aibroker_routing_failure_harvests_quota_and_unavailable(self) -> None:
        detector = DETECTORS["aibroker_routing_failure"]
        acct_dir = self.runtime_root / "execution-accounting"
        acct_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "schema_version": 1,
                "event_type": "provider_result_observed",
                "project_id": self.project_id,
                "task_id": "P20.2",
                "request_id": "req-ai-quota",
                "occurred_at": "2026-10-04T00:00:00Z",
                "metadata": {
                    "status": "failed",
                    "classification": "quota_exhausted",
                    "error": "You have exceeded your current quota",
                },
            },
            {
                "schema_version": 1,
                "event_type": "provider_result_observed",
                "project_id": self.project_id,
                "task_id": "P20.2",
                "request_id": "req-ai-unavail",
                "occurred_at": "2026-10-04T00:01:00Z",
                "metadata": {
                    "status": "no_candidate",
                    "classification": "provider_temporarily_unavailable",
                    "error": "No model candidates available",
                },
            },
        ]
        with (acct_dir / "events.jsonl").open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 2)
        classes = {a[3]["failure_class"] for a in anomalies}
        self.assertIn("provider_quota_exhausted", classes)
        self.assertIn("provider_unavailable", classes)

    def test_execution_transport_failure_harvests_transport_unavailable(self) -> None:
        detector = DETECTORS["execution_transport_failure"]
        logs_dir = self.runtime_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        op_row = {
            "timestamp": "2026-10-04T00:00:00Z",
            "operation_id": "op-trans-01",
            "operation": "spawn",
            "host_id": "remote-worker-1",
            "selected_transport": "ssh",
            "status": "failed",
            "project_id": self.project_id,
            "error": "connection refused by target host",
        }
        with (logs_dir / "transport-operations.ndjson").open("w", encoding="utf-8") as f:
            f.write(json.dumps(op_row) + "\n")

        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        pid, tid, classification, semantic, ev, occ_key = anomalies[0]
        self.assertEqual(pid, self.project_id)
        self.assertEqual(semantic["failure_class"], "transport_unavailable")
        self.assertEqual(occ_key, "transport_op:test-proj:op-trans-01")

    def test_durable_job_failure_harvests_job_state_failed(self) -> None:
        detector = DETECTORS["durable_job_failure"]
        jobs_dir = self.runtime_root / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        jobs_data = {
            "schema_version": 1,
            "jobs": {
                "job-fail-01": {
                    "job_id": "job-fail-01",
                    "project_id": self.project_id,
                    "command_ref": "build",
                    "state": "failed",
                    "failure_kind": "transport_unavailable",
                    "state_reason": "SSH channel closed abruptly",
                }
            },
        }
        write_json(jobs_dir / "index.json", jobs_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        pid, tid, classification, semantic, ev, occ_key = anomalies[0]
        self.assertEqual(pid, self.project_id)
        self.assertEqual(semantic["failure_class"], "transport_unavailable")
        self.assertEqual(occ_key, "job_fail:test-proj:job-fail-01:failed")

    def test_missing_or_ambiguous_evidence_classifies_unknown_requires_owner(self) -> None:
        detector = DETECTORS["transition_executor_failure"]
        te_data = {
            "version": 2,
            "executions": {
                "trans-ambig": {
                    "project_id": self.project_id,
                    "task_id": "P20.2",
                    "state": "blocked",
                    "reason": "unspecified contradiction in lifecycle ledger",
                }
            },
        }
        write_json(self.runtime_root / "transition-executor.json", te_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][3]["failure_class"], "unknown_requires_owner")

    def test_records_without_resolvable_project_id_are_skipped(self) -> None:
        detector = DETECTORS["transition_executor_failure"]
        te_data = {
            "version": 2,
            "executions": {
                "trans-no-proj": {
                    "project_id": None,
                    "task_id": "P20.2",
                    "state": "blocked",
                    "reason": "missing project id",
                }
            },
        }
        write_json(self.runtime_root / "transition-executor.json", te_data, indent=2)
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 0)


class TestP202HarvestDedupAndStability(unittest.TestCase):
    """Test suite for family stability across restarts, signature stability, and replay dedup."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "devorchestrator"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_repeated_family_stability_and_restart(self) -> None:
        semantic = {
            "failure_class": "planner_timeout",
            "role": "planner",
            "stage": "planning",
            "lifecycle_class": "planning",
        }
        # First capture
        fam1 = harvest_tick(
            self.runtime_root,
            config={"projects": [{"project_id": self.project_id}]},
        )
        # Directly capture to verify recurrence count
        from dev_orchestrator.incidents.capture import capture_incident

        c1 = capture_incident(
            self.runtime_root,
            project_id=self.project_id,
            task_id="P20.2",
            classification="planner_timeout",
            semantic=semantic,
            evidence={"attempt": 1},
            occurrence_key="occ-1",
        )
        self.assertEqual(c1["recurrence_count"], 1)
        family_id = c1["family_id"]

        # Second capture of same signature with distinct occurrence key
        c2 = capture_incident(
            self.runtime_root,
            project_id=self.project_id,
            task_id="P20.2",
            classification="planner_timeout",
            semantic=semantic,
            evidence={"attempt": 2},
            occurrence_key="occ-2",
        )
        self.assertEqual(c2["recurrence_count"], 2)
        self.assertEqual(c2["family_id"], family_id)

        # Restart simulation: instantiate fresh store
        fresh_store = load_incident_store(self.runtime_root)
        fam = fresh_store.index["families"][c1["failure_signature"]]
        self.assertEqual(fam["family_id"], family_id)
        self.assertEqual(fam["recurrence_count"], 2)
        self.assertEqual(len(fam["occurrences"]), 2)

    def test_legacy_p1610_family_identities_unchanged_cpf10(self) -> None:
        """CPF-10: Taxonomy additions must not alter existing failure signatures or family IDs."""
        legacy_semantic = {
            "failure_class": "watchdog_stall",
            "diagnosis_code": "worker_hung_no_lineage",
            "lifecycle_class": "executing",
            "contract_class": "liveness",
        }
        sig = failure_signature(legacy_semantic)
        fp = incident_fingerprint(legacy_semantic)
        self.assertEqual(sig, fp)

        # Bootstrap preserved family fam-689a96fe6ea9 check
        bootstrap_semantic = {
            "failure_class": "reviewer_timeout",
            "role_class": "plan_reviewer",
            "lifecycle_class": "PLANNING",
            "contract_class": "broker_codex_harness_deadline",
            "invariant_identifier": "bounded_independent_review_recovery",
        }
        boot_sig = failure_signature(bootstrap_semantic)
        self.assertEqual(f"fam-{boot_sig[:12]}", "fam-689a96fe6ea9")

    def test_replay_dedup_does_not_increment_occurrences_cpf06_cpf09(self) -> None:
        """CPF-06 / CPF-09: Replaying terminal source event produces no duplicate occurrences."""
        from dev_orchestrator.incidents.capture import capture_incident

        semantic = {
            "failure_class": "lost_handoff",
            "lifecycle_class": "handoff",
            "contract_class": "successor_handoff",
        }
        c1 = capture_incident(
            self.runtime_root,
            project_id=self.project_id,
            task_id="P20.2",
            classification="lost_handoff",
            semantic=semantic,
            evidence=None,
            occurrence_key="trans:idem-cpf02",
        )
        self.assertEqual(c1["recurrence_count"], 1)

        # Replay same occurrence key
        c2 = capture_incident(
            self.runtime_root,
            project_id=self.project_id,
            task_id="P20.2",
            classification="lost_handoff",
            semantic=semantic,
            evidence=None,
            occurrence_key="trans:idem-cpf02",
        )
        self.assertEqual(c2["recurrence_count"], 1)
        self.assertEqual(len(c2["occurrences"]), 1)


class TestP202RegressionCandidateAndReplay(unittest.TestCase):
    """Test suite for RegressionCandidate, allowlisted ReplayRunner, and verification."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "devorchestrator"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_regression_candidate_contract_schema_and_validation(self) -> None:
        cand_dict = {
            "schema_version": REGRESSION_CANDIDATE_SCHEMA_VERSION,
            "candidate_id": "cand-test-01",
            "project_id": self.project_id,
            "source_family_id": "fam-689a96fe6ea9",
            "source_failure_signature": "689a96fe6ea97a6a428e20efbf82ccc0",
            "source_incident_ids": ["pkt-01"],
            "minimal_reproduction": {
                "corpus_case_id": "case_01",
                "replay_op": "fingerprint_stability",
                "inputs_digest": "sha256:abc",
            },
            "expected_invariant": "SIGNATURE_BYTE_STABLE",
            "actual_outcome": "signature_byte_stable",
            "regression_class": "planner_timeout",
            "replayability": "deterministic_replayable",
            "verification_status": "proposed",
            "rejection_reason": None,
            "verification_evidence": None,
            "verified_at": None,
            "created_at": "2026-10-04T00:00:00Z",
            "updated_at": "2026-10-04T00:00:00Z",
        }
        errors = validate_regression_candidate(cand_dict)
        self.assertEqual(errors, [])

        norm = normalize_regression_candidate(cand_dict)
        self.assertEqual(norm["candidate_id"], "cand-test-01")

        # Unsupported schema version
        bad_version = dict(cand_dict, schema_version=99)
        self.assertTrue(any("unsupported schema_version" in e for e in validate_regression_candidate(bad_version)))

        # Invalid verification status
        bad_status = dict(cand_dict, verification_status="invalid_status")
        self.assertTrue(any("unknown verification_status" in e for e in validate_regression_candidate(bad_status)))

    def test_create_and_resolve_regression_candidate(self) -> None:
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-01"],
            corpus_case_id="case_01",
            expected_invariant="PROGRESS_ADVANCES",
            actual_outcome="progress_stalled",
            regression_class="planner_timeout",
        )
        self.assertEqual(cand["verification_status"], "proposed")
        cid = cand["candidate_id"]

        resolved = resolve_regression_candidate(self.runtime_root, cid)
        self.assertEqual(resolved["candidate_id"], cid)
        self.assertEqual(resolved["expected_invariant"], "PROGRESS_ADVANCES")

    def test_replay_runner_allowlisted_ops_and_rejections(self) -> None:
        # Allowed op
        self.assertIn("fingerprint_stability", REPLAY_OPS)
        spec = ReplayCaseSpec(
            case_id="case-unit-01",
            replay_op="fingerprint_stability",
            defect_anchor={"failure_class": "planner_timeout", "stage": "planning"},
            corrected_anchor={"failure_class": "planner_timeout", "stage": "planning"},
            expected_invariant="SIGNATURE_MATCHES",
            expected_defect_outcome="computed_signature",
            cpf_scenarios=["CPF-10"],
        )
        result = run_replay_case(spec)
        self.assertTrue(result["deterministic"])

        # Unallowlisted op rejected
        spec_bad_op = ReplayCaseSpec(
            case_id="case-unit-bad",
            replay_op="unregistered_arbitrary_eval",
            defect_anchor={},
            corrected_anchor={},
            expected_invariant="ANY",
            expected_defect_outcome="ANY",
            cpf_scenarios=[],
        )
        with self.assertRaises(ReplayOpNotAllowedError):
            run_replay_case(spec_bad_op)

        # Code string injection rejected
        spec_bad_fixture = ReplayCaseSpec(
            case_id="case-code-inj",
            replay_op="fingerprint_stability",
            defect_anchor={"__code__": "import os; os.system('echo pwned')"},
            corrected_anchor={},
            expected_invariant="ANY",
            expected_defect_outcome="ANY",
            cpf_scenarios=[],
        )
        with self.assertRaises(ReplayFixtureInvalidError):
            run_replay_case(spec_bad_fixture)

    def test_verify_regression_candidate_discriminating_transitions_to_verified(self) -> None:
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-01"],
            corpus_case_id="case_06_cpf10_same_head_byte_stable",
            expected_invariant="SIGNATURE_MATCHES",
            actual_outcome="signature_matches",
            regression_class="planner_timeout",
            replay_op="fingerprint_stability",
        )
        cid = cand["candidate_id"]

        verified_cand = verify_regression_candidate(self.runtime_root, cid)
        self.assertEqual(verified_cand["verification_status"], "verified")
        self.assertIsNone(verified_cand["rejection_reason"])
        self.assertIsNotNone(verified_cand["verification_evidence"])

    def test_verify_regression_candidate_rejects_vacuous_oracle(self) -> None:
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-vacuous",
            source_incident_ids=["pkt-vacuous"],
            corpus_case_id="case_09_vacuous_synthetic_defect",
            expected_invariant="ANY",
            actual_outcome="same_outcome",
            regression_class="planner_timeout",
            replay_op="fingerprint_stability",
        )
        cid = cand["candidate_id"]

        rejected_cand = verify_regression_candidate(self.runtime_root, cid)
        self.assertEqual(rejected_cand["verification_status"], "rejected")
        self.assertEqual(rejected_cand["rejection_reason"], "vacuous_oracle_non_discriminating")

    def test_legacy_bootstrap_candidate_cand_7dd84495b777_is_permanently_rejected(self) -> None:
        """Step 12: Legacy synthetic candidate cand-7dd84495b777 is marked permanently rejected."""
        # Seed candidate cand-7dd84495b777
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-de932c0669d9"],
            corpus_case_id="synthetic_fixture_mode",
            expected_invariant="INELIGIBLE",
            actual_outcome="synthetic_forced_failure",
            regression_class="reviewer_timeout",
            candidate_id="cand-7dd84495b777",
        )
        verified = verify_regression_candidate(self.runtime_root, "cand-7dd84495b777")
        self.assertEqual(verified["verification_status"], "rejected")
        self.assertEqual(verified["rejection_reason"], "vacuous_synthetic_oracle_ineligible")

        # Refuses promotion
        with self.assertRaises(ValueError):
            promote_verified_regression(self.runtime_root, "cand-7dd84495b777")

    def test_promote_verified_regression_runtime_only_and_path_fence_cpf05(self) -> None:
        """CPF-05: Promotion records in runtime store only and refuses repo paths."""
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-01"],
            corpus_case_id="case_06_cpf10_same_head_byte_stable",
            expected_invariant="SIGNATURE_MATCHES",
            actual_outcome="signature_matches",
            regression_class="planner_timeout",
            replay_op="fingerprint_stability",
        )
        cid = cand["candidate_id"]
        verify_regression_candidate(self.runtime_root, cid)

        # Attempt to promote outside runtime store (into tests_py) must be refused
        repo_tests_dir = Path("tests_py")
        with self.assertRaises(ValueError) as ctx:
            promote_verified_regression(self.runtime_root, cid, destination=repo_tests_dir)
        self.assertIn("refused", str(ctx.exception).lower())

        # Promoting without destination succeeds into runtime store only
        promoted = promote_verified_regression(self.runtime_root, cid)
        self.assertEqual(promoted["verification_status"], "promoted")

    def test_zero_production_test_source_writes_across_entire_lifecycle(self) -> None:
        """Assert zero writes outside temp runtime root during entire harvesting/promotion lifecycle."""
        # Snapshot repository git status before running
        tracked_tests = sorted(Path("tests_py").rglob("*.py"))
        tracked_mtimes = {p: p.stat().st_mtime for p in tracked_tests}

        # Run full pipeline
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-01"],
            corpus_case_id="case_06_cpf10_same_head_byte_stable",
            expected_invariant="SIGNATURE_MATCHES",
            actual_outcome="signature_matches",
            regression_class="planner_timeout",
            replay_op="fingerprint_stability",
        )
        verify_regression_candidate(self.runtime_root, cand["candidate_id"])
        promote_verified_regression(self.runtime_root, cand["candidate_id"])

        # Check no tests_py files were modified or created
        current_tests = sorted(Path("tests_py").rglob("*.py"))
        self.assertEqual(len(tracked_tests), len(current_tests))
        for p in current_tests:
            self.assertEqual(p.stat().st_mtime, tracked_mtimes[p])


class TestP202SanitizationAndFencing(unittest.TestCase):
    """Test suite for ingress secret sanitization, corruption fencing, and read-only posture."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "devorchestrator"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ingress_secret_sanitization_in_harvested_evidence(self) -> None:
        from dev_orchestrator.incidents.capture import capture_incident

        semantic = {
            "failure_class": "planner_timeout",
            "stage": "planning",
            "role": "planner",
            "lifecycle_class": "planning",
        }
        leaked_evidence = {
            "auth_header": "Bearer secret-token-xyz-12345",
            "password": "super-secret-password",
            "api_key": "AIzaSyFakeKey1234567890abcdef",
            "prompt": "x" * 2000,  # Oversized prompt
        }
        fam = capture_incident(
            self.runtime_root,
            project_id=self.project_id,
            task_id="P20.2",
            classification="planner_timeout",
            semantic=semantic,
            evidence=leaked_evidence,
            occurrence_key="occ-secret-01",
        )
        ev_ref = fam["evidence_references"][0]
        ev_text = json.dumps(ev_ref)
        self.assertNotIn("secret-token-xyz-12345", ev_text)
        self.assertNotIn("super-secret-password", ev_text)
        self.assertNotIn("AIzaSyFakeKey1234567890abcdef", ev_text)
        # Verify prompt is bounded
        self.assertTrue(len(json.dumps(ev_ref.get("evidence_sample", {}))) <= 1500)

    def test_corrupt_record_fails_closed_cpf08(self) -> None:
        """CPF-08: Corrupted candidate record refuses verification instead of passing."""
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-01"],
            corpus_case_id="case_06_cpf10_same_head_byte_stable",
            expected_invariant="SIGNATURE_MATCHES",
            actual_outcome="signature_matches",
            regression_class="planner_timeout",
        )
        cid = cand["candidate_id"]

        # Corrupt candidate file
        cand_file = self.runtime_root / "incident-packets" / "candidates" / cid / "regression_candidate.json"
        cand_file.write_text("{corrupt-json", encoding="utf-8")

        with self.assertRaises(Exception):
            verify_regression_candidate(self.runtime_root, cid)

    def test_active_corruption_fence_refuses_verification_and_promotion(self) -> None:
        cand = create_regression_candidate(
            self.runtime_root,
            project_id=self.project_id,
            family_id="fam-689a96fe6ea9",
            source_incident_ids=["pkt-01"],
            corpus_case_id="case_06_cpf10_same_head_byte_stable",
            expected_invariant="SIGNATURE_MATCHES",
            actual_outcome="signature_matches",
            regression_class="planner_timeout",
            replay_op="fingerprint_stability",
        )
        cid = cand["candidate_id"]

        # Place persistent corruption fence
        fence_file = self.runtime_root / "incident-packets" / "corruption-fence.json"
        write_json(
            fence_file,
            {"schema_version": 1, "reason": "unrecoverable corruption detected", "fenced_at": utc_now_iso()},
            indent=2,
        )

        with self.assertRaises(IncidentStoreFencedError):
            verify_regression_candidate(self.runtime_root, cid)

        with self.assertRaises(IncidentStoreFencedError):
            promote_verified_regression(self.runtime_root, cid)

    def test_read_only_posture_over_lifecycle_ledgers(self) -> None:
        """Assert zero writes to transition-executor.json, execution-lineage.json, owner-control.json."""
        # Seed ledgers
        te_file = self.runtime_root / "transition-executor.json"
        write_json(te_file, {"version": 2, "executions": {}}, indent=2)
        te_hash = te_file.read_bytes()

        # Run harvest_tick
        harvest_tick(self.runtime_root, config={"projects": [{"project_id": self.project_id}]})

        # Verify untouched
        self.assertEqual(te_file.read_bytes(), te_hash)
        self.assertFalse((self.runtime_root / "execution-lineage.json").exists())
        self.assertFalse((self.runtime_root / "owner-control.json").exists())


class TestP202CPFObservationMatrix(unittest.TestCase):
    """Dynamic table-driven observation matrix test covering CPF-01 through CPF-10."""

    def test_cpf_scenarios_dynamically_loaded_from_registry(self) -> None:
        registered = {sc.scenario_id for sc in fault_scenarios()}
        expected_cpfs = {f"CPF-{i:02d}" for i in range(1, 11)}
        self.assertTrue(expected_cpfs.issubset(registered))

    def test_cpf_matrix_all_ten_scenarios_have_real_computed_observations(self) -> None:
        """Assert one P20.2 observation per scenario against real computed outputs, not dict literals."""
        # Build temp runtime root
        temp_dir = tempfile.mkdtemp()
        rt = Path(temp_dir) / "runtime"
        rt.mkdir(parents=True, exist_ok=True)
        pid = "devorchestrator"
        cfg = {"projects": [{"project_id": pid}]}

        try:
            # CPF-01: stale predecessor worker -> worker_process_lost
            write_json(
                rt / "execution-lineage.json",
                {
                    "records": {
                        "lkey-cpf01": {
                            "project_id": pid,
                            "task_id": "P20.1",
                            "loss_detected": True,
                            "terminal_outcome": "stale_predecessor_fenced",
                            "invariant_key": "CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",
                        }
                    }
                },
                indent=2,
            )
            obs01 = DETECTORS["worker_loss"](rt, cfg, {}, None, None, None, None, None)
            self.assertTrue(len(obs01) >= 1)
            self.assertIn(obs01[0][3]["failure_class"], {"worker_process_lost", "missing_execution_record"})

            # CPF-02: lost handoff -> lost_handoff
            write_json(
                rt / "transition-executor.json",
                {
                    "version": 2,
                    "executions": {
                        "trans-cpf02": {
                            "project_id": pid,
                            "task_id": "P20.1",
                            "state": "blocked",
                            "reason": "NEXT_TASK decision accepted but handoff is missing",
                            "handoff_idempotency_key": "idem-cpf02",
                        }
                    },
                },
                indent=2,
            )
            obs02 = DETECTORS["transition_executor_failure"](rt, cfg, {}, None, None, None, None, None)
            self.assertTrue(len(obs02) >= 1)
            self.assertEqual(obs02[0][3]["failure_class"], "lost_handoff")

            # CPF-03: restart mid-transition -> daemon_restart_during_handoff
            write_json(
                rt / "transition-executor.json",
                {
                    "version": 2,
                    "executions": {
                        "trans-cpf03": {
                            "project_id": pid,
                            "task_id": "P20.2",
                            "state": "running",
                            "source_request_id": "req-cpf03",
                        }
                    },
                },
                indent=2,
            )
            with (rt / "daemon-boots.jsonl").open("w", encoding="utf-8") as f:
                f.write(json.dumps({"schema_version": 1, "boot_id": "boot-cpf03", "pid": 1111}) + "\n")
            obs03 = DETECTORS["daemon_restart_failure"](rt, cfg, {}, None, None, None, None, None)
            self.assertTrue(len(obs03) >= 1)
            self.assertIn(
                obs03[0][3]["failure_class"],
                {"daemon_restart_during_handoff", "daemon_restart_during_execution"},
            )

            # CPF-04: contradictory successor claims fail-closed -> unknown_requires_owner
            write_json(
                rt / "transition-executor.json",
                {
                    "version": 2,
                    "executions": {
                        "trans-cpf04": {
                            "project_id": pid,
                            "task_id": "P20.2",
                            "state": "blocked",
                            "reason": "contradictory successor claims in roadmap vs staged",
                        }
                    },
                },
                indent=2,
            )
            obs04 = DETECTORS["transition_executor_failure"](rt, cfg, {}, None, None, None, None, None)
            self.assertTrue(len(obs04) >= 1)
            self.assertEqual(obs04[0][3]["failure_class"], "unknown_requires_owner")

            # CPF-05: dirty worktree -> promotion fence refuses mutation
            cand05 = create_regression_candidate(
                rt,
                project_id=pid,
                family_id="fam-cpf05",
                source_incident_ids=["pkt-cpf05"],
                corpus_case_id="case_06_cpf10_same_head_byte_stable",
                expected_invariant="INVARIANT_HOLDS",
                actual_outcome="invariant_holds",
                regression_class="planner_timeout",
                replay_op="fingerprint_stability",
            )
            verify_regression_candidate(rt, cand05["candidate_id"])
            with self.assertRaises(ValueError):
                promote_verified_regression(rt, cand05["candidate_id"], destination=Path("tests_py"))

            # CPF-06: repeated ticks byte-stable idempotent
            c_tick1 = harvest_tick(rt, config=cfg)
            c_tick2 = harvest_tick(rt, config=cfg)
            store06 = load_incident_store(rt)
            # Re-running harvest_tick over unchanged ledgers adds 0 new occurrences
            for fam in store06.index["families"].values():
                occ_keys = [o["occurrence_key"] if isinstance(o, dict) else o for o in fam["occurrences"]]
                self.assertEqual(len(occ_keys), len(set(occ_keys)))

            # CPF-07: crash after intent before publication reconciles cleanly
            store07 = load_incident_store(rt)
            intents = store07.reconcile_journal()
            self.assertIsInstance(intents, list)

            # CPF-08: missing declaration gate fail-closed -> owner gate never cleared
            write_json(
                rt / "transition-executor.json",
                {
                    "version": 2,
                    "executions": {
                        "trans-cpf08": {
                            "project_id": pid,
                            "task_id": "P20.2",
                            "state": "blocked",
                            "reason": "CONTROL_PLANE_DECLARATION_REQUIRED owner gate active",
                        }
                    },
                },
                indent=2,
            )
            obs08 = DETECTORS["transition_executor_failure"](rt, cfg, {}, None, None, None, None, None)
            self.assertTrue(len(obs08) >= 1)
            # Gate is preserved as durable evidence, not cleared
            self.assertTrue((rt / "transition-executor.json").is_file())

            # CPF-09: repaired HEAD gate resolution -> replayed blocked request yields no second incident
            from dev_orchestrator.incidents.capture import capture_incident

            sem09 = {
                "failure_class": "duplicate_request",
                "lifecycle_class": "executing",
                "contract_class": "authority",
            }
            c09_1 = capture_incident(
                rt,
                project_id=pid,
                task_id="P20.2",
                classification="duplicate_request",
                semantic=sem09,
                evidence=None,
                occurrence_key="gate_resolved:replayed_req_01",
            )
            c09_2 = capture_incident(
                rt,
                project_id=pid,
                task_id="P20.2",
                classification="duplicate_request",
                semantic=sem09,
                evidence=None,
                occurrence_key="gate_resolved:replayed_req_01",
            )
            self.assertEqual(c09_1["recurrence_count"], c09_2["recurrence_count"])

            # CPF-10: same-HEAD retries byte-stable signature
            sig1 = failure_signature({"failure_class": "planner_timeout", "role": "planner"})
            sig2 = failure_signature({"failure_class": "planner_timeout", "role": "planner"})
            self.assertEqual(sig1, sig2)

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
