# P20.4 Run I preflight reviewer availability

Date: 2026-10-08

Run I (`p20-live-20261008-i`) also remained outside the measurement window.
After the weekly-limit classification repair and machine-local Planner routing
update, its Copilot Planner succeeded on the first attempt and produced a valid
P204.1 plan. The Planner dispatch was
`e1c8195f-178a-454d-8068-a9936ce98696`; execution was
`764a659d-2e67-4be6-8738-acad8ddbb482`.

The independent Plan Reviewer then returned `no_candidate`. This was correct:
provider independence excluded the Copilot Planner resource, and the freshly
restarted Broker had no non-Copilot reviewer health snapshot yet. The initial
control became blocked with `plan review failed: no eligible resource`. Run I
created no canonical soak job, run file, `started_at`, measurement clock,
reliability run, task evidence, or fault disposition.

The Broker service has now probed these already configured independent reviewer
resources as available:

- `agy/agy-2/claude-opus-4-6-thinking`;
- `agy/agy-3/claude-opus-4-6-thinking`;
- `agy/agy-2/gemini-3.8-flash-high-adjudicator`;
- `agy/agy-3/gemini-3.8-flash-high-adjudicator`.

The Run I isolated daemon was stopped through its exact PID/create-time-owned
normal stop path after preserving its evidence. The next attempt must use fresh
Run J run/project/permit/path/job/source-request identities and a new six-hour
clock.
