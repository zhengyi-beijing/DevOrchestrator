# DevOrchestrator

Project-neutral local orchestration infrastructure for AI-assisted development.
One DevOrchestrator daemon can observe multiple external Git repositories, accept
stateless project control intents, drive planner/worker/reviewer lifecycle roles,
and delegate exact AI-resource execution to AIResourceBroker. Browser Bridge is
retained as a compatibility transport rather than an authoritative state store.

## Current authority boundary

Implemented:

- multi-project repository/Worker monitoring and one-process daemon;
- stateless `project-status <project_id>` and `project-continue <project_id>` control;
- continuous lifecycle routing across PLAN / EXECUTE / REVIEW / REMEDIATE boundaries;
- provider-neutral `AIExecutionPort` integration with AIResourceBroker;
- independent AIBroker Planner, plan Reviewer, Worker, and Worker Reviewer roles;
- restart reconciliation, idempotent control history, and fail-closed repository-truth guards;
- active-project progress watchdog, automatic read-only diagnostics, and safe two-phase recovery;
- Browser Bridge/Web Sol compatibility path for projects not yet migrated.

DevOrchestrator owns lifecycle semantics. AIResourceBroker owns provider/account/model
selection and exact provider execution. See `docs/AIBROKER_INTEGRATION_CONTRACT.md`
and `docs/CONTINUOUS_EXECUTION_ACCEPTANCE_2026-09-10.md`.

Hardware actions remain outside this generic orchestration authority.

## Portable external-project onboarding

DevOrchestrator runs as a standalone service. External product repositories are
referenced by configuration; they do not need to vendor or import this source.
Generic Git repositories can be monitored this way. The currently implemented
WORKER_DONE/Web Sol flow additionally expects the `agent_files` project
contract (or another registered ProjectAdapter); dynamic third-party adapter
discovery is not part of V1. See `docs/PORTABLE_PROJECT_INTEGRATION.md`.

Copy the example configuration:

```powershell
Copy-Item .\config\projects.example.json .\config\projects.json
```

Edit `project_id` and `repo_path`. Relative `repo_path` values are resolved from
the directory containing `projects.json`, not from the current shell directory.

Validate the integration before starting services:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m dev_orchestrator validate-config --config .\config\projects.json
```

A project may be orchestration-ready without a browser conversation binding when
it uses the direct AIBroker reviewer/planner path. Legacy browser-bound projects
remain supported during migration.

## Run

One monitor tick:

```powershell
python -m dev_orchestrator monitor --once `
  --config .\config\projects.json `
  --runtime-root .\runtime
```

Preferred unified daemon:

```powershell
python -m dev_orchestrator start-daemon `
  --config .\config\projects.json `
  --runtime-root .\runtime
```

Default surfaces:

- dashboard: `http://127.0.0.1:8770/`
- Browser Bridge: `http://127.0.0.1:8765/`

The daemon-hosted dashboard is the normal observation and control surface.
Its buttons come from server-advertised capabilities and every mutation is
revalidated by the daemon. To publish ChatGPT conversation presence, click
**Pair ChatGPT heartbeat** on the dashboard, copy the displayed
`pairing-id:code`, then choose **Pair DevOrchestrator 8770 heartbeat** from the
installed userscript menu and paste the value. The single-use capability can
send heartbeat/liveness only; lifecycle actions remain on authenticated 8770.

The CLI uses that same loopback authenticated API. For example:

```powershell
python -m dev_orchestrator control-overview `
  --config .\config\projects.json `
  --runtime-root .\runtime
python -m dev_orchestrator project-control <project_id> pause `
  --runtime-root .\runtime
```

Status / stop:

```powershell
python -m dev_orchestrator status-daemon --runtime-root .\runtime
python -m dev_orchestrator watchdog-status --runtime-root .\runtime
python -m dev_orchestrator stop-daemon --runtime-root .\runtime
```

Legacy independent monitor/Web lifecycle commands remain compatibility paths,
but one daemon per computer is the target architecture.

## Canonical self-host operational acceptance

The canonical `main` worktree for DevOrchestrator self-hosting is
`C:\work\github\DevOrchestrator-dev`.

To validate configuration, inspect or start the unified daemon, and run the
read-only operational acceptance utility:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m dev_orchestrator validate-config --config .\config\projects.json
python -m dev_orchestrator status-daemon --runtime-root .\runtime
# Start daemon if not running:
# python -m dev_orchestrator start-daemon --config .\config\projects.json --runtime-root .\runtime
python ops/self_host_acceptance.py
```

Under Windows PowerShell 5.1, run each command as a separate statement; do not
use `&&` or `||` operators.

### Acceptance behavior and overrides

`ops/self_host_acceptance.py` verifies:
- daemon health, running state, and fresh monitor heartbeat;
- enabled, non-degraded 8770 Control API authority;
- exact project identity (`devorchestrator` on branch `main` at the repository root);
- P11 execution accounting availability;
- AIBroker resource and execution visibility both through the unified surface and directly on 8875.

The utility is strictly read-only: it issues GET requests only and cannot mutate
lifecycle state.

- **Exit code 0 (`"status": "PASS"`)**: all required checks pass. Any reported
  overview warnings are preserved in the result without causing failure.
- **Non-zero exit code (`"status": "FAIL"`)**: returned when any required check
  is unavailable, degraded, stale, malformed, or has an identity mismatch.
  Categorized diagnostics describe the issue without exposing secrets or
  response bodies.

To override target endpoints or project identity:

```powershell
python ops/self_host_acceptance.py `
  --control-url http://127.0.0.1:8770 `
  --broker-url http://127.0.0.1:8875 `
  --project-id devorchestrator `
  --expected-repo-path C:\work\github\DevOrchestrator-dev `
  --expected-branch main `
  --timeout 10.0
```

## Tests

Python regression suite:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests_py -v
```

Userscript syntax check:

```powershell
node --check .\browser\chatgpt-web-adapter.user.js
```

Repository hygiene:

```powershell
git diff --check
```

The normal automated suite uses temporary repositories and local loopback
services; XLabServer, LabDemo hardware, and a deployed ChatGPT browser are not
required for ordinary Core development.

## Repository ownership boundary

DevOrchestrator owns its configured runtime directory and lifecycle ledgers.
Managed planning may deterministically freeze an approved plan and managed Workers
may modify the explicitly configured project repository. Repository-truth guards,
review independence, and project execution policy bound those writes.

Machine-specific project paths, browser bindings and runtime choices belong in
the ignored `config/projects.json` or other explicitly selected local config,
not in Core source.
