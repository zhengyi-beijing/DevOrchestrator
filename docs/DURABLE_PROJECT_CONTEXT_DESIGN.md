# Durable Project Context Foundation Design

Status: **V1 ACCEPTED DESIGN**

## Purpose

DevOrchestrator orchestrates AI agents across long-running development workflows.
Prior to this capability, project context lived primarily in ephemeral chat
transcripts or manual prompt copy-pasting. When conversations rolled over or new
tasks started, agents lacked durable awareness of overarching project goals,
architectural responsibilities, safety boundaries, and validation workflows.

Durable Project Context provides a first-class, project-scoped context mechanism
that survives conversations and restarts, injecting bounded, deterministic
context into AI Planner, Worker, and Reviewer prompts without relying on chat
memory.

## Required Context Domains

The context document format specifies seven canonical domains:

1. `goals`: High-level business and technical objectives of the project.
2. `architecture`: Module structure, component boundaries, and subsystem responsibilities.
3. `protected_scope`: Explicitly off-limits files, components, directories, and resources (e.g. stable controllers, external production databases).
4. `safety_constraints`: Inviolable operational rules (e.g., fail closed, no push without authorization, one daemon per host).
5. `validation_commands`: Exact commands to execute tests, linters, userscript syntax checks, and repository hygiene checks.
6. `runtime_assumptions`: Operating environment, supported Python versions, required tools, network endpoints, and ports.
7. `key_decisions`: Durable architectural decisions, accepted conventions, and foundational lifecycle choices.

## Versioned Document Schema (Schema Version 1)

Context documents are stored in JSON (default: `<repo>/agent/project-context.json`).

### Top-Level Document Structure

- `schema_version` (integer, required): Must equal `1`.
- `project_id` (string, optional): Matches configured project id for project isolation.
- `updated_at` (string ISO-8601, optional): Timestamp of last document revision.
- Domain keys (`goals`, `architecture`, `protected_scope`, `safety_constraints`, `validation_commands`, `runtime_assumptions`, `key_decisions`):
  - Each domain value must be a JSON array of non-blank strings.
  - At most 40 entries per domain.
  - Each entry must be at most 600 characters long.
  - No entries may contain forbidden secret markers.
  - Unknown top-level keys are rejected.

### Worked Example

```json
{
  "schema_version": 1,
  "project_id": "my-service",
  "updated_at": "2026-09-12T12:00:00+00:00",
  "goals": [
    "Provide resilient payment webhook processing with idempotent delivery."
  ],
  "architecture": [
    "src/service/webhooks.py handles HTTP ingress and signature validation.",
    "src/service/ledger.py manages transactional database records."
  ],
  "protected_scope": [
    "Production database credentials and production API tokens.",
    "The stable controller daemon running at /opt/service-controller."
  ],
  "safety_constraints": [
    "Never bypass HMAC signature validation.",
    "Fail closed on invalid or unverified webhook events."
  ],
  "validation_commands": [
    "pytest tests/ -v",
    "flake8 src/ tests/"
  ],
  "runtime_assumptions": [
    "Python 3.11+ on Linux or Windows.",
    "Local PostgreSQL instance on port 5432."
  ],
  "key_decisions": [
    "Use PostgreSQL advisory locks for concurrent event deduplication."
  ]
}
```

## Configuration Declaration

Projects opt in via `projects.json`:

```json
{
  "project_id": "my-service",
  "repo_path": "../my-service",
  "project_context": {
    "enabled": true,
    "document_path": "agent/project-context.json",
    "supplement_path": "agent/generated-context.json",
    "require_valid": true,
    "max_chars": 6000,
    "inject_roles": ["planner", "worker", "reviewer"]
  }
}
```

### Configuration Fields

- `enabled` (boolean, default `false`): Enables context resolution and prompt injection.
- `document_path` (string, default `"agent/project-context.json"`): Repo-relative path to authoritative context document.
- `supplement_path` (string or `null`, default `null`): Optional repo-relative path to supplemental generated context.
- `require_valid` (boolean, default `true`): If true, an invalid context document blocks planner, worker, and reviewer dispatch.
- `max_chars` (integer, clamped 500..20000, default `6000`): Maximum character budget for the rendered context block.
- `inject_roles` (list of strings, default `["planner", "worker", "reviewer"]`): Non-empty subset of supported AI roles.

## Precedence: Declared-Authoritative vs. Supplemental

- The declared document (`document_path`) is **authoritative**.
- The supplement document (`supplement_path`) is **supplemental only**.
- Precedence rule: For each domain, if the declared document contains one or more entries, the declared entries are used and marked with provenance `"declared"`. If the declared domain is empty, supplemental entries (if any) are used and marked with provenance `"generated"`.
- Supplemental context **never** overrides declared entries.
- A missing or unreadable supplement degrades to declared-only context (`supplement_used: false`).

