# P10 Active-Project Progress Watchdog and Automatic Diagnostics

Status: **PENDING DESIGN**

Goal: make DevOrchestrator detect active projects that have exceeded a configurable no-progress threshold and automatically launch one bounded diagnostic task instead of silently remaining in a running state.

Required behavior:
- monitor only active lifecycle states such as PLANNING, EXECUTING, REVIEWING, and REMEDIATING;
- reuse existing durable progress signals (`last_activity_at`, lifecycle/role state, AIBroker dispatch state, Progress Channel events, Git/agent-file changes) rather than introducing a second heartbeat system;
- support a configurable default no-progress threshold (initial target 15 minutes) plus per-project/per-lifecycle overrides;
- on threshold breach, create exactly one diagnostic attempt for the current project/task/lifecycle run, with durable deduplication and cooldown so daemon ticks cannot fan out duplicate diagnostics;
- diagnostic execution is read-only by default and must inspect DevO lifecycle state, AIBroker dispatch/resource/quota state, relevant process liveness, Git truth, recent role output and agent files;
- return a structured diagnosis such as `healthy_slow`, `agent_stalled`, `process_dead`, `provider_or_quota_blocked`, `state_desync`, `external_wait`, or `unknown`, with evidence and recommended next action;
- permit only normal DevO lifecycle recovery (`retry`, `reconcile`, bounded remediation) when the diagnosis proves a safe recovery path; destructive Git actions, arbitrary process termination, credential changes, hardware actions, scope expansion or ambiguous recovery require OWNER_GATE;
- emit Progress Channel milestones for STALL_DETECTED, DIAGNOSTIC_STARTED, DIAGNOSTIC_RESULT, RECOVERY_STARTED and OWNER_GATE;
- persist last diagnosis, evidence hash, cooldown/attempt count and recovery result across restart and surface them through project status for the future 8770 dashboard.

Scope: modify only `C:\work\github\DevOrchestrator-dev`. Do not modify/restart/promote the stable controller at `C:\work\github\DevOrchestrator`, do not push, and perform no physical hardware actions.

Acceptance: focused watchdog/diagnostic/recovery regressions + full unittest suite + browser userscript syntax check + git diff check; update agent docs and commit locally clean; independent reviewer must accept before any later owner-gated promotion.
