# Portable project integration contract

Status: **V1 CONTRACT — CONFIG-DRIVEN / PROJECT-NEUTRAL**

## Purpose

DevOrchestrator is a standalone service that observes and orchestrates external
repositories. A product repository does not import DevOrchestrator source code
and DevOrchestrator Core does not branch on product names.

A new project is onboarded by configuration. The observed repository remains
read-only to the monitor/Bridge/Decision Guard slices currently implemented.
Worker actuation is a separate future authority boundary.

## Supported deployment model

```text
DevOrchestrator clone/service
  + config/projects.json
  + runtime/                  # DevOrchestrator-owned, ignored by Git
  + external project A       # referenced by repo_path
  + external project B
```

Python 3.11+ and Git must be available on the DevOrchestrator host.
The external project may live anywhere accessible to that host.

## Minimal project configuration

```json
{
  "projects": [
    {
      "project_id": "my-product",
      "repo_path": "../../my-product"
    }
  ]
}
```

`repo_path` may also be relative. Relative paths are resolved from the directory
containing the selected `projects.json`, never from the shell working directory.
This makes a checked-in deployment layout relocatable across development hosts.

Legacy `id` / `root` remain accepted, but new configurations should use
`project_id` / `repo_path`.

## Validate before starting services

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m dev_orchestrator validate-config --config .\config\projects.json
```

Validation is read-only and fails closed when:

- configuration JSON or canonical project identity is invalid;
- canonical project ids or live conversation bindings conflict;
- the configured adapter is unknown;
- repository truth cannot be read from Git.

A project without `conversation_binding` is still a valid **monitor-only**
project. `orchestration_ready=false` is reported explicitly; it is not a
configuration failure.

## Default `agent_files` adapter

For richer task/Worker projection, an external project may expose:

```text
agent/CURRENT.md
agent/next.md
agent/result.md              # optional for monitoring
tmp/worker-dsh/status.json   # or another configured worker_runtime
```

Missing agent/Worker files do not cause DevOrchestrator to write them.
The adapter only projects what exists.

For the currently implemented WORKER_DONE -> Web Sol flow, a project must
either follow this `agent_files` contract or use another registered
`ProjectAdapter`. V1 does not yet provide dynamic third-party adapter plugin
discovery, so "config-only full orchestration" currently means
`agent_files`-compatible projects. Generic Git repositories remain valid
monitor-only integrations.

## Enabling Web Sol transport

Browser/Web Sol transport is optional. To become orchestration-ready, add a
credential-free binding:

```json
"conversation_binding": {
  "transport": "browser_bridge",
  "adapter": "chatgpt_web",
  "binding_id": "<chatgpt-conversation-id>"
}
```

The Browser Bridge, response consumer and Decision Guard remain project-neutral.
The current implementation can transport/validate reasoning results but does
not execute `next_action` or start/restart a Worker.

## Run

One read-only monitor tick:

```powershell
python -m dev_orchestrator monitor --once `
  --config .\config\projects.json `
  --runtime-root .\runtime
```

Preferred one-process service:

```powershell
python -m dev_orchestrator start-daemon `
  --config .\config\projects.json `
  --runtime-root .\runtime
```

## V1 acceptance boundary

- a clean DevOrchestrator clone can run the automated suite without XLabServer;
- an arbitrary external Git repository can be registered by configuration only;
- monitoring does not modify the external repository;
- relative project paths are deterministic and config-relative;
- onboarding can be checked with `validate-config` before service start;
- LabDemo/XLabServer and a deployed ChatGPT browser are integration targets,
  not prerequisites for ordinary Core development.
