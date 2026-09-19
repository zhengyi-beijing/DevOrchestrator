# P14.5 Reviewer Harness & OpenCodeReview Adapter Contract

## 1. Overview and Purpose

P14.5 establishes a provider-neutral, evidence-only **Reviewer Harness** within DevOrchestrator, initially backed by **OpenCodeReview** (OCR) for deterministic preparation, file selection, and rule resolution, and **AIBroker** for semantic reviewer model delegation.

Reviews execute as recoverable P14 durable jobs, emit content-addressed input and bounded output artifacts (findings, coverage, session metadata, and SARIF 2.1.0), and report evidence exclusively to `AIReviewerCoordinator`.

---

## 2. Authority Boundaries

1. **DevOrchestrator Lifecycle Authority**: `AIReviewerCoordinator` is the sole lifecycle authority for technical code review. It alone writes `review-decisions.json` and emits lifecycle progress events (`REVIEW_STARTED`, `REVIEW_ACCEPTED`, `REMEDIATE`, `REVIEW_FAILED`, `OWNER_GATE`).
2. **Evidence-Only Boundary**:
   - `ReviewerHarness` is strictly an evidence-producing interface. It exposes `submit`, `status`, `reconcile`, and `result` operations with zero lifecycle mutation capability.
   - `OpenCodeReview` is an external preparation engine, invoked solely as a pinned executable with `shell=False`. OCR never receives provider credentials, never selects a provider or model, and cannot advance DevO lifecycle states.
   - Delegated AI reviewer models operate under a strict evidence-only output schema (`reviewed_files`, `skipped_files`, `findings`). Any model-supplied `decision` or `next_action` token is explicitly rejected.
   - The P14 job runtime (`ExecutionJobStore`, `LocalJobTransport`, `SSHJobTransport`, `supervisor`) executes allowlisted commands and produces execution evidence only.
3. **Preservation of Direct and Legacy Paths**: Projects that do not opt into `reviewer_harness` continue using the existing direct AIBroker reviewer thread or Browser Bridge paths without change.
4. **Preservation of Independent Gates**: Semantic review findings complement but never replace independent compiler, build, test, lint, or static-analysis gates. A review cannot produce an advancing `next` decision unless required independent gate evidence is independently successful.

---

## 3. Review Modes and Repository Identity Anchoring

### 3.1 Review Modes
- **Diff Mode (`mode="diff"`)**:
  - `workspace`: Reviews uncommitted working tree changes against current `HEAD`.
  - `range`: Reviews committed differences between `base` and `head` references.
  - `commit`: Reviews the diff introduced by a specific commit.
- **Scan Mode (`mode="scan"`)**:
  - Bounded full-repository scan constrained strictly to configured `scan_roots` (repository-relative directories).
  - Enforces deterministic limits on maximum files, total bytes, and packet batch sizes to prevent resource exhaustion.

### 3.2 Repository Identity Anchors
Every `ReviewRequest` records immutable repository truth at submission:
- `branch`: Current active branch name.
- `head`: Current commit SHA-1/SHA-256 hash.
- `status_hash`: Cryptographic digest of tracked/uncommitted repository status.

Before `AIReviewerCoordinator` accepts any review result or writes `review-decisions.json`, current repository truth is re-evaluated against these recorded anchors. If `branch`, `head`, or `status_hash` has drifted, the review fails closed without advancing the lifecycle.

---

## 4. OpenCodeReview Adapter & Capability Probing

### 4.1 Invocation Contract
- Executed strictly via `subprocess.Popen` with `shell=False` and fixed argv construction.
- Resolves only allowlisted verbs:
  - Capability probe: `ocr --version` or `ocr --capabilities` returning version and supported features.
  - Diff preview: `ocr review-preview --diff-mode <mode> --base <ref> --head <ref>` emitting JSON file selection and exclusions.
  - Scan preview: `ocr scan-preview --roots <roots>` emitting JSON file selection within scan bounds.
  - Rule resolution: `ocr resolve-rules --rules <rule_pack_path>` associating rules with target files.
- Fails closed on non-zero exit code, unparseable JSON, or capability mismatch.
- Human-oriented text outputs are never parsed as structured evidence.

### 4.2 Security and Containment
- Repository path and scan roots must be contained within `repo_path` using `realpath` resolution. Traversal (`..`) or symlinks escaping containment are rejected.
- Wire-supplied refs and paths are validated strings only; they never become shell commands or environment variables.
- OCR never receives provider API keys, tokens, or model configurations.

---

## 5. Review Session Lifecycle

A review session follows an explicit state machine:

```text
INITIALIZED → PREPARING → PREPARED → SUBMITTED → RUNNING → COMPLETED (Terminal)
                                                         ↘ FAILED (Terminal)
                                                         ↘ CANCELLED (Terminal)
                                                         ↘ UNKNOWN_RECOVERY
```

- `initialized`: `ReviewRequest` validated and session identity assigned.
- `preparing`: OCR invoked for diff/scan preview and rule resolution.
- `prepared`: `ReviewManifest` generated, input digest computed, input artifact persisted.
- `submitted`: P14 job claimed and supervisor spawned via `JobService.submit()`.
- `running`: Detached supervisor running review packets against AIBroker.
- `completed`: All packets executed, output artifacts verified and persisted. (Terminal)
- `failed`: Preparation error, unrecoverable packet failure, or validation error. (Terminal)
- `cancelled`: Explicitly cancelled by operator. (Terminal)
- `unknown_recovery`: Transport dropped or supervisor crashed without terminal artifacts. Reconciled before retry.

