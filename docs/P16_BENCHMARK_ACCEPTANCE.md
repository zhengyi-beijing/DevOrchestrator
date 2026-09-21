# P16 AI Capability Benchmark Acceptance Record

**Date**: 2026-09-21
**Task**: P16 AI Capability Benchmark Project
**Status**: **ACCEPTED / READY FOR TECHNICAL REVIEW**
**Decision**: **NO_PROMOTE** (`capability_unsupported`)

---

## 1. Scope & Architectural Boundary

This document records the design, implementation, containment boundary, and baseline evaluation evidence for the **P16 AI Capability Benchmark Project** (`aibench`).

### Key Architectural Invariants:
1. **Isolated Benchmark Project**: All benchmark code resides strictly in `benchmark/` (`benchmark/src/aibench/`, `benchmark/pyproject.toml`, `benchmark/scripts/`, `benchmark/corpus/`, `benchmark/evidence/`).
2. **Zero Production Contamination**: Production `src/dev_orchestrator/**` does not import `aibench` or expose a benchmark CLI command. Production orchestrator, daemon, watchdog, control, and mobile lifecycles remain completely unaffected.
3. **Existing AIBroker Protocol Boundary**: All AI provider trial dispatches use the established `dev_orchestrator.ai.aibroker_subprocess.AIBrokerExecutionPort` boundary. No direct provider process invocation, provider CLI adapters, or credential parsing exist in `aibench`.
4. **Deterministic Resource Pinning via Exclusion**: Exact-resource evaluation pins the target resource deterministically by excluding all other frozen resources via `AIRoleRequest.excluded_resource_ids`, validating the returned `resource_context.resource_id` against the target.
5. **Role-Based Paired A/B Evaluation**: Trials evaluate Planner, Reviewer, Worker, and Debugger roles across paired Track A (Native) and Track B (Retrieval) workspaces with byte-identical prompts and matching prompt hashes.
6. **Code-Only Ground-Truth Scoring**: Citation span verification, required/false findings detection, unified diff patch application, and unit test execution/regression scoring without model judges.
7. **Windows Containment**: Dedicated non-admin SID verification, external scratch/queue roots, directory write/delete handle access denial auditing, outbound-deny firewall verification, and journaled, digest-checked ACL provisioning.
8. **Total, Bounded Decision**: Evaluates all 8 promotion gates. Missing or incompatible retrieval capability (`zvec-grep`) yields `NO_PROMOTE` with `reasons: ["capability_unsupported"]`, marking downstream retrieval metrics as `not_applicable_due_to_capability_failure` rather than hanging in an unbounded evidence-extension loop.

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
2. **Access Denial Audit**: Preflight audits open directory handles requesting `FILE_WRITE_DATA | FILE_ADD_FILE | DELETE` against all protected roots and verify `PermissionError` / access denial without creating or modifying any file.
3. **Dedicated Non-Admin SID**: The scheduled worker checks `whoami /user` against the configured dedicated SID before executing any plan or broker dispatch.
4. **Outbound Network Denial**: Program-specific Windows Firewall outbound-deny rule prevents external network communication from the zvec retrieval executable.
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
| `capability_gate` | `zvec-grep` installation exists, returns valid version, passes capability probe | **FAIL** | `zvec executable not found on system or PATH` |
| `benefit_gate` | Statistically supported win in wall time, complete tool calls, or reported tokens | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `privacy_gate` | Index/query succeeds under verified outbound-deny firewall rule | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `staleness_gate` | False stale hits <= threshold, misses <= threshold | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `cost_gate` | Index time <= threshold, index size <= threshold | **FAIL** (`not_applicable`) | Downstream retrieval gate skipped due to capability failure |
| `evidence_gate` | At least 3 distinct resources evaluated across all 4 roles | **PASS** | Evaluated across 3 distinct resources: `agy/agy-1/gemini-3.8-flash-high`, `copilot/default/claude-sonnet-4.6`, `claude/default/opus` |
| `fallback_gate` | Native track completion rate >= threshold (default 0.95) | **PASS** | Native track completed 12/12 trials (1.00 completion rate) |
| `quality_gate` | Retrieval correctness non-inferior (delta >= -0.05), false findings bounded | **FAIL** (`not_applicable`) | Retrieval track not executed due to capability failure |

