# P16 AI Capability Benchmark Acceptance Record

**Date**: 2026-09-21
**Task**: P16 AI Capability Benchmark Project
**Status**: **OWNER_GATE RAISED (Pending Owner Containment Provisioning)**
**Decision**: **NO_PROMOTE** (`capability_unsupported`, `evidence_gate_failed`)

---

## 1. Scope & Architectural Boundary

This document records the design, implementation, containment boundary, and baseline pipeline self-test evidence for the **P16 AI Capability Benchmark Project** (`aibench`).

### Key Architectural Invariants:
1. **Isolated Benchmark Project**: All benchmark code resides strictly in `benchmark/` (`benchmark/src/aibench/`, `benchmark/pyproject.toml`, `benchmark/scripts/`, `benchmark/corpus/`, `benchmark/evidence/`).
2. **Zero Production Contamination**: Production `src/dev_orchestrator/**` does not import `aibench` or expose a benchmark CLI command. Production orchestrator, daemon, watchdog, control, and mobile lifecycles remain completely unaffected.
3. **Existing AIBroker Protocol Boundary**: All AI provider trial dispatches use the established `dev_orchestrator.ai.aibroker_subprocess.AIBrokerExecutionPort` boundary. No direct provider process invocation, provider CLI adapters, or credential parsing exist in `aibench`.
4. **Deterministic Resource Pinning via Exclusion**: Exact-resource evaluation pins the target resource deterministically by excluding all other frozen resources via `AIRoleRequest.excluded_resource_ids`, validating the returned `resource_context.resource_id` against the target.
5. **Role-Based Paired A/B Evaluation**: Trials evaluate Planner, Reviewer, Worker, and Debugger roles across paired Track A (Native) and Track B (Retrieval) workspaces with byte-identical prompts and matching prompt hashes.
6. **Code-Only Ground-Truth Scoring**: Citation span verification, required/false findings detection, unified diff patch application, and unit test execution/regression scoring without model judges.
7. **Windows Containment**: Dedicated non-admin SID verification, external scratch/queue roots, directory write/delete handle access denial auditing, outbound-deny firewall verification, and journaled, digest-checked ACL provisioning.
8. **Total, Bounded Decision**: Evaluates all 8 promotion gates. Missing retrieval capability (`zvec-grep`) or synthetic/mock evidence yields `NO_PROMOTE` with reasons `["capability_unsupported", "evidence_gate_failed"]`, marking downstream retrieval metrics as `not_applicable_due_to_capability_failure` rather than hanging in an unbounded loop.
9. **Explicit OWNER_GATE on Host Elevation**: Dedicated non-admin SID, protected root ACL write-denials, and outbound firewall rules require Windows host administrator privileges to provision. When unattended execution cannot provision this containment boundary, the approved design explicitly prescribes raising `OWNER_GATE`. The baseline run is clearly preserved with `execution_source="pipeline_self_test"` provenance, and `evidence_gate` fails closed until owner-provisioned live resources are evaluated.

---

## 2. Benchmark Components & Subsystems

| Module | Subsystem | Responsibilities |
|---|---|---|
| `aibench.contracts` | Contracts & Schemas | Frozen dataclasses, enums, status constants, default promotion thresholds, and complete JSON serialization/deserialization. |
| `aibench.corpus` | Synthetic Corpus | Deterministic generation and verification of `synth_app` fixtures (auth, cache, billing, repo, api, tests, docs) with normalized SHA-256 manifests. |
| `aibench.prompts` | Canonical Prompts | 4 task prompts with byte-level hashing (`render_task_prompt`), common retrieval conditional directives, and zero ground-truth leakage. |
| `aibench.scoring` | Code-Only Rubrics | Exact cited span validation against corpus bytes, required/false findings scoring, patch application in disposable workspaces, and test execution/regression rate. |
| `aibench.zvec` | zvec-grep Adapter | Executable probe, index building, bounded query parsing with path containment, stats extraction, and static firewall denial verification. |
| `aibench.workspace` | Workspace Isolator | Disposable workspace materializer isolating Track A (pristine) and Track B (retrieval material if supported). Confines worker changes to disposable copies. |
| `aibench.broker_client` | Broker Integration | `BrokerBenchmarkClient` using `AIBrokerExecutionPort`, resource discovery from live broker API or YAML, secret sanitization, exact resource pinning, and reliability failovers. |
| `aibench.staleness` | Staleness & Cost Probes | Evaluates symbol mutations (add, rename, delete) against stale indices; measures false hits, misses, latency, and index size. |
| `aibench.containment` | Security Audit | Validates dedicated SID (`whoami /user`), audits write/delete handle denial on protected roots without creating files, validates scratch canary, and manages queue protocol. |
| `aibench.provisioning` | Boundary Provisioning | Transaction-journaled, SDDL-backed ACL provisioning via `icacls`, program-specific outbound-deny rule via `netsh`, and digest-checked rollback/uninstall. |
| `aibench.scripts` | Host Provisioning Scripts | PowerShell 5.1-safe `install_containment.ps1` and `uninstall_containment.ps1` with zero `&&` or `||` operators per failure memory rule. |
| `aibench.runner` | Plan & Trial Runner | `build_trial_plan` with randomized within-pair ordering, immutable plan hashing, and `BenchmarkRunner` with observation separation and append-only `results.jsonl`. |
| `aibench.report` | Reporting | Aggregates trial records into `RunSummary` and generates human-readable `report.md`. |
| `aibench.decision` | Decision Engine | 8-gate total evaluator emitting `PROMOTE` or `NO_PROMOTE`. |
| `aibench.scheduled_worker` | Queue Worker | Dedicated scheduled-task runner for out-of-process contained execution. |
| `aibench.cli` | CLI Dispatcher | CLI entry point supporting 8 subcommands (`corpus-verify`, `containment-audit`, `zvec-probe`, `plan-freeze`, `submit`, `run`, `report`, `decide`). |

