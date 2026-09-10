# AGY Project Isolation Contract

Status: IMPLEMENTED / LIVE VALIDATED
Date: 2026-09-10

DevOrchestrator project identity and Antigravity provider-project identity are separate namespaces. `AgentRequest.project_id` identifies the DevOrchestrator project; each project's local `execution.backends.agy.project` identifies an existing Antigravity provider project ID or name.

Required invocation identity:

```text
agy --project <execution.backends.agy.project> ... --add-dir <working_directory> ...
```

The child process also uses `cwd=<working_directory>`, and the `[WORKSPACE]` prompt prefix remains defense in depth. Neither cwd, `--add-dir`, prompt text, nor `AgentRequest.project_id` may substitute for an explicit provider project.

A missing provider project leaves the project execution policy valid so other configured backends remain routable, but `AgyBackend.probe()` reports unavailable and `start()` fails before creating run artifacts. An explicitly present blank value is a configuration error. No code path may fall back to AGY's ambient, recent, or `default-cli-project` context.

Required automated coverage:
- configured provider project is forwarded exactly to `--project`;
- request `project_id` cannot override the provider project;
- missing/blank provider project fails closed without run artifacts;
- an unprovisioned AGY backend does not block routing to another available backend;
- separate backend instances can carry separate provider projects;
- `--add-dir` and cwd remain repository-scoped;
- `--dangerously-skip-permissions` remains forbidden.

ZXZ-PC live validation: historical LabDemo, LineScanViewer, and xray-hw-platform CLI conversations all used `default-cli-project`. On 2026-09-10 three distinct Antigravity projects were provisioned from the matching repository folders and their provider IDs were stored only in ignored local `config/projects.json`.

Three read-only headless smokes then completed with exit code 0, returned the expected DevOrchestrator project identity, left every repository `git status` unchanged, and Antigravity logs independently confirmed `Conversation using project ID` matched the distinct provider project for LabDemo, LineScanViewer, and xray-hw-platform.

Operational note: Antigravity 1.1.28 accepts both valid and nonexistent project IDs with `--project <id> --version`, so `probe()` can validate executable availability but cannot prove provider-project existence without starting a session. A stale mapping therefore fails closed on the first real run; local provisioning plus the read-only smoke is the acceptance evidence for project existence.