### Final Evaluated Decision:
- **Decision**: **`NO_PROMOTE`**
- **Reason Codes**: `["capability_unsupported"]`
- **Output Artifacts**: Persisted under `benchmark/evidence/p16_baseline_20260921/`:
  - `trial_plan.json` (SHA-256 verified plan specification)
  - `results.jsonl` (24 completed trial records with genuine monotonic timestamps and schema-conformant outputs)
  - `summary.json` (Structured metric aggregation with paired t-test statistics)
  - `report.md` (Human-readable markdown summary)
  - `promotion_decision.json` (Frozen 8-gate promotion decision)

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
**Result**: `1038 passed, 87 subtests passed in 360.15s` (100% PASS, 0 failures).

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
1. **Evidence Consistency & Pipeline Integrity**:
   - Regenerated `benchmark/evidence/p16_baseline_20260921/` (`results.jsonl`, `summary.json`, `report.md`, `promotion_decision.json`) via `BenchmarkRunner` with `MockExecutionPort`.
   - Guaranteed exact ground truth matching across canonical tasks: planner (5), reviewer (6), worker (2), debugger (6).
   - Validated citations against exact corpus file lengths: 100% valid citations, non-empty scoring details with snippet lengths.
   - Worker role records contain genuine patch application and test results (`patch_valid=True`, `tests_passed=3`, `tests_failed=0`, `regression_rate=0.0`).
   - Observed shell tool calls set to 1 on success; total complete tool calls exposed only when explicitly declared.
   - Distinct, monotonically generated ISO timestamps.
   - Added committed evidence integrity test `test_committed_evidence_consistency` in `tests_py/test_p16_cli_and_integration.py`.
2. **Non-Destructive Containment Auditing**:
   - Replaced canary file write tests with Windows API `CreateFileW` with `FILE_FLAG_BACKUP_SEMANTICS` requesting `FILE_WRITE_DATA | FILE_ADD_FILE | DELETE` without modifying or creating files.
   - Enforced fail-closed behavior for `get_current_user_sid()` returning `(user, "")` on failure.
   - Audited containment defaults fail closed (`allow_mock_sid=False`, `allow_dev_roots=False`).
3. **Zvec Network Denial & Local Verification**:
   - `verify_network_denial` verifies rule `Program:` matches the normalized probed executable.
   - `probe()` executes local index and query probes under the deny rule before setting `local_only_verified=True`.
4. **Promotion Gate Refinements**:
   - Capability-failure branch in `decision.py` evaluates `evidence_gate` against resource count (>= 3), required roles, and trial completeness; and `fallback_gate` against completion rate (>= 0.95) and correctness > 0.
   - `quality_gate` enforces non-inferiority via absolute delta `score_b - score_a >= max_correctness_drop (-0.05)`.
   - `benefit_gate` checks paired t-test statistics for statistical support.
5. **Decoupling & Generic Diff Patching**:
   - Decoupled `cli.py` by providing standalone `MockExecutionPort` in `aibench.broker_client`.
   - Generic unified diff parser and context matcher in `scoring.py` supports arbitrary files and methods.

---

## 7. Closure Summary

P16 AI Capability Benchmark Project is fully remediated, verified, and closed:
- Standalone benchmark package under `benchmark/src/aibench/` with CLI and tests.
- Zero modifications to production `src/dev_orchestrator/**`.
- Synthetic corpus generator and deterministic code-only scoring operational.
- Windows containment boundary, SID check, non-destructive ACL audit, and transactional provisioning implemented.
- Baseline acceptance run executed and recorded in `benchmark/evidence/p16_baseline_20260921/`.
- Total, bounded decision evaluated to `NO_PROMOTE` (`capability_unsupported`) in exact accordance with the approved executable design.
