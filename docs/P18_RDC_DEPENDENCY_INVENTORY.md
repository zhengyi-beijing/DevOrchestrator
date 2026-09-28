# P18 RDC Dependency Inventory & Native Transport Classification

## Executive Summary

Remote Desktop Commander (RDC) has historically been used as a broad, interactive fallback for remote machine operations on DevOrchestrator hosts (notably `ZXZ-PC`). This document provides the authoritative inventory of machine operations, classifying them into:
1. **Routine Local / SSH Candidates**: Routine CLI, process, log, file, test, build, and status operations that execute non-interactively and deterministically via DevOrchestrator native transports (`LocalMachineTransport` / `SSHMachineTransport`).
2. **GUI-Only Operations**: Operations that strictly require interactive desktop windows, displays, or direct visual GUI interaction.
3. **Bootstrap and Emergency Operations**: Initial host provisioning, Tailscale/SSH setup, machine reboots, and dead-daemon manual rescues.
4. **Hardware Operations**: Real X-ray source, conveyor/VFD, detector-board, and serial-device actions. These are strictly classified as `hardware` and rejected unconditionally in P18.

## Measurable Target

- **Routine DevO Operations**: Measurable reduction from historical RDC reliance to **0 RDC calls** for routine status, log inspection, file read/write staging, git commands, and test/build executions.
- **RDC Transport**: Relegated to an explicit, non-executing escalation descriptor (`RDCTransport`) returning `rdc_fallback_required`.

---

## Detailed Operation Inventory

| Operation | Category | Target Transport | Effect Class | Durable Path Required | Before P18 | Target Post-P18 | Observed Post-P18 (ZXZ-PC) | Description / Fencing |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `git status` | Routine CLI | Local / SSH | `read_only` | No (direct exec) | RDC GUI / shell | Native exec | Local native exec; 0 RDC | Repository status check in canonical worktrees. |
| `git log` / `git diff` | Routine CLI | Local / SSH | `read_only` | No (direct exec) | RDC GUI / shell | Native exec | `git log` via local native exec; 0 RDC | Inspecting commit history and working tree diffs. |
| `git fetch` / `git checkout` | Routine CLI | Local / SSH | `idempotent` | Yes (durable spawn) | RDC GUI / shell | Native spawn | Not exercised (mutating sync outside acceptance scope) | Safe git workspace synchronization. |
| `git commit` | Routine CLI | Local / SSH | `effectful` | Yes (durable spawn) | RDC GUI / shell | Native spawn | Not exercised (commit performed after verification) | Local version control record creation. |
| Daemon Status (`devorch-status`) | Control API | Local / SSH | `read_only` | No (direct exec) | RDC GUI / browser | Control API / exec | Local native exec; 0 RDC | Checking daemon lifecycle, monitor heartbeat, and worker state. |
| Daemon Log Inspection | Log Read | Local / SSH | `read_only` | No (direct exec) | RDC text editor | Control API / exec | Local native exec; 0 RDC | Paginated reading of daemon, worker, and event logs. |
| File Read | File I/O | Local / SSH | `read_only` | No (direct read) | RDC explorer | Native `read_file` | Local `read_file` plus binary readback; 0 RDC | Scoped reading under host-local `file_roots`. |
| File Stat | File I/O | Local / SSH | `read_only` | No (direct stat) | RDC explorer | Native `stat` | Local `stat`; 0 RDC | Scoped metadata and digest inspection. |
| Arbitrary File Write | File I/O | Local / SSH | `effectful` | Yes (durable CAS intent) | RDC drag-drop / editor | Native `stage_write` + `write_file` | 14-byte binary stage + CAS + readback; 0 RDC | Strict Base64 staging followed by CAS write intent under canonical path lock. |
| Test Execution (`pytest`) | Process | Local / SSH | `idempotent` | Yes (durable spawn) | RDC terminal | Native spawn via `JobService` | Durable compile/build exercise covered the execution path; 0 RDC | Detached test suite execution with bounded log capture. |
| Build / Lint Execution | Process | Local / SSH | `idempotent` | Yes (durable spawn) | RDC terminal | Native spawn via `JobService` | `compileall` job completed with exit 0; 0 RDC | Running compilation, code formatting, or type checking. |
| AI Broker / Model Dispatch | AI Invocation | Local / SSH | `effectful` | Yes (durable spawn / broker) | RDC browser / script | Native execution transport | Not exercised (outside machine-operation acceptance set) | Existing P13/P14 broker dispatch paths. |
| Interactive GUI Tooling | GUI-Only | RDC Fallback | `manual_gui` | No | RDC | RDC (Escalation only) | Not exercised; native acceptance required no GUI | Inspecting proprietary GUI viewers, Windows desktop notifications. |
| Host Initial Provisioning | Bootstrap | RDC Fallback | `bootstrap` | No | RDC | RDC (Manual bootstrap) | Not exercised | Initial OpenSSH server setup, Tailscale peer authentication. |
| Hard Recovery / Dead Host | Emergency | RDC Fallback | `emergency` | No | RDC | RDC (Manual rescue) | Not exercised | Rescuing hung OS, blue screens, network adapter resets. |
| Real X-ray Tube Actuation | Hardware | Rejected | `hardware` | N/A | Manual / GUI | **REJECTED** | Not exercised; remains unconditionally rejected | Unconditionally rejected in P18 with escalation evidence. |
| Conveyor / VFD Motion | Hardware | Rejected | `hardware` | N/A | Manual / GUI | **REJECTED** | Not exercised; remains unconditionally rejected | Unconditionally rejected in P18 with escalation evidence. |
| Detector Board I/O | Hardware | Rejected | `hardware` | N/A | Manual / GUI | **REJECTED** | Not exercised; remains unconditionally rejected | Unconditionally rejected in P18 with escalation evidence. |
| Raw Serial Device Command | Hardware | Rejected | `hardware` | N/A | Manual / GUI | **REJECTED** | Not exercised; remains unconditionally rejected | Unconditionally rejected in P18 with escalation evidence. |

