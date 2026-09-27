# P17 Runtime Root Inventory & Target Stable/Dev Deployment Design

Status: **FROZEN / ACCEPTED (PHASE W)**  
Task: P17 Single-Authority Goal Convergence Baseline  

Inventories actual path, runtime, and configuration ownership across the system,
and specifies the target separation of controller code, development workspace,
canonical state root, and configuration roots.

---

## 1. Inventory of Current Roots

| Root Identifier | Current Location / Derivation | Current Production Behavior | Target Architecture Disposition |
| :--- | :--- | :--- | :--- |
| **`controller_root`** | `C:\work\github\DevOrchestrator-dev` (active dev worktree) / `C:\work\github\DevOrchestrator` (detached stable worktree) | Historically, daemon running in one directory derived runtime paths relative to `REPO_ROOT = Path(__file__).resolve().parent.parent`. | Stable controller executable code strictly located at dedicated controller root; never mutates own worktree during normal worker tasks. |
| **`workspace_root`** | `C:\work\github\DevOrchestrator-dev` | The target repository where AI Workers, Planners, and Reviewers modify code and run tests. | Explicitly configured per project: `project.repo_path`. Completely decoupled from controller code location. |
| **`state_root`** | `<controller_root>\runtime\` (or `<workspace_root>\runtime\`) | Implicitly derived from whichever repository process executes the daemon, risking split-brain state trees. | **One canonical absolute state root** (e.g. `C:\devorch\runtime\`). Must NEVER be derived from code location. Fail closed if not explicitly set. |
| **`config_root`** | `<controller_root>\config\` | Loaded relative to current working directory or `REPO_ROOT`. | Explicit absolute configuration root (e.g. `C:\devorch\config\`). Only one authoritative configuration store. |
| **`logs_root`** | `<controller_root>\runtime\logs\` | Child of implicit runtime directory. | Child of explicit canonical `state_root`. |
| **`shadow_sink`** | `<workspace_root>\runtime\p17-shadow\` | Created in P17 as pure isolated shadow namespace. | Restricted exclusively to development workspace; strictly forbidden from canonical state root. |

---

## 2. Target Stable / Development Deployment Architecture

```
+-------------------------------------------------------------+
|                     Host Machine (ZXZ-PC)                  |
+-------------------------------------------------------------+
                              |
       +----------------------+----------------------+
       |                                             |
       v                                             v
[Controller Root]                            [Workspace Root]
C:\work\github\DevOrchestrator              C:\work\github\DevOrchestrator-dev
(Stable daemon & runtime code)               (Development target worktree)
       |                                             |
       +----------------------+----------------------+
                              |
                              v
                   [Canonical State Root]
                    C:\devorch\runtime\
                   (state_root_owner.lock)
                   (single canonical writer)
                              |
               +--------------+--------------+
               |                             |
               v                             v
       [Canonical Control]           [Canonical History]
       runtime/control/inbox         runtime/control/history/
```

### 2.1 State-Root Ownership Lock Specification
To prevent split-brain state corruption, the canonical state root is guarded by a mandatory filesystem lock:
- File location: `<state_root>/state_root_owner.lock`
- Payload schema:
  ```json
  {
    "host": "ZXZ-PC",
    "pid": 30628,
    "controller_root": "C:\\work\\github\\DevOrchestrator",
    "controller_commit": "c62cb60fd1949b8c41ba1c12811b06066054142b",
    "acquired_at": "2026-09-27T05:54:07.847731+00:00"
  }
  ```
- **Fail-Closed Rule**: If a second daemon attempts to initialize or reconcile against `<state_root>` with a differing `controller_root` or conflicting PID, initialization MUST fail closed immediately with a typed fatal error (`LOCKED_BY_ANOTHER_CONTROLLER`).

---

## 3. Seven-Step Safe Promotion Protocol

When promoting development improvements to the stable controller, the deployment executes the following atomic sequence:

1. **Accepted Dev Commit**: Worker completes task; independent reviewer emits `ACCEPT` / `NEXT`; all verification records pass at exact anchor.
2. **Clean Exact Dev SHA**: Git status in `workspace_root` is verified completely clean; exact SHA captured (e.g. `c62cb60...`).
3. **Explicit Promotion Transaction**: An explicit owner-authorized command transfers code to `controller_root` (via fast-forward or clean checkout) without altering `state_root`.
4. **Shared Canonical State / Config**: Both roots point to the exact same external `state_root` and `config_root`.
5. **Controlled Daemon Restart**: Daemon at `controller_root` is stopped gracefully; new daemon started on the new commit.
6. **Health and Replay Smoke Check**: Daemon runs health probe (`/v1/health` and invariant checks); verifies zero split-brain or orphaned leases.
7. **Rollback on Failure**: If any invariant or smoke check fails, immediately roll back `controller_root` to the prior stable SHA while preserving durable state intact.

---

## 4. Current P17 Guardrail
P17 strictly **prohibits** activating the stable/dev split in production during Phase W.
All stable/dev separation tests in P17 run exclusively inside isolated temporary fixtures (`tests_py/test_p17_contract_amendments.py::test_isolated_fixture_runtime_root_fail_closed_and_ownership_lock`).
Production migration is deferred to future gated migration **M5**.
