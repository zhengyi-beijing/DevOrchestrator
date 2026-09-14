# GitHub Copilot Repository Instructions

Follow `AGENTS.md`. For Planner, Reviewer, Adjudicator, Worker, remediation, or
other lifecycle changes, read the canonical `docs/development-workflow.md`.

Do not create an unbounded Planner/Reviewer loop. `NON_BLOCKING` findings must
not prevent implementation. Put deep correctness review after implementation,
then remediate concrete findings and run regression.