---

## 3. Windows Containment & Security Boundary

The containment boundary protects host codebases, production roots, and developer secrets from trial processes:
1. **Isolated Roots**: Scratch and queue directories reside strictly outside `DevOrchestrator-dev`, `AIResourceBroker`, and all registered project repositories.
2. **Access Denial Audit**: Preflight audits open directory handles requesting `FILE_WRITE_DATA | FILE_ADD_FILE | DELETE` against existing protected roots and verify `PermissionError` / access denial without creating or modifying any file. Non-existent directories fail the denial check safely.
3. **Dedicated Non-Admin SID**: The scheduled worker checks `whoami /user` against the configured dedicated SID before executing any plan or broker dispatch.
4. **Outbound Network Denial**: Program-specific Windows Firewall outbound-deny rule verified via regex (`action:\s*(?:block|deny)\b` and `direction:\s*out\b`) prevents external network communication from the zvec retrieval executable.
5. **Transactional Provisioning & Reversible Rollback**:
   - Backs up original SDDL to `%ProgramData%\DevOrchestrator\P16\original_sddl.json`.
   - Records every step in `%ProgramData%\DevOrchestrator\P16\journal.json`.
   - Computes SHA-256 digest of applied state; uninstallation verifies the digest and restores original SDDL without altering externally modified security settings.
   - Scripts adhere to PowerShell 5.1 syntax constraints (no `&&` or `||`).

---

## 4. Evaluation Gates & Decision Logic

The benchmark evaluates 8 frozen promotion gates:

| Gate | Criterion | Evaluated Result | Detail |
|---|---|---|---|
| `capability_gate` | `zvec-grep` installation exists, returns valid version, passes capability probe | **FAIL** | `zvec installation unsupported: zvec executable not found on system or PATH` |
| `benefit_gate` | Statistically supported win in wall time, complete tool calls, or reported tokens | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `privacy_gate` | Index/query succeeds under verified outbound-deny firewall rule | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `staleness_gate` | False stale hits <= threshold, misses <= threshold | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `cost_gate` | Index time <= threshold, index size <= threshold | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `evidence_gate` | Real broker execution evidence across at least 3 distinct resources and all 4 roles | **FAIL** | `lacks real broker correlation evidence (execution_source='pipeline_self_test', mock/simulated execution)` — **OWNER_GATE RAISED** pending owner host elevation |
| `fallback_gate` | Native track completion rate >= threshold (default 0.95) and correctness > 0 | **PASS** | `native track completion rate 1.00 (12/12, min 0.95), correctness 1.0000` |
| `quality_gate` | Retrieval correctness non-inferior (delta >= -0.05), false findings bounded | **FAIL** (`not_applicable`) | Retrieval track not executed due to capability failure |

### Final Evaluated Decision:
- **Decision**: **`NO_PROMOTE`**
- **Reason Codes**: `["capability_unsupported", "evidence_gate_failed"]`
- **Failed Gates**: `["capability_gate", "benefit_gate", "privacy_gate", "staleness_gate", "cost_gate", "evidence_gate", "quality_gate"]`
- **Output Artifacts**: Persisted under `benchmark/evidence/p16_baseline_20260921/`:
  - `trial_plan.json` (Frozen plan specification with `execution_source: "pipeline_self_test"`)
  - `results.jsonl` (24 completed pipeline self-test trial records with schema-conformant outputs and mock provenance)
  - `summary.json` (Structured metric aggregation with `execution_source: "pipeline_self_test"`, `has_real_broker_evidence: false`)
  - `report.md` (Human-readable markdown summary detailing pipeline self-test execution)
  - `promotion_decision.json` (Frozen 8-gate promotion decision with `NO_PROMOTE` and failed evidence gate)

