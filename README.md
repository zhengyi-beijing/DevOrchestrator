# DevOrchestrator

Project-neutral local orchestration infrastructure for AI-assisted development.
One DevOrchestrator service can observe multiple external Git repositories,
route bounded reasoning events through a Browser Bridge, and persist validated
decision dispositions without embedding product-specific logic in Core.

## Current authority boundary

Implemented:

- multi-project read-only repository/Worker monitoring;
- one-process daemon with dashboard and Browser Bridge;
- configuration-driven `ProjectAdapter` selection;
- WORKER_DONE reasoning-event dispatch;
- ChatGPT Web binding/claim/renew/response transport;
- response stability, replay/idempotency and live-binding gates;
- Response Consumer + repository-truth Decision Guard;
- AgentBackend registry/router foundation.

Not implemented/authorized:

- executing a `next_action`;
- starting/restarting a project Worker from a Web Sol decision;
- PHASE_AUTO or automatic stage crossing;
- hardware actions.

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

A project without a browser conversation binding is valid but monitor-only.
`validate-config` reports `orchestration_ready=false` explicitly.

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

DevOrchestrator owns its configured runtime directory. Monitoring, Bridge
transport and response validation do not write into observed product
repositories. Current Worker actuation remains intentionally absent.

Machine-specific project paths, browser bindings and runtime choices belong in
the ignored `config/projects.json` or other explicitly selected local config,
not in Core source.
