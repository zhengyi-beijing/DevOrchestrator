# P17 Historical Incident Corpus & Replay Mapping

Status: **FROZEN / ACCEPTED (PHASE W)**  
Task: P17 Single-Authority Goal Convergence Baseline  

Maps real production incidents from P12 through P16.14 to their reproduction fixtures,
expected v0 convergence decisions, invariant verdicts, and regression coverage.

---

## 1. Summary Metrics

- **Total Historical Replay Classes**: 26
- **Corpus Directory**: `tests_py/data/p17_corpus/`
- **Replay Harness**: `dev_orchestrator.convergence.replay.ReplayHarness`
- **Automated Test Suite**: `tests_py/test_p17_replay_corpus.py` and `tests_py/test_p17_replay_determinism.py`
- **Corpus Pass Rate**: 26 / 26 (100%)
- **Aggregate Duplicate Executions**: 0
- **Trace Hash Determinism**: Verified bit-for-bit across clean runs and crash-injection replays.

---

## 2. Full Incident Replay Matrix (Classes 1–26)

| ID | Incident Class Name | CPF Scenario | Legacy Production Result | Expected v0 Decision | Invariant Verdict | Covering Regression Test |
| :---: | :--- | :---: | :--- | :--- | :--- | :--- |
| **01** | `p1614_remediation_budget_deadlock` | `CPF-08` | Remediation budget exhausted; daemon stalled awaiting manual continue. | `WRITE_HANDOFF` | `COMPLETE_EXHAUSTION: False`<br>`NO_HUMAN_CLOCK: True` | `tests_py/test_p17_cpf_scenarios_part2.py::test_cpf_08_missing_declaration_and_bounded_problem_fails_closed` |
| **02** | `lost_successor_handoff` | `CPF-02`<br>`CPF-07` | Accepted NEXT review decision lost handoff; watchdog required to rebuild. | `PUBLISH_SUCCESSOR` | `PROGRESS_TOTALITY: True`<br>`SUCCESSOR_DETERMINISM: True` | `tests_py/test_p17_cpf_scenarios_part1.py::test_cpf_02_zero_touch_lost_handoff_recovers` |
| **03** | `markdown_complete_without_verification` | `CPF-04` | Markdown status manually edited to COMPLETE without verified acceptance. | `EXECUTE` | `ACCEPTANCE_BEFORE_ADVANCE: True`<br>`MARKDOWN_NON_AUTHORITY: True` | `tests_py/test_p17_acceptance_model.py::test_acceptance_none_cannot_satisfy_goal` |
| **04** | `stale_reviewer_anchor_after_head_change` | `CPF-01` | Review performed at old commit HEAD while working tree had moved forward. | `VERIFY` | `ANCHOR_BINDING: True`<br>`ACCEPTANCE_BEFORE_ADVANCE: True` | `tests_py/test_p17_acceptance_model.py::test_verified_acceptance_with_unresolved_blocking_finding_fails` |
| **05** | `replayed_duplicate_command_or_event` | `CPF-06`<br>`CPF-10` | Same worker completion re-delivered; caused duplicate actuation in legacy. | `NOOP_ACTIVE` | `IDEMPOTENT_REPLAY: True`<br>`SINGLE_ACTIVE_LEASE: True` | `tests_py/test_p17_cpf_scenarios_part2.py::test_cpf_06_duplicate_events_idempotent` |
| **06** | `worker_process_death_stale_active_ownership` | `CPF-01` | Worker died; ledger retained active running state and blocked progress. | `RETRY_NEW_STRATEGY` | `SINGLE_ACTIVE_LEASE: True`<br>`BOUNDED_PROBLEM: True` | `tests_py/test_p17_cpf_scenarios_part1.py::test_cpf_01_stale_predecessor_worker_fenced` |
| **07** | `provider_quota_timeout_with_alternatives` | `CPF-08` | AIBroker provider quota exceeded; wasted reasoning attempts instead of failover. | `FAILOVER_RESOURCE` | `BOUNDED_PROBLEM: True`<br>`PROGRESS_TOTALITY: True` | `tests_py/test_p17_retry_wait_escalation.py::test_transient_resource_fails_over_at_same_tier_first` |
| **08** | `owner_gate_identity_mismatch_discharge` | `CPF-08`<br>`CPF-09` | Owner command rejected because background commit changed task revision. | `EXECUTE` | `HUMAN_REQUEST_DISCHARGEABLE: True`<br>`SINGLE_AUTHORITY: True` | `tests_py/test_p17_human_and_emergency.py::test_human_request_creation_and_cas_answer` |
| **09** | `emergency_pause_requested_after_head_change` | `CPF-01` | Emergency pause refused because lifecycle CAS token changed. | `WAIT_UNTIL` | `EMERGENCY_BRAKE: True`<br>`SINGLE_AUTHORITY: True` | `tests_py/test_p17_human_and_emergency.py::test_emergency_pause_overrides_all_decisions` |
| **10** | `multi_owner_contradictory_authority_ambiguity` | `CPF-04` | Conflicting authority records; legacy picked arbitrarily and risked data loss. | `REQUEST_HUMAN` | `FAIL_CLOSED_AMBIGUITY: False`<br>`HUMAN_TYPED: True` | `tests_py/test_p17_cpf_scenarios_part1.py::test_cpf_04_contradictory_authority_fails_closed` |
| **11** | `transient_git_read_failure` | `CPF-08` | Transient git index.lock caused false owner gating. | `FAILOVER_RESOURCE` | `BOUNDED_PROBLEM: True`<br>`PROGRESS_TOTALITY: True` | `tests_py/test_p17_retry_wait_escalation.py::test_transient_resource_fails_over_at_same_tier_first` |
| **12** | `known_powershell_incompatibility_after_rule_learned` | `CPF-08` | PowerShell 5.1 pipeline chaining failed repeatedly despite known lesson. | `RETRY_NEW_STRATEGY` | `LEARNED_CONSTRAINT_CONSUMPTION: True`<br>`LEARNING_REGRESSION: True` | `tests_py/test_p17_preflight_constraints.py::test_recurrence_classified_as_learning_regression` |
| **13** | `dirty_worktree_at_transition_boundary` | `CPF-05` | Dirty worktree blocked transition; stalled without explicit waiting status. | `WAIT_UNTIL` | `WAIT_IS_NOT_PROGRESS: True`<br>`ANCHOR_BINDING: True` | `tests_py/test_p17_cpf_scenarios_part1.py::test_cpf_05_dirty_worktree_defers_without_mutation` |
| **14** | `reviewer_unable_to_run_tests_with_claimed_counts` | `CPF-04` | Reviewer could not run tests; prose counts accepted as fake evidence. | `VERIFY` | `ACCEPTANCE_BEFORE_ADVANCE: True`<br>`ANCHOR_BINDING: True` | `tests_py/test_p17_acceptance_model.py::test_verified_acceptance_with_unresolved_blocking_finding_fails` |
| **15** | `runtime_config_split_code_location` | `CPF-04` | Daemon launched from different worktree created parallel runtime root. | `REQUEST_HUMAN` | `FAIL_CLOSED_AMBIGUITY: False`<br>`HUMAN_TYPED: True` | `tests_py/test_p17_contract_amendments.py::test_isolated_fixture_runtime_root_fail_closed_and_ownership_lock` |
| **16** | `malformed_untyped_reviewer_finding_prose` | `CPF-08` | Review parsed prose "BLOCKING:" substring, missed unstructured issues. | `RETRY_SAME_STRATEGY` | `BOUNDED_PROBLEM: True`<br>`PROGRESS_TOTALITY: True` | `tests_py/test_p17_findings_and_output_invalid.py::test_malformed_findings_raise_output_invalid` |
| **17** | `exhausted_problem_requiring_complete_handoff` | `CPF-08` | Stalled at diagnosis_unknown without structured handoff. | `WRITE_HANDOFF` | `COMPLETE_EXHAUSTION: False`<br>`BOUNDED_PROBLEM: True` | `tests_py/test_p17_retry_wait_escalation.py::test_total_exhaustion_writes_handoff` |
| **18** | `normal_happy_path_goal_completion` | `CPF-02` | Normal happy path completion and successor publication. | `PUBLISH_SUCCESSOR` | `ACCEPTANCE_BEFORE_ADVANCE: True`<br>`SUCCESSOR_DETERMINISM: True` | `tests_py/test_p17_replay_corpus.py::test_all_26_cases_pass_replay` |
| **19** | `malformed_partial_role_output_invalid` | `CPF-08` | Partially valid JSON findings treated as empty findings, falsely accepted. | `RETRY_SAME_STRATEGY` | `BOUNDED_PROBLEM: True`<br>`PROGRESS_TOTALITY: True` | `tests_py/test_p17_findings_and_output_invalid.py::test_output_invalid_triggers_bounded_schema_repair` |
| **20** | `multiple_simultaneous_blocking_findings_same_family` | `CPF-08` | Multiple blocking findings broke single-finding assumptions. | `RETRY_NEW_STRATEGY` | `BOUNDED_PROBLEM: True`<br>`PROGRESS_TOTALITY: True` | `tests_py/test_p17_problem_identity.py::test_deterministic_next_problem_selection` |
| **21** | `restart_during_broker_effect` | `CPF-03`<br>`CPF-06` | Daemon restart lost in-flight broker request; orphaned or duplicated. | `NOOP_ACTIVE` | `SINGLE_ACTIVE_LEASE: True`<br>`IDEMPOTENT_REPLAY: True` | `tests_py/test_p17_lease_and_effects.py::test_reconcile_live_lease_retains_active_lease` |
| **22** | `stable_problem_budget_across_fix_commits_and_failover` | `CPF-08` | Ordinary commit or restart reset problem retry budget to zero. | `ESCALATE_CAPABILITY` | `STABLE_PROBLEM_IDENTITY: True`<br>`BOUNDED_PROBLEM: True` | `tests_py/test_p17_retry_wait_escalation.py::test_strategy_exhaustion_escalates_capability` |
| **23** | `timed_quota_reset_produces_wait_until` | `CPF-08` | Provider rate limit caused tight spin-retry loop instead of timed wait. | `WAIT_UNTIL` | `WAIT_IS_NOT_PROGRESS: True`<br>`BOUNDED_PROBLEM: True` | `tests_py/test_p17_retry_wait_escalation.py::test_known_quota_reset_produces_budget_free_wait` |
| **24** | `human_question_discharged_across_head_change` | `CPF-09` | Human answer rejected because background commit changed HEAD. | `EXECUTE` | `HUMAN_REQUEST_DISCHARGEABLE: True`<br>`PROGRESS_TOTALITY: True` | `tests_py/test_p17_human_and_emergency.py::test_human_request_creation_and_cas_answer` |
| **25** | `shared_credential_mutual_exclusion` | `CPF-06` | Two concurrent sessions contended on one OAuth refresh token. | `NOOP_ACTIVE` | `SINGLE_ACTIVE_LEASE: True`<br>`IDEMPOTENT_REPLAY: True` | `tests_py/test_p17_evidence_and_policy.py::test_shared_credential_lease` |
| **26** | `crash_injection_between_durable_writes` | `CPF-03`<br>`CPF-07` | Crash between status write and handoff write left inconsistent state. | `PUBLISH_SUCCESSOR` | `IDEMPOTENT_REPLAY: True`<br>`SUCCESSOR_DETERMINISM: True` | `tests_py/test_p17_replay_determinism.py::test_crash_injection_between_durable_writes_converges` |

---

## 3. P16.14 to P17 Bootstrap Exception Note
The P16.14 -> P17 legacy handoff had `acceptance=null/NONE` due to historical reviewer infrastructure exhaustion. In P17, this edge is recorded strictly as a historical `ACCEPTANCE_BEFORE_ADVANCE` violation discharged by `agent/evidence/P17_BOOTSTRAP_OWNER_OVERRIDE.json`, not as permission for markdown-only closure.