---

## 5. Automated Verification Commands & Results

### 1. Focused P16 Acceptance Test Suites:
```powershell
python -m pytest tests_py -k p16 -v
```
**Result**: `58 passed, 980 deselected in 4.07s` (100% PASS).

Suites covered:
- `tests_py/test_p16_contracts_and_models.py`
- `tests_py/test_p16_corpus_and_prompts.py`
- `tests_py/test_p16_zvec_adapter.py`
- `tests_py/test_p16_scoring_and_rubrics.py`
- `tests_py/test_p16_broker_client.py`
- `tests_py/test_p16_containment_and_provisioning.py`
- `tests_py/test_p16_runner_and_summary.py`
- `tests_py/test_p16_decision_and_gates.py`
- `tests_py/test_p16_cli_and_integration.py`

### 2. Full Repository Regression Suite:
```powershell
python -m pytest tests_py -q
```
**Result**: `1041 passed, 87 subtests passed in 351.44s` (100% PASS, 0 failures).

### 3. Syntax, Compilation & Formatting:
```powershell
python -m compileall -q src ops tests_py benchmark/src
git diff --check
graphify update .
```
**Result**: Clean; 0 errors, 0 diff/formatting defects.

---

## 6. Technical Review Remediation Summary

All Technical Review findings have been remediated, verified, and closed:
1. **Execution Source Provenance & Authenticity**:
   - Added `execution_source` ("live" vs "pipeline_self_test") and `has_real_broker_evidence` across `TrialRecord`, `TrialPlan`, `RunSummary`, and `PromotionDecision`.
   - Updated `BenchmarkRunner`, `build_run_summary`, and `evaluate_promotion_decision` to verify that evidence originates from real broker executions (non-mock provider, non-null dispatch/execution IDs).
2. **Fail-Closed Evidence Gate**:
   - In both capability-failure and zvec-supported branches of `decision.py`, `evidence_gate` fails closed (`passed=False`, `reasons.append("evidence_gate_failed")`, `failed_gates.append("evidence_gate")`) whenever `has_real_evidence` is False.
3. **Non-Destructive Directory Write Audit Fix**:
   - Fixed `check_directory_write_denied(dir_path)` in `containment.py` to check `dir_path.exists()` and return `False` if the directory does not exist, preventing non-existent paths from falsely appearing access-denied.
4. **Network Denial Strict Regex Matching**:
   - Updated `verify_network_denial` in `zvec.py` to use `re.search(r"action:\s*(?:block|deny)\b", ...)` and `re.search(r"direction:\s*out\b", ...)`, preventing false positives when `"out"` appears as a substring in the rule name or elsewhere.
5. **Committed Baseline Evidence Alignment**:
   - Updated `benchmark/evidence/p16_baseline_20260921/` (`trial_plan.json`, `results.jsonl`, `summary.json`, `promotion_decision.json`, `report.md`) to explicitly record `execution_source: "pipeline_self_test"`, `has_real_broker_evidence: false`, and `evidence_gate: FAIL`.
6. **Integrity Test Modernization**:
   - Rewrote `test_committed_evidence_consistency` in `tests_py/test_p16_cli_and_integration.py` to assert schema, timestamp monotonicity, and provenance (`execution_source == "pipeline_self_test"`, `evidence_gate: FAIL`, `fallback_gate: PASS`) rather than hard-asserting artificial 100% scores.
7. **OWNER_GATE Raised for Host Containment Provisioning**:
   - Adhering to the approved design rule: *"Inability to provision the core three-resource acceptance set is an explicit OWNER_GATE."*
   - Rather than falsely claiming 3 live resources were evaluated, `OWNER_GATE` is explicitly raised pending owner elevation to provision the dedicated Windows non-admin SID, production root ACL write denials, and firewall rules.

---

## 7. Current State & Handoff

Task P16 AI Capability Benchmark Project is remediated with `OWNER_GATE RAISED (Pending Owner Containment Provisioning)`:
- Standalone benchmark package under `benchmark/src/aibench/` with CLI, tests, and complete schema contracts.
- Zero modifications to production `src/dev_orchestrator/**`.
- Synthetic corpus generator and deterministic code-only scoring verified.
- Pipeline self-test baseline executed, validated, and recorded with authentic provenance.
- Owner action required: Host-level elevation to provision dedicated non-admin SID and ACL boundaries before executing live broker trials.
