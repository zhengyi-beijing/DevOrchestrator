# DevOrchestrator Backlog

Longer-horizon work that is intentionally not part of the currently authorized execution slice.

## Machine-independent development environment

Goal: make normal DevOrchestrator development and most integration testing independent of XLabServer or any single lab machine. XLabServer should remain only a real integration target, not a development prerequisite.

Scope candidates:
- keep machine-specific project paths, browser bindings, ports, and runtime locations in local overrides rather than repository assumptions;
- provide a portable development/test configuration using temporary repositories, fake Worker state, and a fake browser adapter/transport;
- keep generated runtime/evidence fully outside tracked source or covered by `.gitignore`;
- allow core, Web UI, Bridge, dispatcher, response consumer, Decision Guard, and router tests to run on another Windows host and, where practical, macOS/Linux;
- document how to clone/bootstrap the repository on a new machine without XLabServer access;
- preserve a separate explicit real-integration profile for LabDemo/XLabServer and deployed ChatGPT browser validation.

Acceptance target:
- a clean clone on another development computer can install dependencies and run the normal automated suite without XLabServer;
- no tracked configuration requires `D:\LabDemo\LabDemo`, `10.138.7.167`, or any other XLabServer-specific value;
- real LabDemo/XLabServer tests remain opt-in and clearly separated from portable CI/development tests.

Priority: backlog; schedule only with separate owner authorization.
