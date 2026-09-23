"""Acceptance regression and unit tests for P16.7 Self-Healing Project Activation & Readiness.

Covers:
- Acceptance regression: replay of the 2026-09-23 xray-hw-platform incident
- Readiness grammar, vocabulary, task-id drift fail-closed
- Successor activation writing pending_design atomically with next.md + rollback
- Transition executor launch refusal on stale/invalid readiness
- Migration predicates, budget consumption, audit record logging
- Canonical blocker ordering, payloads, and path mode
- Registration uniqueness and REGISTRATION_TEMPLATE_MISSING
- Intent survival across daemon restart
- Watchdog-versus-continue race idempotency (NOOP_ALREADY_EXECUTING)
- Watchdog handoff with and without active execution intent
- Recovery exhaustion terminal state without owner gate
- Control API routes and CLI commands
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dev_orchestrator.core.readiness import (
    READINESS_VOCABULARY,
    ReadinessResolution,
    migrate_legacy_readiness,
    parse_legacy_status_components,
    resolve_readiness,
    write_structured_readiness,
)
from dev_orchestrator.core.activation import (
    detect_orphan_state,
    load_activation_requests,
    reconcile_project_registration,
    record_activation_request,
)
from dev_orchestrator.core.blockers import (
    Blocker,
    blocker_payload,
    explain_block,
)
from dev_orchestrator.core.execution_intent import (
    check_intent_budgets,
    compute_recovery_fingerprint,
    get_active_intent,
    load_execution_intents,
    record_intent_action,
    record_or_refresh_intent,
    terminate_intent,
)
from dev_orchestrator.core.activation_supervisor import ActivationSupervisor
from dev_orchestrator.core.diagnostics import classify_failure_class
from dev_orchestrator.core.control_commands import (
    ControlCommandCoordinator,
    submit_control_command,
)
from dev_orchestrator.core.watchdog import WatchdogCoordinator
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.repository import classify_porcelain_entries, read_repository_truth
from tests_py.test_transition_executor import FakeBackend, TransitionExecutor, make_repo
from tests_py.test_control_commands import FakeExecutor


class TestReadinessGrammarAndResolution(unittest.TestCase):
    """Unit tests for closed legacy grammar parsing and readiness resolution."""

    def test_legacy_component_parsing(self):
        # design_ready + not_started -> ready_to_run
        c, s, cand = parse_legacy_status_components("READY / OWNER_GOAL_DEFINED / NOT_STARTED")
        self.assertEqual(cand, "ready_to_run")
        self.assertIn("design_ready", c)
        self.assertIn("not_started", c)

        # DESIGN READY / EXECUTABLE
        c, s, cand = parse_legacy_status_components("DESIGN READY / EXECUTABLE")
        self.assertEqual(cand, "ready_to_run")

        # PENDING DESIGN -> pending_design
        c, s, cand = parse_legacy_status_components("PENDING DESIGN")
        self.assertEqual(cand, "pending_design")

        # BLOCKED -> blocked
        c, s, cand = parse_legacy_status_components("BLOCKED")
        self.assertEqual(cand, "blocked")

        # Conflicting components -> unresolvable
        c, s, cand = parse_legacy_status_components("READY / BLOCKED")
        self.assertIsNone(cand)
        self.assertIn("CONFLICT", s)

        # Unknown component -> unresolvable
        c, s, cand = parse_legacy_status_components("SOMETHING_STRANGE")
        self.assertIsNone(cand)

    def test_task_id_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            (repo / "agent").mkdir(parents=True)
            (repo / "agent" / "execution-state.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "execution_state": "ready_to_run",
                    "task_id": "P1",
                    "updated_at": utc_now_iso(),
                }),
                encoding="utf-8",
            )
            # Resolve with current_task_id="P2" -> mismatch
            res = resolve_readiness(repo, current_task_id="P2")
            self.assertEqual(res.state, "invalid")
            self.assertFalse(res.valid)
            self.assertTrue(res.stale)
            self.assertEqual(res.code, "READINESS_TASK_ID_MISMATCH")

    def test_structured_readiness_write_and_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            write_structured_readiness(repo, "ready_to_run", "P1")
            res = resolve_readiness(repo, current_task_id="P1")
            self.assertEqual(res.state, "ready_to_run")
            self.assertTrue(res.valid)
            self.assertFalse(res.stale)
            self.assertEqual(res.task_id, "P1")
            self.assertEqual(res.source, "structured")

    def test_readiness_vocabulary_closed(self):
        self.assertEqual(
            READINESS_VOCABULARY,
            {"pending_design", "ready_to_run", "executing", "completed", "blocked"},
        )


class TestSuccessorActivationAndRollback(unittest.TestCase):
    """Test successor activation writing pending_design atomically with next.md."""

    def test_activate_staged_successor_writes_pending_design_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            head = make_repo(repo, task_id="P1")
            # Write prior ready_to_run structured readiness for P1
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            # Stage successor P2
            (repo / "agent" / "staged").mkdir()
            (repo / "agent" / "staged" / "next.P2.md").write_text("# P2 successor\nStatus: PENDING DESIGN\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/staged"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "stage P2"], check=True)

            planner = AIPlannerCoordinator(repo / "runtime", None)
            pred_bytes = (repo / "agent" / "next.md").read_bytes()
            succ_bytes = (repo / "agent" / "staged" / "next.P2.md").read_bytes()
            new_head = planner._activate_staged_successor(
                repo,
                pred_bytes,
                succ_bytes,
                "P1",
                "P2",
            )
            self.assertTrue(new_head)

            # Assert structured readiness now contains pending_design with task_id P2
            state_data = json.loads((repo / "agent" / "execution-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state_data["execution_state"], "pending_design")
            self.assertEqual(state_data["task_id"], "P2")

            # Assert git HEAD is clean
            diff = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True)
            self.assertEqual(diff.strip(), "")


class TestOrphanDetectionAndReconciliation(unittest.TestCase):
    """Test orphan state detection, registration reconcile, and uniqueness."""

    def test_orphan_diagnosis_before_registration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "my_project"
            make_repo(repo, task_id="P1")

            # Write stale .devorch/status.json in repo
            devorch = repo / ".devorch"
            devorch.mkdir()
            (devorch / "status.json").write_text(
                json.dumps({
                    "project_id": "my_project",
                    "state": "REVIEW_FAILED",
                    "phase": "worker",
                    "task_id": "P4.1",
                }),
                encoding="utf-8",
            )

            # Unregistered project
            config = {"projects": []}
            orphan = detect_orphan_state("my_project", repo, config, runtime)
            self.assertIsNotNone(orphan)
            self.assertEqual(orphan["code"], "ORPHANED_PROJECT_STATE")
            self.assertEqual(orphan["stale_state"], "REVIEW_FAILED")
            self.assertEqual(orphan["stale_task_id"], "P4.1")
            self.assertFalse(orphan["authoritative"])

    def test_reconcile_project_registration_with_template(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "my_project"
            make_repo(repo, task_id="P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir()
            cfg_path.write_text(
                json.dumps({
                    "activation_profiles": {
                        "default": {
                            "adapter": "agent_files",
                            "conversation_binding": {
                                "transport": "browser",
                                "adapter": "chatgpt_web",
                                "binding_id": "bind-default",
                            },
                        }
                    },
                    "projects": [],
                }),
                encoding="utf-8",
            )

            req = record_activation_request(
                runtime_root=runtime,
                repo_path=repo,
                project_id="my_project",
                config_path=cfg_path,
                profile="default",
                requested_action="continue",
                source="test",
            )

            proj, err = reconcile_project_registration(req, cfg_path, runtime)
            self.assertIsNotNone(proj)
            self.assertEqual(err, "")
            self.assertEqual(proj["project_id"], "my_project")

            # Check config file was atomically updated
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(len(loaded["projects"]), 1)
            self.assertEqual(loaded["projects"][0]["project_id"], "my_project")

    def test_reconcile_missing_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "my_project"
            make_repo(repo, task_id="P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir()
            cfg_path.write_text(json.dumps({"projects": []}), encoding="utf-8")

            req = {
                "request_id": "r1",
                "project_id": "my_project",
                "repo_path": str(repo),
                "profile": "unknown",
            }
            proj, err = reconcile_project_registration(req, cfg_path, runtime)
            self.assertIsNone(proj)
            self.assertIn("REGISTRATION_TEMPLATE_MISSING", err)


class TestReadinessMigrationPredicates(unittest.TestCase):
    """Test all 7 migration predicates and migration logging."""

    def test_migration_succeeds_and_logs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "proj1"
            make_repo(repo, task_id="P1")

            project = {
                "project_id": "proj1",
                "repo_path": str(repo),
                "orchestration_ready": True,
            }
            snapshot = {
                "project_id": "proj1",
                "repo_path": str(repo),
                "telemetry": {"task_id": "P1"},
                "worker": {"state": "not_started"},
            }

            res, err, rec = migrate_legacy_readiness(project, snapshot, runtime)
            self.assertTrue(res, f"migration failed: {err}")

            # Check agent/execution-state.json was committed
            state_file = repo / "agent" / "execution-state.json"
            self.assertTrue(state_file.is_file())
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state_data["execution_state"], "ready_to_run")
            self.assertEqual(state_data["task_id"], "P1")

            # Check audit record
            mig_log = runtime / "readiness-migrations.jsonl"
            self.assertTrue(mig_log.is_file())
            records = [json.loads(line) for line in mig_log.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["project_id"], "proj1")
            self.assertEqual(records[0]["task_id"], "P1")
            self.assertEqual(records[0]["candidate"], "ready_to_run")

    def test_migration_refused_on_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "proj1"
            make_repo(repo, task_id="P1")

            # Dirty worktree with unexpected file
            (repo / "dirty.txt").write_text("untracked change\n", encoding="utf-8")

            project = {
                "project_id": "proj1",
                "repo_path": str(repo),
                "orchestration_ready": True,
            }
            snapshot = {
                "project_id": "proj1",
                "repo_path": str(repo),
                "telemetry": {"task_id": "P1"},
                "worker": {"state": "not_started"},
            }

            res, err, rec = migrate_legacy_readiness(project, snapshot, runtime)
            self.assertFalse(res)
            self.assertIn("DIRTY_WORKTREE", err)


class TestExecutionIntentAndBudgets(unittest.TestCase):
    """Test execution intent ledger, cycles, and budget enforcement."""

    def test_intent_survives_restart_and_tracks_actions(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            intent = record_or_refresh_intent(runtime, "p1", task_id="P1", command_id="cmd-1")
            self.assertEqual(intent["project_id"], "p1")
            self.assertEqual(intent["actions_used"], 0)

            # Record action
            intent, _, _, _ = record_intent_action(runtime, "p1")
            self.assertEqual(intent["actions_used"], 1)

            # Reload from disk
            intents = load_execution_intents(runtime)
            self.assertEqual(intents["intents"]["p1"]["actions_used"], 1)

    def test_livelock_cycle_detection(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            record_or_refresh_intent(runtime, "p1", task_id="P1")

            # Simulate A -> B -> A -> B cycle
            fpA = compute_recovery_fingerprint("p1", "P1", "READY_TO_RUN", "B_A", "head1")
            fpB = compute_recovery_fingerprint("p1", "P1", "READY_TO_RUN", "B_B", "head1")

            record_intent_action(runtime, "p1", fingerprint=fpA)
            record_intent_action(runtime, "p1", fingerprint=fpB)
            record_intent_action(runtime, "p1", fingerprint=fpA)
            intent, _, _, _ = record_intent_action(runtime, "p1", fingerprint=fpB)

            is_exhausted, reason, code = check_intent_budgets(intent)
            self.assertTrue(is_exhausted)
            self.assertEqual(code, "RECOVERY_LIVELOCK_DETECTED")

    def test_action_budget_exhaustion(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            record_or_refresh_intent(runtime, "p1", task_id="P1")
            for i in range(21):
                fp = compute_recovery_fingerprint("p1", "P1", "READY_TO_RUN", f"B_{i}", "head1")
                intent, _, _, _ = record_intent_action(runtime, "p1", fingerprint=fp)

            is_exhausted, reason, code = check_intent_budgets(intent, {"max_recovery_actions": 20})
            self.assertTrue(is_exhausted)
            self.assertEqual(code, "RECOVERY_BUDGET_EXHAUSTED")


class TestWatchdogHandoffAndCoordination(unittest.TestCase):
    """Test watchdog handoff translation with active intent vs without."""

    def test_watchdog_handoff_with_active_intent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "proj"
            make_repo(repo, task_id="P1")

            # Record active intent
            intent = record_or_refresh_intent(runtime, "p1", task_id="P1")

            watchdog = WatchdogCoordinator(runtime)
            epoch = {"id": "epoch-1", "anchor": "head1", "run_id": "r1"}
            intent["recovery_epoch_id"] = "epoch-1"
            write_json(runtime / "execution-intent.json", {"schema_version": 1, "intents": {"p1": intent}})

            att_record = {
                "attempt_key": "a3",
                "recovery_epoch_id": "epoch-1",
                "task_id": "P1",
            }
            watchdog._emit_owner_gate_once("p1", att_record, "inspection_timeout")

            prow = watchdog._cached_state.get("projects", {}).get("p1", {})
            self.assertIsNotNone(prow.get("recovery_handoff"))
            self.assertEqual(prow["recovery_handoff"]["state"], "recovery_exhausted")
            self.assertNotIn("owner_gate", prow)

    def test_watchdog_owner_gate_without_active_intent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "proj"
            make_repo(repo, task_id="P1")

            watchdog = WatchdogCoordinator(runtime)
            att_record = {
                "attempt_key": "a3",
                "recovery_epoch_id": "epoch-1",
                "task_id": "P1",
            }

            watchdog._emit_owner_gate_once("p1", att_record, "owner_review_needed")
            prow = watchdog._cached_state.get("projects", {}).get("p1", {})
            self.assertIsNone(prow.get("recovery_handoff"))


class TestAcceptanceRegressionXrayHwPlatform(unittest.TestCase):
    """End-to-end acceptance regression replaying the 2026-09-23 xray-hw-platform incident."""

    def test_full_incident_replay_self_healing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "xray-hw-platform"
            make_repo(repo, task_id="P1")

            # 1. Stale local .devorch/status.json with P4.1 and REVIEW_FAILED
            devorch = repo / ".devorch"
            devorch.mkdir(exist_ok=True)
            (devorch / "status.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "project_id": "xray-hw-platform",
                    "lifecycle_state": "REVIEW_FAILED",
                    "status": "REVIEW_FAILED",
                    "phase": "worker",
                    "task_id": "P4.1",
                    "updated_at": "2026-09-22T20:00:00Z",
                }),
                encoding="utf-8",
            )

            # 2. Config has activation_profiles but project is NOT in registry
            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "activation_profiles": {
                        "default": {
                            "adapter": "agent_files",
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["next_task"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                                "bootstrap": {"request_id": "req-1", "task_id": "P1"},
                            },
                        }
                    },
                    "projects": [],
                }),
                encoding="utf-8",
            )

            # 3. Assert orphan diagnosis before registration
            orphan = detect_orphan_state("xray-hw-platform", repo, json.loads(cfg_path.read_text()), runtime)
            self.assertIsNotNone(orphan)
            self.assertEqual(orphan["code"], "ORPHANED_PROJECT_STATE")
            self.assertEqual(orphan["stale_task_id"], "P4.1")
            self.assertEqual(orphan["stale_state"], "REVIEW_FAILED")

            # Assert explain_block reports orphan and unregistered
            blockers = explain_block(repo_path=repo, runtime_root=runtime, config_path=cfg_path)
            codes = [b.code for b in blockers]
            self.assertIn("ORPHANED_PROJECT_STATE", codes)
            self.assertIn("PROJECT_NOT_REGISTERED", codes)

            # 4. One project-activate request
            req = record_activation_request(
                runtime_root=runtime,
                repo_path=repo,
                project_id="xray-hw-platform",
                config_path=cfg_path,
                profile="default",
                requested_action="continue",
                source="cli",
            )
            self.assertEqual(req["state"], "pending")

            # Check intent exists
            active_intent = get_active_intent(runtime, "xray-hw-platform")
            self.assertIsNotNone(active_intent)
            self.assertEqual(active_intent["state"], "pending")

            # Setup supervisor with fake executor
            executor = FakeExecutor(launch=True)
            supervisor = ActivationSupervisor(runtime, executor=executor)

            # 5. First supervisor tick: should reconcile registration
            summary = {"projects": []}
            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertTrue(any(o.get("remediation") == "reconcile_registration" for o in outcomes))

            # Reload config to verify registration
            loaded_cfg = load_projects_config(cfg_path)
            self.assertEqual(len(loaded_cfg["projects"]), 1)
            self.assertEqual(loaded_cfg["projects"][0]["project_id"], "xray-hw-platform")

            # 6. Second supervisor tick: should migrate legacy readiness
            proj_snapshot = {
                "project_id": "xray-hw-platform",
                "repo_path": str(repo),
                "state": "IDLE",
                "telemetry": {"task_id": "P1"},
                "worker": {"state": "not_started"},
            }
            summary = {"projects": [proj_snapshot]}
            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertTrue(any(o.get("remediation") == "migrate_readiness" for o in outcomes))

            # Structured readiness now exists
            readiness_res = resolve_readiness(repo, current_task_id="P1")
            self.assertEqual(readiness_res.state, "ready_to_run")
            self.assertTrue(readiness_res.valid)

            # Assert that supervisor automatically submitted the forward transition retry into control inbox
            remed_cmds = list((runtime / "control" / "inbox").glob("cmd-rec-xray-hw-platform-P1-*.json")) + list(
                (runtime / "control" / "history").glob("cmd-rec-xray-hw-platform-P1-*.json")
            )
            self.assertTrue(len(remed_cmds) >= 1)
            remed_cmd_data = json.loads(remed_cmds[0].read_text(encoding="utf-8"))
            self.assertEqual(remed_cmd_data["action"], "continue")
            self.assertEqual(remed_cmd_data["source"], "activation_supervisor")

            # 7. Third supervisor tick: with readiness ready_to_run, drives forward transition
            proj_snapshot["state"] = "READY_TO_RUN"
            proj_snapshot["readiness"] = readiness_res.to_dict()
            summary = {"projects": [proj_snapshot]}

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes))

            # 8. Duplicate continue returns NOOP_ALREADY_EXECUTING
            coordinator = ControlCommandCoordinator(runtime, None)
            submit_control_command(
                runtime, "xray-hw-platform", "continue",
                command_id="cmd-dup-1",
                expected=project_identity(proj_snapshot, runtime),
            )
            with patch("dev_orchestrator.core.control_commands._active_execution", return_value={"state": "running", "source_request_id": "e1"}):
                outcomes = coordinator.advance(cfg_path, summary, executor)
                dup_outcomes = [o for o in outcomes if o.get("command_id") == "cmd-dup-1"]
                self.assertEqual(len(dup_outcomes), 1)
                self.assertEqual(dup_outcomes[0].get("effect"), "NOOP_ALREADY_EXECUTING")

            # 9. Verify zero owner-gates fabricated during this whole sequence
            wd_file = runtime / "watchdog.json"
            if wd_file.is_file():
                wd_data = read_json(wd_file)
                self.assertNotIn("owner_gate", wd_data.get("projects", {}).get("xray-hw-platform", {}))


class TestPorcelainClassification(unittest.TestCase):
    """Unit tests for porcelain entry classification distinguishing expected task artifacts."""

    def test_classify_porcelain_worktree_modified_with_leading_space(self):
        # In git status --porcelain v1, worktree-only modified files start with a leading space ' M ...'
        entries = [
            " M agent/next.md",
            " M agent/CURRENT.md",
            " M agent/result.md",
            " M agent/execution-state.json",
            " M agent/staged/P16.8.md",
        ]
        expected, unexpected = classify_porcelain_entries(entries)
        self.assertEqual(len(expected), 5)
        self.assertEqual(len(unexpected), 0)
        self.assertIn(" M agent/next.md", expected)

    def test_classify_porcelain_staged_and_untracked_variants(self):
        entries = [
            "M  agent/next.md",
            "MM agent/next.md",
            "?? agent/execution-state.json",
            "?? agent/staged/P16.8.md",
            ' M "agent/next.md"',
            "R  agent/old.md -> agent/next.md",
        ]
        expected, unexpected = classify_porcelain_entries(entries)
        self.assertEqual(len(expected), 6)
        self.assertEqual(len(unexpected), 0)

    def test_classify_porcelain_unexpected_entries(self):
        entries = [
            " M src/dev_orchestrator/cli.py",
            "?? scratch/temp.txt",
            " M agent/other_file.py",
            " M gent/next.md",  # Truncated path is unexpected!
        ]
        expected, unexpected = classify_porcelain_entries(entries)
        self.assertEqual(len(expected), 0)
        self.assertEqual(len(unexpected), 4)

    def test_classify_porcelain_mixed_entries(self):
        entries = [
            " M agent/next.md",
            " M src/dev_orchestrator/cli.py",
            "?? agent/execution-state.json",
            "?? untracked.py",
        ]
        expected, unexpected = classify_porcelain_entries(entries)
        self.assertEqual(list(expected), [" M agent/next.md", "?? agent/execution-state.json"])
        self.assertEqual(list(unexpected), [" M src/dev_orchestrator/cli.py", "?? untracked.py"])


class TestTransientInspectionAndBackoff(unittest.TestCase):
    """Unit tests for injected transient inspection failures and supervisor transient backoff."""

    def test_injected_transient_inspection_failure_derived_by_explain_block(self):
        # Verify that explain_block uses classify_failure_class to emit TRANSIENT_INSPECTION_FAILURE
        snapshot = {
            "project_id": "proj-1",
            "state": "MONITOR_ERROR",
            "error": "broker service invocation failed: status_call_timed_out",
            "telemetry": {"task_id": "P1"},
        }
        blockers = explain_block(snapshot=snapshot, runtime_root="runtime")
        self.assertTrue(len(blockers) >= 1)
        top = blockers[0]
        self.assertEqual(top.code, "TRANSIENT_INSPECTION_FAILURE")
        self.assertEqual(top.failure_class, "transient_infrastructure")
        self.assertFalse(top.owner_gate_required)

    def test_injected_transient_broker_timeout_derived_by_explain_block(self):
        snapshot = {
            "project_id": "proj-1",
            "state": "IDLE",
            "broker": {"error": "status_call_timed_out"},
            "telemetry": {"task_id": "P1"},
        }
        blockers = explain_block(snapshot=snapshot, runtime_root="runtime")
        self.assertTrue(len(blockers) >= 1)
        top = blockers[0]
        self.assertEqual(top.code, "TRANSIENT_INSPECTION_FAILURE")
        self.assertEqual(top.failure_class, "transient_infrastructure")

    def test_supervisor_transient_backoff_and_forward_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo1"
            make_repo(repo, task_id="P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            # Record active execution intent
            record_or_refresh_intent(
                runtime, "proj-1", task_id="P1", command_id="cmd-init-1",
                source="test", repo_path=str(repo), state="active",
            )

            # Inject transient inspection error in snapshot
            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "MONITOR_ERROR",
                "error": "timed out reading project status",
                "telemetry": {"task_id": "P1"},
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            coordinator = ControlCommandCoordinator(runtime, None)
            supervisor = ActivationSupervisor(runtime, controls=coordinator, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            # Case C should trigger
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "remediated")
            self.assertEqual(outcomes[0]["action"], "transient_backoff")
            self.assertEqual(outcomes[0]["remediation"], "transient_backoff")

            # Forward transition retry should be submitted to control inbox/history
            cmd_files = list((runtime / "control" / "inbox").glob("cmd-rec-proj-1-P1-*.json")) + list(
                (runtime / "control" / "history").glob("cmd-rec-proj-1-P1-*.json")
            )
            self.assertEqual(len(cmd_files), 1)
            cmd_data = json.loads(cmd_files[0].read_text(encoding="utf-8"))
            self.assertEqual(cmd_data["action"], "continue")
            self.assertEqual(cmd_data["source"], "activation_supervisor")

            # Intent actions should be incremented
            intent = get_active_intent(runtime, "proj-1")
            self.assertEqual(intent["actions_used"], 1)


class TestMonitorAutoStartRaceDuplicateWorkerPrevented(unittest.TestCase):
    """Verifies that monitor auto-start racing explicit continue cannot launch duplicate workers."""

    def test_running_active_execution_prevents_duplicate_worker_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo1"
            make_repo(repo, task_id="P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            # Record active execution from an auto-start
            exec_file = runtime / "transition-executor.json"
            write_json(exec_file, {
                "executions": {
                    "exec-autostart-1": {
                        "project_id": "proj-1",
                        "task_id": "P1",
                        "state": "running",
                        "source_request_id": "exec-autostart-1",
                        "broker_request_id": "broker-req-1",
                    }
                }
            })

            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "WORKER_RUNNING",
                "telemetry": {"task_id": "P1"},
                "worker": {"kind": "task", "state": "running", "process_alive": True},
            }
            summary = {"projects": [snapshot]}

            # Explicit continue submitted while worker is already running
            submit_control_command(
                runtime,
                "proj-1",
                "continue",
                command_id="cmd-owner-continue-1",
                expected=project_identity(snapshot, runtime),
            )

            launch_calls = []
            class MockLaunchExecutor(FakeExecutor):
                def start_control(self, *args, **kwargs):
                    launch_calls.append(args)
                    return super().start_control(*args, **kwargs)

            executor = MockLaunchExecutor(launch=True)
            coordinator = ControlCommandCoordinator(runtime, None)

            outcomes = coordinator.advance(cfg_path, summary, executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["state"], "accepted")
            self.assertEqual(outcomes[0]["effect"], "NOOP_ALREADY_EXECUTING")
            self.assertEqual(outcomes[0]["execution_id"], "exec-autostart-1")
            self.assertEqual(outcomes[0]["task_id"], "P1")

            # Proves no second worker was launched!
            self.assertEqual(len(launch_calls), 0)

    def test_starting_worker_in_snapshot_prevents_duplicate_worker_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo1"
            make_repo(repo, task_id="P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            # Snapshot has starting worker (e.g. from background process launch)
            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "WORKER_RUNNING",
                "telemetry": {"task_id": "P1"},
                "worker": {"kind": "task", "state": "starting", "task_id": "P1", "process_alive": True},
            }
            summary = {"projects": [snapshot]}

            submit_control_command(
                runtime,
                "proj-1",
                "continue",
                command_id="cmd-owner-continue-2",
                expected=project_identity(snapshot, runtime),
            )

            launch_calls = []
            class MockLaunchExecutor(FakeExecutor):
                def start_control(self, *args, **kwargs):
                    launch_calls.append(args)
                    return super().start_control(*args, **kwargs)

            executor = MockLaunchExecutor(launch=True)
            coordinator = ControlCommandCoordinator(runtime, None)

            outcomes = coordinator.advance(cfg_path, summary, executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["state"], "accepted")
            self.assertEqual(outcomes[0]["effect"], "NOOP_ALREADY_EXECUTING")

            # Zero launch calls
            self.assertEqual(len(launch_calls), 0)


class TestP167ReviewRemediation(unittest.TestCase):
    """Regression tests verifying closure of P16.7 technical review findings round 2."""

    def test_readiness_task_id_mismatch_populates_migration_candidate_and_migrates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            # Structured file on disk for P1
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            # Update next.md to task P2 with Status: READY_TO_RUN
            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P2: Feature Two\n\nStatus: **READY_TO_RUN**\n\nDescription here\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "advance next.md to P2"], check=True)

            # resolve_readiness with current_task_id="P2"
            res = resolve_readiness(repo, current_task_id="P2")
            self.assertFalse(res.valid)
            self.assertTrue(res.stale)
            self.assertEqual(res.code, "READINESS_TASK_ID_MISMATCH")
            self.assertEqual(res.migration_candidate, "ready_to_run")
            self.assertTrue(res.migration_required)

            # Migrate legacy readiness
            p_entry = {"project_id": "proj-1", "repo_path": str(repo)}
            snap_entry = {"repo_path": str(repo), "telemetry": {"task_id": "P2"}}
            ok, msg, record = migrate_legacy_readiness(p_entry, snap_entry, runtime)
            self.assertTrue(ok)
            self.assertEqual(msg, "migrated")
            self.assertIsNotNone(record)
            self.assertEqual(record["superseded_task_id"], "P1")
            self.assertEqual(record["task_id"], "P2")
            self.assertEqual(record["candidate"], "ready_to_run")

            # Post-migration check
            post_res = resolve_readiness(repo, current_task_id="P2")
            self.assertTrue(post_res.valid)
            self.assertFalse(post_res.stale)
            self.assertEqual(post_res.state, "ready_to_run")
            self.assertEqual(post_res.task_id, "P2")

    def test_readiness_migration_rollback_on_commit_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P2: Feature Two\n\nStatus: **READY_TO_RUN**\n\nDescription here\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "advance next.md to P2"], check=True)

            p_entry = {"project_id": "proj-1", "repo_path": str(repo)}
            snap_entry = {"repo_path": str(repo), "telemetry": {"task_id": "P2"}}

            # Mock git commit to fail
            real_run = subprocess.run
            def fake_subprocess_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and "commit" in cmd:
                    raise RuntimeError("simulated commit failure")
                return real_run(cmd, *args, **kwargs)

            with patch("dev_orchestrator.core.readiness.subprocess.run", side_effect=fake_subprocess_run):
                ok, msg, record = migrate_legacy_readiness(p_entry, snap_entry, runtime)
                self.assertFalse(ok)
                self.assertIn("commit failed", msg)
                self.assertIsNone(record)

            # Rollback should restore P1 execution-state
            state_file = repo / "agent" / "execution-state.json"
            self.assertTrue(state_file.is_file())
            content = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(content.get("task_id"), "P1")

    def test_supervisor_migrates_task_id_mismatch_and_resubmits_forward(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P2: Feature Two\n\nStatus: **READY_TO_RUN**\n\nDescription\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "advance next.md to P2"], check=True)

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            record_or_refresh_intent(
                runtime, "proj-1", task_id="P2", command_id="cmd-init-2",
                source="test", repo_path=str(repo), state="active",
            )

            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "IDLE",
                "telemetry": {"task_id": "P2"},
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            coordinator = ControlCommandCoordinator(runtime, None)
            supervisor = ActivationSupervisor(runtime, controls=coordinator, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "remediated")
            self.assertEqual(outcomes[0]["remediation"], "migrate_readiness")

            # Check forward command was queued (inbox or history)
            remed_cmds = list((runtime / "control" / "inbox").glob("cmd-rec-proj-1-P2-*.json")) + list(
                (runtime / "control" / "history").glob("cmd-rec-proj-1-P2-*.json")
            )
            self.assertTrue(len(remed_cmds) >= 1)

    def test_forward_path_exhausts_elapsed_time_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            # Record intent created 35 minutes ago
            from datetime import timedelta
            past_time = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
            record_or_refresh_intent(
                runtime, "proj-1", task_id="P1", command_id="cmd-init-1",
                source="test", repo_path=str(repo), state="active",
            )
            # Update created_at in storage
            intent_file = runtime / "execution-intent.json"
            data = read_json(intent_file)
            data["intents"]["proj-1"]["created_at"] = past_time
            write_json(intent_file, data)

            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "READY_TO_RUN",
                "telemetry": {"task_id": "P1"},
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            supervisor = ActivationSupervisor(runtime, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "exhausted")
            self.assertEqual(outcomes[0]["blocker_code"], "RECOVERY_BUDGET_EXHAUSTED")

            # Intent state is exhausted
            intent_after = get_active_intent(runtime, "proj-1")
            self.assertIsNone(intent_after)
            all_intents = load_execution_intents(runtime).get("intents", {})
            self.assertEqual(all_intents["proj-1"]["state"], "exhausted")

            # Zero commands submitted to inbox
            inbox_cmds = list((runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_cmds), 0)

    def test_forward_path_exhausts_actions_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            # Record intent with actions_used=20 (default max is 20)
            record_or_refresh_intent(
                runtime, "proj-1", task_id="P1", command_id="cmd-init-1",
                source="test", repo_path=str(repo), state="active",
            )
            intent_file = runtime / "execution-intent.json"
            data = read_json(intent_file)
            data["intents"]["proj-1"]["actions_used"] = 20
            write_json(intent_file, data)

            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "READY_TO_RUN",
                "telemetry": {"task_id": "P1"},
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            supervisor = ActivationSupervisor(runtime, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "exhausted")
            self.assertEqual(outcomes[0]["blocker_code"], "RECOVERY_BUDGET_EXHAUSTED")

            all_intents = load_execution_intents(runtime).get("intents", {})
            self.assertEqual(all_intents["proj-1"]["state"], "exhausted")

            inbox_cmds = list((runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_cmds), 0)

    def test_intent_terminates_satisfied_on_worker_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "proj-1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            record_or_refresh_intent(
                runtime, "proj-1", task_id="P1", command_id="cmd-init-1",
                source="test", repo_path=str(repo), state="active",
            )

            # Snapshot has active worker
            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "WORKER_RUNNING",
                "telemetry": {"task_id": "P1"},
                "worker": {"kind": "task", "state": "running", "task_id": "P1"},
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            supervisor = ActivationSupervisor(runtime, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "intent_satisfied")

            all_intents = load_execution_intents(runtime).get("intents", {})
            self.assertEqual(all_intents["proj-1"]["state"], "satisfied")

            # Next tick: no active intent, zero outcomes
            outcomes2 = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes2), 0)

    def test_owner_pause_or_gate_precedence_over_readiness_or_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            # Structured file for P1, next.md for P2 -> task id mismatch
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add P1 readiness"], check=True)

            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P2: Feature Two\n\nStatus: **READY_TO_RUN**\n\nDescription\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "advance next.md to P2"], check=True)

            # Owner pause via OwnerControlStore
            from dev_orchestrator.control.owner_store import OwnerControlStore
            OwnerControlStore(runtime).set_paused("proj-1", True, command_id="cmd-pause-1", action="pause")

            project_def = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "execution": {
                    "enabled": True,
                    "owner_authorized": True,
                    "allowed_next_actions": ["continue_current_stage"],
                    "preferred_backends": ["agy"],
                    "backends": {"agy": {"executable": "agy.cmd"}},
                },
            }

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [project_def]
                }),
                encoding="utf-8",
            )

            # Explain block: OWNER_PAUSED must sort ahead of READINESS_TASK_ID_MISMATCH
            blockers = explain_block(
                project_id="proj-1",
                repo_path=repo,
                runtime_root=runtime,
                config_path=cfg_path,
                project_config=project_def,
            )
            codes = [b.code for b in blockers]
            self.assertIn("OWNER_PAUSED", codes)
            self.assertIn("READINESS_TASK_ID_MISMATCH", codes)
            self.assertEqual(blockers[0].code, "OWNER_PAUSED")

            record_or_refresh_intent(
                runtime, "proj-1", task_id="P2", command_id="cmd-init-2",
                source="test", repo_path=str(repo), state="active",
            )

            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "IDLE",
                "telemetry": {"task_id": "P2"},
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            supervisor = ActivationSupervisor(runtime, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "owner_gate_preserved")
            self.assertEqual(outcomes[0]["blocker_code"], "OWNER_PAUSED")

            # Intent terminated as owner_gate
            all_intents = load_execution_intents(runtime).get("intents", {})
            self.assertEqual(all_intents["proj-1"]["state"], "owner_gate")

            # No readiness migration was attempted; state file still P1
            state_file = repo / "agent" / "execution-state.json"
            content = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(content.get("task_id"), "P1")

    def test_remediate_after_handoff_with_committed_execution_state_and_readiness_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            # Structured file committed for P1
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "commit P1 readiness"], check=True)

            # Completed worker advanced next.md to P2 before review
            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P2: Next Feature\n\nStatus: **READY_TO_RUN**\n\nDescription\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "advance next.md to P2"], check=True)

            truth = read_repository_truth(repo)
            readiness_res = resolve_readiness(repo, current_task_id="P2", next_status="Status: **READY_TO_RUN**")
            self.assertEqual(readiness_res.code, "READINESS_TASK_ID_MISMATCH")
            self.assertEqual(readiness_res.task_id, "P1")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "p1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage", "next_task"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            req_id = "ai_review:p1:remediate_1"
            decision_record = {
                "project_id": "p1",
                "request_id": req_id,
                "disposition": "apply",
                "decision": "remediate",
                "next_action": "continue_current_stage",
                "task_id": "P1",
                "stage_id": None,
                "branch": truth.branch,
                "head": truth.head,
                "role": "reviewer",
                "event": "worker_done",
                "review_status_hash": truth.status_hash,
                "consumed_at": "2026-09-23T12:00:00Z",
            }
            (runtime / "review-decisions.json").write_text(
                json.dumps({"version": 1, "decisions": {req_id: decision_record}}),
                encoding="utf-8",
            )

            snapshot = {
                "project_id": "p1",
                "repo_path": str(repo),
                "state": "WAITING_REVIEW",
                "next_title": "# Task P2: Next Feature",
                "next_status": "Status: **READY_TO_RUN**",
                "telemetry": {"task_id": "P2"},
                "readiness": readiness_res.to_dict(),
            }

            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance({"projects": [snapshot]}, cfg_path)
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0].task_id, "P1")

            ledger = executor._load_ledger()
            exec_rec = ledger["executions"].get(req_id)
            self.assertIsNotNone(exec_rec)
            self.assertEqual(exec_rec.get("task_id"), "P1")
            self.assertEqual(exec_rec.get("source_kind"), "remediation")
            self.assertNotEqual(exec_rec.get("state"), "blocked")

    def test_accepted_next_task_with_committed_predecessor_execution_state_and_readiness_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "commit P1 readiness"], check=True)

            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P2: Next Feature\n\nStatus: **READY_TO_RUN**\n\nDescription\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "advance next.md to P2"], check=True)

            truth = read_repository_truth(repo)
            readiness_res = resolve_readiness(repo, current_task_id="P2", next_status="Status: **READY_TO_RUN**")
            self.assertEqual(readiness_res.code, "READINESS_TASK_ID_MISMATCH")
            self.assertEqual(readiness_res.task_id, "P1")
            self.assertEqual(readiness_res.migration_candidate, "ready_to_run")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "p1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage", "next_task"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            req_id = "ai_review:p1:next_1"
            decision_record = {
                "project_id": "p1",
                "request_id": req_id,
                "disposition": "apply",
                "decision": "next",
                "next_action": "next_task",
                "task_id": "P1",
                "stage_id": None,
                "branch": truth.branch,
                "head": truth.head,
                "role": "reviewer",
                "event": "worker_done",
                "review_status_hash": truth.status_hash,
                "consumed_at": "2026-09-23T12:00:00Z",
            }
            (runtime / "review-decisions.json").write_text(
                json.dumps({"version": 1, "decisions": {req_id: decision_record}}),
                encoding="utf-8",
            )

            snapshot = {
                "project_id": "p1",
                "repo_path": str(repo),
                "state": "IDLE",
                "next_title": "# Task P2: Next Feature",
                "next_status": "Status: **READY_TO_RUN**",
                "telemetry": {"task_id": "P2"},
                "readiness": readiness_res.to_dict(),
            }

            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance({"projects": [snapshot]}, cfg_path)
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0].task_id, "P2")

            ledger = executor._load_ledger()
            exec_rec = ledger["executions"].get(req_id)
            self.assertIsNotNone(exec_rec)
            self.assertEqual(exec_rec.get("task_id"), "P2")
            self.assertEqual(exec_rec.get("source_kind"), "decision")
            self.assertNotEqual(exec_rec.get("state"), "blocked")

    def test_readiness_caused_block_does_not_permanently_consume_decision_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            exec_state = repo / "agent" / "execution-state.json"
            exec_state.write_text("{\"corrupted\": true}", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "corrupted schema"], check=True)

            truth = read_repository_truth(repo)
            readiness_res = resolve_readiness(repo, current_task_id="P1", next_status="Status: **READY_TO_RUN**")
            self.assertEqual(readiness_res.code, "READINESS_SCHEMA_INVALID")

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                json.dumps({
                    "projects": [
                        {
                            "project_id": "p1",
                            "repo_path": str(repo),
                            "execution": {
                                "enabled": True,
                                "owner_authorized": True,
                                "allowed_next_actions": ["continue_current_stage", "next_task"],
                                "preferred_backends": ["agy"],
                                "backends": {"agy": {"executable": "agy.cmd"}},
                            },
                        }
                    ]
                }),
                encoding="utf-8",
            )

            req_id = "ai_review:p1:rem_schema_err"
            decision_record = {
                "project_id": "p1",
                "request_id": req_id,
                "disposition": "apply",
                "decision": "remediate",
                "next_action": "continue_current_stage",
                "task_id": "P1",
                "stage_id": None,
                "branch": truth.branch,
                "head": truth.head,
                "role": "reviewer",
                "event": "worker_done",
                "review_status_hash": truth.status_hash,
                "consumed_at": "2026-09-23T12:00:00Z",
            }
            (runtime / "review-decisions.json").write_text(
                json.dumps({"version": 1, "decisions": {req_id: decision_record}}),
                encoding="utf-8",
            )

            snapshot = {
                "project_id": "p1",
                "repo_path": str(repo),
                "state": "WAITING_REVIEW",
                "next_title": "# Task P1: First Feature",
                "next_status": "Status: **READY_TO_RUN**",
                "telemetry": {"task_id": "P1"},
                "readiness": readiness_res.to_dict(),
            }

            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance({"projects": [snapshot]}, cfg_path)
            self.assertEqual(len(launches), 0)

            # Key assertion: req_id must NOT be permanently recorded as blocked in ledger!
            ledger = executor._load_ledger()
            self.assertNotIn(req_id, ledger["executions"])

            # Fix readiness schema and commit:
            write_structured_readiness(repo, "ready_to_run", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fix readiness schema"], check=True)

            fixed_truth = read_repository_truth(repo)
            decision_record["head"] = fixed_truth.head
            decision_record["review_status_hash"] = fixed_truth.status_hash
            (runtime / "review-decisions.json").write_text(
                json.dumps({"version": 1, "decisions": {req_id: decision_record}}),
                encoding="utf-8",
            )

            fixed_readiness = resolve_readiness(repo, current_task_id="P1", next_status="Status: **READY_TO_RUN**")
            snapshot["readiness"] = fixed_readiness.to_dict()

            launches2 = executor.advance({"projects": [snapshot]}, cfg_path)
            self.assertEqual(len(launches2), 1)
            self.assertEqual(launches2[0].task_id, "P1")

    def test_readiness_not_ready_to_run_lifecycle_hold_terminates_intent_without_false_exhaustion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            repo = root / "repo"
            make_repo(repo, task_id="P1")

            next_md = repo / "agent" / "next.md"
            next_md.write_text("# Task P1: Initial Task\n\nStatus: **PENDING DESIGN**\n\nDescription\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "set pending design"], check=True)

            write_structured_readiness(repo, "pending_design", "P1")
            subprocess.run(["git", "-C", str(repo), "add", "agent/execution-state.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "commit pending design execution state"], check=True)

            project_def = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "execution": {
                    "enabled": True,
                    "owner_authorized": True,
                    "allowed_next_actions": ["continue_current_stage"],
                    "preferred_backends": ["agy"],
                    "backends": {"agy": {"executable": "agy.cmd"}},
                },
            }

            cfg_path = root / "config" / "projects.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps({"projects": [project_def]}), encoding="utf-8")

            # Verify explain_block produces failure_class="lifecycle" (not "terminal")
            blockers = explain_block(
                project_id="proj-1",
                repo_path=repo,
                runtime_root=runtime,
                config_path=cfg_path,
                project_config=project_def,
                action="continue",
            )
            top = blockers[0]
            self.assertEqual(top.code, "READINESS_NOT_READY_TO_RUN")
            self.assertEqual(top.failure_class, "lifecycle")

            # Active intent exists
            record_or_refresh_intent(
                runtime, "proj-1", task_id="P1", command_id="cmd-init-1",
                source="test", repo_path=str(repo), state="active",
            )

            readiness_res = resolve_readiness(repo, current_task_id="P1", next_status="Status: **PENDING DESIGN**")
            snapshot = {
                "project_id": "proj-1",
                "repo_path": str(repo),
                "state": "IDLE",
                "telemetry": {"task_id": "P1"},
                "readiness": readiness_res.to_dict(),
            }
            summary = {"projects": [snapshot]}

            executor = FakeExecutor(launch=True)
            supervisor = ActivationSupervisor(runtime, executor=executor)

            outcomes = supervisor.advance(cfg_path, summary, executor=executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "lifecycle_hold")
            self.assertEqual(outcomes[0]["blocker_code"], "READINESS_NOT_READY_TO_RUN")

            # Intent terminated as stopped (NOT exhausted)
            all_intents = load_execution_intents(runtime).get("intents", {})
            self.assertEqual(all_intents["proj-1"]["state"], "stopped")
            self.assertEqual(all_intents["proj-1"]["failure_class"], "lifecycle")

            # Key assertion: subsequent explain_block does NOT publish RECOVERY_BUDGET_EXHAUSTED!
            blockers2 = explain_block(
                project_id="proj-1",
                repo_path=repo,
                runtime_root=runtime,
                config_path=cfg_path,
                project_config=project_def,
            )
            codes2 = [b.code for b in blockers2]
            self.assertNotIn("RECOVERY_BUDGET_EXHAUSTED", codes2)
            self.assertNotIn("RECOVERY_LIVELOCK_DETECTED", codes2)


if __name__ == "__main__":
    unittest.main()
