"""Provider-independent, bounded workflow-policy prompt fragments.

The detailed source of truth is ``docs/development-workflow.md``.  Runtime
prompts deliberately carry only the role-specific invariants required for
unattended execution; they never read provider-native instruction files.
"""

from __future__ import annotations

from typing import Final

CANONICAL_WORKFLOW_PATH: Final = "docs/development-workflow.md"
WORKFLOW_POLICY_MAX_CHARS: Final = 1600

_ROLE_LINES: Final[dict[str, tuple[str, ...]]] = {
    "planner": (
        "Produce a bounded plan that is safe and specific enough to execute.",
        "Define scope, key interfaces, acceptance criteria, risks, and a verification path.",
        "Do not try to close every local implementation detail before work begins.",
        "Leave naming, local schema choices, edge cases, and fixable exception details to Worker plus tests when safe.",
        "Escalate only a genuine architecture, durability, public-interface, security, ownership, core-contract, or verification blocker.",
    ),
    "plan_reviewer": (
        "Evaluate execution readiness, not formal completeness or whether the plan could be improved further.",
        "Classify every finding as BLOCKING or NON_BLOCKING.",
        "BLOCKING is reserved for architecture, data-loss, irreversible public-interface, security/permission, source-of-truth, core-contract, or no-verification-path failures.",
        "Naming, local schema details, edge cases, local exception behavior, test additions, and safely testable implementation choices are NON_BLOCKING by default.",
        "NON_BLOCKING findings must not reject or delay implementation; carry them into acceptance notes, Worker work, or Technical Review.",
        "Approve once a Worker can begin safely without guessing the core contract.",
    ),
    "adjudicator": (
        "Consider only the unresolved BLOCKING delta after the bounded plan-remediation budget is exhausted.",
        "Preserve all accepted and resolved plan content.",
        "Do not redesign the task or rewrite the full plan.",
        "Operate read-only: do not modify repository files, commit, push, or execute implementation.",
        "Return only a bounded delta or contract patch that resolves the disputed blocker.",
    ),
    "worker": (
        "Implement the approved executable direction and stay within task scope.",
        "Resolve NON_BLOCKING details through code, focused tests, and observed behavior.",
        "Do not return to planning for naming, local schema, edge-case, or implementation-strategy choices that can be tested safely.",
        "Escalate only a newly discovered genuine architecture, durability, public-interface, security, ownership, core-contract, or verification blocker.",
        "Use implementation and evidence to converge the remaining details.",
    ),
    "technical_reviewer": (
        "Perform deep correctness review against the implemented code, tests, behavior, and acceptance criteria.",
        "Inspect edge cases, concurrency, durability, corruption, compatibility, regressions, and failure behavior as relevant.",
        "Send concrete fixable findings directly to bounded remediation followed by regression.",
        "Do not reopen full planning for ordinary correctness findings.",
        "Escalate to design only for a genuinely new architecture-level blocker.",
    ),
    "remediator": (
        "Fix the concrete Technical Review findings within the current task and approved direction.",
        "Use focused tests to reproduce and close each finding, then run relevant regression.",
        "Resolve local implementation details directly in code and tests.",
        "Do not rewrite the full plan or restart general design discussion.",
        "Escalate only if remediation reveals a genuinely new architecture-level blocker.",
    ),
}

WORKFLOW_POLICY_ROLES: Final = frozenset(_ROLE_LINES)


def workflow_policy_prompt(
    role: str, *, max_chars: int = WORKFLOW_POLICY_MAX_CHARS
) -> str:
    """Render one deterministic role policy with a hard size ceiling."""
    if role not in _ROLE_LINES:
        raise ValueError(f"unsupported workflow policy role: {role!r}")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 128:
        raise ValueError("max_chars must be an integer of at least 128")
    lines = [
        f"[WORKFLOW_POLICY role={role}]",
        f"Canonical policy: {CANONICAL_WORKFLOW_PATH}",
        "Enough design to execute safely; use implementation and evidence to resolve the rest.",
        *(_ROLE_LINES[role]),
        "[/WORKFLOW_POLICY]",
    ]
    rendered = "\n".join(lines)
    if len(rendered) > max_chars:
        raise ValueError(
            f"workflow policy for {role} exceeds the {max_chars}-character limit"
        )
    return rendered


def inject_workflow_policy(
    prompt: str, role: str, *, max_chars: int = WORKFLOW_POLICY_MAX_CHARS
) -> str:
    """Append the role policy exactly once without provider-specific behavior."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a nonblank string")
    policy = workflow_policy_prompt(role, max_chars=max_chars)
    if prompt.rstrip().endswith(policy):
        return prompt
    return prompt.rstrip() + "\n\n" + policy
