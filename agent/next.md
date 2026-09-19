# P14.5 Reviewer Harness & OpenCodeReview Adapter

Status: **PENDING DESIGN**

Goal: standardize code-review preparation, project rule enforcement, model delegation and structured findings without making any review engine or model the DevO lifecycle authority.

Scope:
- Introduce a provider-neutral ReviewerHarness boundary owned by DevO; OpenCodeReview is the first adapter/backend, not a hard architectural dependency.
- Use OpenCodeReview for deterministic diff/full-scan preparation, file selection, rule packs, review sessions, coverage and structured finding localization.
- Keep reviewer model selection in AIBroker by role/quota/cost policy.
- Support delegated semantic review while preserving independent build/test/static-analysis gates.
- Normalize findings into a durable DevO finding contract and run review as a P14 durable job.

Acceptance:
- A representative repository can run diff review and bounded full scan without RDC as the normal transport.
- Review can delegate to an AIBroker-selected model, persist structured findings/coverage and recover across transport interruption.
- ReviewerHarness supplies evidence only; DevO remains the sole lifecycle authority.

Design note: detailed executable design must be independently reviewed before implementation.