---

## Safety and Identity Fences

1. **Host-Local Authority**: Executing hosts determine executable commands, parameters, file roots, and path containment strictly from host-local `execution-jobs.json`. Wire callers can never supply executable scripts, shell strings, or uncontained paths.
2. **Read-Only vs Effectful Separation**:
   - `read_only`: Direct non-persisted execution permitted; failover permitted only on pre-dispatch connection failure.
   - `idempotent` / `effectful`: Must route through durable `JobService` or durable write-intent store. Disconnect during or after dispatch is classified as `ambiguous`; automatic replay and cross-transport failover are prohibited.
3. **Binary Staging and CAS Handoff**:
   - Binary data is never passed through argv, command parameters, environment variables, or JSON text conversion.
   - Decoded bytes are validated for size (<= 8 MiB) and SHA-256 digest, then stored in digest-addressed staging.
   - Mutation requires a durable compare-and-swap (CAS) intent evaluated under a file lock on the target's canonical path.
4. **Hardware Prohibition**:
   - Any command referencing hardware devices is unconditionally rejected with `hardware_execution_not_supported_in_p18`.

## ZXZ-PC Zero-RDC Acceptance (2026-09-28)

The bounded acceptance exercise ran on `ZXZ-PC` through the current P18 loopback Control API using PowerShell `5.1.26100.9444`. Sequencing used semicolons and explicit status checks; it did not use `&&` or `||`. The canonical daemon was not restarted during remediation, so an ephemeral loopback server loaded the current worktree while leaving lifecycle authority undisturbed.

- Four read-only native execs succeeded for daemon status, daemon-log inspection, `git status`, and `git log`.
- Durable job `job-927222a2d8290076` ran `transport_compileall`, progressed through queued/running, and completed with exit code `0`.
- Native `read_file` and `stat` succeeded for this inventory document.
- A 14-byte arbitrary binary payload (`AAECDQoff4DI/v9BQkM=`) was staged, committed with a durable CAS intent, and read back byte-for-byte with digest `sha256:feec999ce6022562110591cd579bf1fd8e009f288c3a0e6c604b5f1b533cc8bf`.
- All 14 captured transport-operation rows selected `local`; observed RDC calls were `0`.
- The historical execution report returned `data_status.rdc = unavailable`, so no numeric pre-P18 baseline is claimed.

Measured summary: [`docs/evidence/P18_ZXZ_PC_ZERO_RDC_ACCEPTANCE.json`](evidence/P18_ZXZ_PC_ZERO_RDC_ACCEPTANCE.json). Captured operation rows: [`docs/evidence/P18_ZXZ_PC_TRANSPORT_OPERATIONS.ndjson`](evidence/P18_ZXZ_PC_TRANSPORT_OPERATIONS.ndjson).
