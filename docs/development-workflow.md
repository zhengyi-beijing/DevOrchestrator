# Development Workflow and Review Policy

This document is the canonical source of truth for DevOrchestrator development
workflow policy. Provider-native instruction files are intentionally thin and
point here; unattended DevO execution additionally injects bounded role policy
directly into prompts.

## Purpose

Plan Review is an execution-readiness gate, not a formal proof and not a demand
that every implementation detail be settled before coding. The threshold is:

> Enough design to execute safely; use implementation and evidence to resolve the rest.

The workflow protects architecture, durability, interfaces, permissions, and
verification without turning local engineering choices into an unbounded
Planner/Reviewer debate.

## Roles

### Planner

The Planner defines task scope, the implementation direction, key interfaces,
acceptance criteria, risks, exclusions, and a viable verification path. The plan
must be executable, but need not predetermine every name, local schema detail,
exception branch, edge case, or test fixture.

### Plan Reviewer

The Plan Reviewer checks whether a Worker can begin safely. Every finding is
classified as `BLOCKING` or `NON_BLOCKING`. The Reviewer must not reject merely
because a plan could be more detailed or aesthetically improved.

### Adjudicator

The Adjudicator handles only an unresolved blocking delta after the bounded
remediation budget is exhausted. It preserves accepted plan content, does not
redesign or rewrite the full plan, and returns only a bounded delta or contract
patch. Adjudication is read-only: it must not modify the repository or produce
implementation changes before approval/materialization.

### Worker

The Worker implements the executable direction. It resolves non-blocking details
with code, focused tests, integration evidence, and ordinary engineering
judgment rather than automatically returning to planning.

### Technical Reviewer

The Technical Reviewer performs the deep correctness review after code and tests
exist. This is the primary stage for detailed analysis of edge cases,
concurrency, durability, corruption, API compatibility, regressions, and failure
behavior.

### Remediator

The Remediator fixes concrete Technical Review findings, runs focused tests and
regression, and stays within the approved task direction. It does not restart a
full design cycle unless the evidence reveals a genuinely new architecture-level
blocker.

## Finding Classification

| Classification | Typical criteria | Plan consequence |
| --- | --- | --- |
| `BLOCKING` | Architecture ownership conflict; destructive persistence ambiguity or data-loss risk; irreversible or severely incompatible public API; security or permission boundary violation; source-of-truth conflict; core contract ambiguity that prevents a Worker choosing a safe direction; no viable verification path | Reject for a bounded remediation round, or adjudicate the remaining delta after the budget |
| `NON_BLOCKING` | Internal naming; local validation or schema details; small-scope exception behavior; edge cases; test coverage additions; implementation strategy that can be resolved safely and verified experimentally | Record as acceptance notes, Worker work, or Technical Review input; do not reject or delay implementation solely for this finding |

When uncertain, ask whether beginning implementation could reasonably cause an
architectural reversal, data loss, an irreversible external contract, a security
violation, or untestable behavior. If not, the finding is normally non-blocking.

## Bounded Plan Review

The normal path is:

```text
PLAN → PLAN_REVIEW → WORKER
```

A Plan Reviewer may reject only for a concrete blocking finding:

```text
PLAN → PLAN_REVIEW → PLAN_REMEDIATION → PLAN_REVIEW
```

Plan remediation defaults to at most two rounds. Any explicit override must
remain finite and bounded. It must never create an indefinite full-plan loop.
If a blocker remains after the budget, preserve resolved content and move only
the unresolved delta to bounded adjudication:

```text
PLAN_REMEDIATION_LIMIT → DELTA_ADJUDICATION → bounded contract patch or owner gate
```

The Adjudicator is not a second Planner. Its input contains the unresolved
blocker and the relevant accepted contract; its output contains only the delta
needed to resolve that blocker.

## Implementation-First Resolution

If a question can be resolved safely through code, a unit test, an integration
test, regression, or Technical Review, implementation should proceed. Local
ambiguity does not justify reopening planning when the core direction and
verification path are clear.

## Technical Review and Remediation

The standard evidence-producing flow is:

```text
Plan → Plan Review → Worker → Focused Tests → Technical Review
     → Remediation → Regression → DONE
```

Technical Review may be deep and skeptical. Concrete findings normally go
straight to remediation and regression. Reopen design only when implementation
evidence reveals a genuinely new blocking architecture, durability, interface,
security, ownership, contract, or verification problem.

## Context Propagation Contract

Ordinary documentation cannot be assumed to enter every provider's context.
Policy therefore propagates through four layers:

1. **DevO explicit prompt injection** — the provider-independent correctness
   contract for unattended Planner, Plan Reviewer, Worker, Remediator, Technical
   Reviewer, and any future Adjudicator execution.
2. **Provider-native project instructions** — `AGENTS.md`, `CLAUDE.md`,
   `GEMINI.md`, and `.github/copilot-instructions.md` cover direct CLI, IDE, and
   agent use according to each provider's discovery behavior.
3. **Canonical detailed policy** — this file owns the complete, durable rules.
4. **Staged task specifications** — files such as `agent/staged/P12.md` define
   task-specific scope and acceptance only; they do not duplicate this policy.

Provider-native files are defense in depth, not the unattended-execution
contract. Provider switching must not change workflow behavior or depend on a
previous session remembering the rules.

## Anti-Patterns

- An unbounded full-plan Planner/Reviewer loop.
- Rejecting one new implementation-level edge case per review round.
- Treating safely testable implementation details as architecture blockers.
- Having an Adjudicator redesign or rewrite the complete plan.
- Allowing an Adjudicator to edit the repository.
- Restarting full planning after every Technical Review finding.
- Losing workflow rules when switching providers or sessions.
- Assuming an ordinary document is automatically discovered by every provider.
- Maintaining divergent full workflow copies for individual providers.

Detailed policy changes belong primarily in this document. Thin provider entry
files and bounded runtime fragments should change only when their discovery or
minimum enforcement contract changes.