## Fail-Closed Rules and Reasons

When `enabled` is `true` and `require_valid` is `true`, invalid context halts actuation immediately:

- Unknown top-level keys: `unknown top-level keys: [...]`
- Unsupported schema version: `unsupported project context schema_version`
- Missing required domains: `missing required context domains: [...]`
- Path escape attempt: `document path escapes repository root` or `document path must be relative to repository`
- Drive specification in path: `document path must not contain a drive specification`
- Project id mismatch: `project_id mismatch: expected '...', found '...'`
- Domain entry count exceeded: `domain '...' exceeds 40 entries`
- Entry length exceeded: `domain '...' entry exceeds 600 characters`
- Secret markers detected: `domain '...' entry contains forbidden secret marker '...'`
- Unparseable JSON / OS read error: `cannot read context document: ...` or `invalid JSON in context document: ...`

When invalid and `require_valid` is `true`:
- `AIPlannerCoordinator.start()` refuses planning with `(None, "durable project context is invalid: <reason>")`.
- `TransitionExecutor._launch()` records execution as `blocked` with `"durable project context is invalid: <reason>"`.
- `AIReviewerCoordinator.advance()` records review as `failed` with `"durable project context is invalid: <reason>"`.

## Role-Bounded Injection Contract

When context is ready and the role is listed in `inject_roles`, a deterministic block is rendered and appended to role prompts:

```text
[PROJECT_CONTEXT_BEGIN]
schema_version=1 digest=d41d8cd98f00b204 provenance={"architecture": "declared", ...}

## goals
- Provide resilient payment webhook processing...

## architecture (supplemental, unverified)
- src/service/webhooks.py handles HTTP ingress...

[PROJECT_CONTEXT_END]
```

If the rendered text would exceed `max_chars`, rendering truncates cleanly at an entry boundary with:
```text
[PROJECT_CONTEXT_TRUNCATED]
[PROJECT_CONTEXT_END]
```

The block is injected cleanly:
- **Planner**: Appended between planning instructions/metadata and `Current agent/next.md`.
- **Plan Reviewer**: Appended between instructions/metadata and `Original task`.
- **Worker / Remediator**: Appended to `worker_prompt`.
- **Reviewer**: Appended after decision contract and git truth lines.

All JSON output schemas, decision tokens (`next`, `remediate`, `owner_gate`, `stop`), and parsers remain byte-identical.

## Project Isolation and Secret Rules

1. **Path Containment**: Paths are resolved with `Path.resolve()` and validated against `repo.resolve()`. Absolute paths, drive qualifications, and traversal outside the repository are rejected.
2. **Project ID Isolation**: When `project_id` is specified in the context JSON, it must match the project id in `projects.json`.
3. **Secret Markers**: Entries are scanned case-insensitively for forbidden markers:
   `password`, `api_key`, `api key`, `secret`, `token`, `bearer `, `client_secret`, `private_key`, `-----BEGIN`. Any match fails validation closed.
4. **Metadata Isolation**: Status payloads expose only schema version, state, reason, digest, entry counts, and path info—never raw prompt text or credentials.

## Status and Metadata Surfaces

Context status is surfaced across:
- Project snapshots: Attached as `snapshot["project_context"]`.
- `runtime/summary.json` and `runtime/projects/<id>.json`: Consumed by existing `/api/summary` and `/api/projects/<id>` dashboard endpoints.
- `.devorch/status.json`: Persisted in project-local mirrors.
- Read-only CLI:
  ```powershell
  python -m dev_orchestrator project-context --config .\config\projects.json --project-id my-project
  ```
  Returns JSON status metadata and exit code 0 on ready/absent, or exit code 1 on invalid.
- `validate-config`:
  ```powershell
  python -m dev_orchestrator validate-config --config .\config\projects.json
  ```
  Reports `project_context_state` and `project_context_error` per project.

## Graphify Relationship

Graphify is an optional external tool capable of analyzing codebases and producing dependency/structure graphs.
- DevOrchestrator **never** imports, bundles, runs, or requires Graphify at runtime.
- Any tool or pipeline may write a JSON file conforming to the context schema at `supplement_path`.
- If the file is missing or unreadable, DevOrchestrator operates normally using declared context alone.

## Future Schema Update Path (Schema Version 1 -> 2)

Future versions (e.g. `schema_version: 2`) may introduce structured domain objects (such as structured key decisions with rationale, alternatives considered, and date recorded).
- Unrecognized schema versions are rejected with `"unsupported project context schema_version"`.
- Backward-compatible migration tools or multi-version resolvers can branch cleanly on `schema_version`.
