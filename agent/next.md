# P16.11 AGY-First AI Resource Pool Benchmark & Routing

Status: **PENDING DESIGN**

Goal: quantify how much DevO workload can be completed by the three near-zero marginal-cost AGY accounts, then use measured evidence to design an AGY-first model-routing policy without sacrificing reviewer independence or lifecycle reliability.

- Treat the three AGY accounts as a shared quota/time-window constrained compute pool, not fixed Planner/Reviewer/Worker identities.
- Benchmark AGY by DevO role and real historical/replay task class: Planner, Worker/Implementer, Debugger/Root-cause Analyst, evidence packaging, and review where independence permits.
- Measure AGY coverage: tasks completed by AGY and accepted by an independent reviewer / total eligible tasks.
- Record first-pass acceptance, paid-model escalation rate, repeated-failure rate, median/p95 wall time, retries, quota/cooldown state, and successful-task cost.
- Compare AGY-first routing against representative Codex/Sol, Claude, Web Sol and other available resources under matched tasks and acceptance criteria.
- Prefer parallel use of the three AGY accounts on independent work; do not rotate accounts blindly against the same failure signature.
- On repeated equivalent failure, require a changed strategy/context or escalate to a heterogeneous model/provider rather than consuming another AGY account with the same approach.
- Keep final/high-risk review independent from the implementation path; free/low-cost execution must not weaken review, safety, Git, owner or lifecycle gates.
- Produce a machine-readable routing recommendation based on observed role/task success, availability and quota state rather than a static model ranking.

Acceptance:
1. All three AGY accounts are represented as schedulable pool resources with availability/quota/cooldown telemetry.
2. A representative replay set reports AGY coverage, independent-review acceptance, escalation rate, repeated-failure rate and wall time.
3. At least one AGY-first vs non-AGY baseline comparison is reproducible under matched task conditions.
4. Same-failure retry policy prevents blind AGY-account rotation and demonstrates heterogeneous escalation.
5. Results yield a documented routing policy identifying where AGY-first is supported, unsupported, or still uncertain.

Sequence: P16.10 automatic failure harvesting -> P16.11 AGY-first pool benchmark and evidence-based routing.