---

## 6. Structured Findings Schema

Each localized finding adheres to a versioned data schema:

| Field | Type | Description |
| :--- | :--- | :--- |
| `fingerprint` | `string` | Deterministic SHA-256: `sha256(file + ":" + start_line + ":" + end_line + ":" + rule_id + ":" + category)` |
| `file` | `string` | Repository-relative POSIX file path. |
| `start_line` | `integer` | 1-based inclusive start line. |
| `end_line` | `integer` | 1-based inclusive end line (`end_line >= start_line`). |
| `severity` | `string` | Closed enum: `"blocking"`, `"warning"`, `"info"`. |
| `category` | `string` | Finding category (e.g., `"security"`, `"correctness"`, `"durability"`, `"style"`). |
| `rule_id` | `string` | Rule identifier matching rule pack. |
| `rule_provenance` | `string?` | Originating rule pack or policy reference. |
| `message` | `string` | Descriptive explanation of the finding. |
| `evidence` | `string?` | Relevant code snippet from the reviewed file. |
| `resource_context` | `object?` | AIBroker resource identity (`resource_id`, `provider`, `account`, `model`). |
| `session_id` | `string` | Originating review session identifier. |
| `packet_id` | `string?` | Specific packet identifier in which the finding was detected. |
| `status` | `string` | Initial status (`"open"`). |

Path containment is strictly enforced: `file` must be a relative path within the repository root; absolute paths or traversal outside the repository fail validation.

---

## 7. Coverage Accounting and Completeness

### 7.1 Exhaustive File Accounting
Every selected file from `ReviewManifest` must be accounted for in `ReviewCoverage`:
- `selected_files`: All candidate files identified by OCR preview.
- `reviewed_files`: Files successfully inspected by semantic reviewer packets.
- `reused_files`: Files whose prior valid review results were safely reused.
- `skipped_files`: Files explicitly skipped with recorded reason (e.g. generated, binary, size cap).
- `failed_files`: Files where model inspection failed or timed out.
- `excluded_files`: Files excluded during preparation with recorded reasons.

### 7.2 Completeness State
- `complete`: Every selected file is either `reviewed` or validly `skipped`. Coverage rate is 100% of reviewable scope.
- `partial`: One or more files remain unreviewed due to packet failure, budget limits, or transport interruption.
- `failed`: Preparation failure or packet execution crash.
- `cancelled`: Session was cancelled.

**Completeness Contract**: Zero findings is acceptable evidence to advance the lifecycle (`decision="next"`) **only if** coverage completeness is `complete`. A `partial` coverage outcome cannot advance the lifecycle and must fail closed.

---

## 8. Artifact Retention and P14 Integration

### 8.1 Input Artifact
- Written to `<job_dir>/input.json` before supervisor launch.
- Carries `ReviewRequest` and `ReviewManifest`.
- Content-addressed via `sha256` digest recorded in `JobSpec.input_digest` and `JobRecord.input_digest`.
- Preserved byte-identically across deterministic retry successors.

### 8.2 Output Artifacts
Retained under `<job_dir>/artifacts/` within configured byte limits:
- `findings.json`: Canonical JSON array of validated `ReviewFinding` objects.
- `coverage.json`: Canonical JSON object of `ReviewCoverage`.
- `session.json`: Final serialized `ReviewSession` metadata.
- `review.sarif`: Deterministic Static Analysis Results Interchange Format (SARIF 2.1.0) document.

### 8.3 Digest-Verified Retrieval
Artifacts retrieved via `JobService.get_artifact()` (locally or over SSH) verify content SHA-256 against the recorded descriptor before being consumed by the harness.

---

## 9. Deterministic Disposition Policy

`AIReviewerCoordinator` derives lifecycle decisions from evidence using a deterministic disposition policy:

1. **Pre-condition Checks**:
   - Repository truth unchanged: `branch`, `head`, `status_hash` match the anchors recorded in `ReviewRequest`.
   - Independent gates: all required independent build, test, and static-analysis gates report `"passed"`.
2. **Coverage Gate**:
   - If `coverage.completeness != "complete"`: no advancing decision; record review failure or required recovery.
3. **Findings Classification**:
   - If any finding has `severity in blocking_severities` (default: `"blocking"`):
     - Decision: `remediate`
     - Next action: `continue_current_stage`
     - Reason: Summarizes blocking findings with rule IDs and file locations.
     - Writes `review-decisions.json` and emits `REMEDIATE`.
   - If all findings are non-blocking (`"warning"`, `"info"`) and coverage is `complete`:
     - Decision: `next`
     - Next action: `next_task`
     - Reason: Documents clean review with complete coverage.
     - Writes `review-decisions.json` and emits `REVIEW_ACCEPTED`.

---

## 10. Recovery and Replay Invariants

1. **Daemon Restart**: In-flight review sessions are detected at daemon startup. The P14 job status is reconciled. Completed artifacts are reloaded by digest. In-flight packets reconcile AIBroker status before any retry.
2. **Transport Loss**: SSH transport drop transitions the P14 job to `unknown_recovery`. Reconnecting polls remote status and recovers without duplicate supervisor spawns or duplicate AIBroker dispatches.
3. **Idempotency**: Re-submitting an identical review request returns the existing session and job without creating duplicate executions.
