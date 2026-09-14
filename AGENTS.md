# Project Agent Instructions

## graphify

This project has a knowledge graph at `graphify-out/` with god nodes,
community structure, and cross-file relationships.

- For codebase questions, first run `graphify query "<question>"` when
  `graphify-out/graph.json` exists. Use `graphify path "<A>" "<B>"` for
  relationships and `graphify explain "<concept>"` for focused concepts.
- Dirty `graphify-out/` files are expected after hooks or incremental updates;
  they are not a reason to skip Graphify. Skip it only for stale/incorrect graph
  investigations or when the user explicitly says not to use it.
- Prefer `graphify-out/wiki/index.md` for broad navigation when it exists. Read
  `graphify-out/GRAPH_REPORT.md` only for broad architecture review or when a
  scoped query/path/explain does not provide enough context.
- After modifying code, run `graphify update .` to keep the graph current.

## Development Workflow / Review Policy

Before changing DevOrchestrator lifecycle, Planner, Reviewer, Adjudicator,
Worker, or remediation behavior, read and follow
`docs/development-workflow.md`, the canonical workflow policy.

- Plan Review decides whether a plan is safe and clear enough to execute; it
  does not require all implementation details to be closed first.
- Classify findings as `BLOCKING` or `NON_BLOCKING`. A non-blocking finding must
  not by itself reject or delay implementation.
- Reserve blocking status for architecture, data-loss/durability, irreversible
  public-interface, security/permission, source-of-truth, core-contract, or
  verification-path failures.
- Keep plan remediation bounded to at most two rounds by default. After the
  budget, adjudicate only the unresolved blocking delta; never restart an
  unbounded full-plan loop.
- Adjudication is read-only and must not edit the repository, redesign the task,
  or rewrite the full plan.
- Resolve local schema, naming, exception, edge-case, test, and implementation
  choices in Worker implementation and tests when safe.
- Put deep correctness review after implementation. Technical Review findings
  normally go directly to remediation and regression, not back to full planning.
