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

Status / stop:

```powershell
python -m dev_orchestrator status-daemon --runtime-root .\runtime
python -m dev_orchestrator stop-daemon --runtime-root .\runtime
```

Legacy independent monitor/Web lifecycle commands remain compatibility paths,
but one daemon per computer is the target architecture.

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
