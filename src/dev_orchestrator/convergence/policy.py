"""Immutable Policy inputs for single-authority convergence.

Carries independent problem budgets, ordered capability tiers, quota-reset rules,
wait bounds, OUTPUT_INVALID repair bounds, deterministic problem ordering,
human-authorization boundaries, required sources, and deterministic policy_digest.
Decide() reads neither configuration nor environment; all policy is explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

DEFAULT_CAPABILITY_TIERS = (
    "tier_1_routine",
    "tier_2_standard",
    "tier_3_advanced",
    "tier_4_frontier",
)

DEFAULT_HUMAN_BOUNDARIES = (
    "ambiguous_product_requirements",
    "irreversible_operation",
    "hardware_actuation",
    "missing_credentials",
    "cost_limit_exceeded",
    "irreconcilable_identity",
)

DEFAULT_REQUIRED_SOURCES: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProblemBudget:
    max_resource_attempts: int = 3
    max_strategy_attempts: int = 2
    max_capability_escalations: int = 2
    max_total_attempts: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_resource_attempts": self.max_resource_attempts,
            "max_strategy_attempts": self.max_strategy_attempts,
            "max_capability_escalations": self.max_capability_escalations,
            "max_total_attempts": self.max_total_attempts,
        }


@dataclass(frozen=True)
class Policy:
    default_budget: ProblemBudget = field(default_factory=ProblemBudget)
    problem_budgets: dict[str, ProblemBudget] = field(default_factory=dict)
    capability_tiers: tuple[str, ...] = DEFAULT_CAPABILITY_TIERS
    quota_reset_rules: dict[str, Any] = field(default_factory=dict)
    wait_bounds: dict[str, Any] = field(default_factory=lambda: {"min_seconds": 5, "max_seconds": 3600})
    max_output_invalid_repairs: int = 2
    deterministic_problem_order: str = "stable_hash"
    human_authorization_boundaries: tuple[str, ...] = DEFAULT_HUMAN_BOUNDARIES
    required_sources: tuple[str, ...] = DEFAULT_REQUIRED_SOURCES
    policy_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_budget": self.default_budget.to_dict(),
            "problem_budgets": {k: b.to_dict() for k, b in self.problem_budgets.items()},
            "capability_tiers": list(self.capability_tiers),
            "quota_reset_rules": dict(self.quota_reset_rules),
            "wait_bounds": dict(self.wait_bounds),
            "max_output_invalid_repairs": self.max_output_invalid_repairs,
            "deterministic_problem_order": self.deterministic_problem_order,
            "human_authorization_boundaries": list(self.human_authorization_boundaries),
            "required_sources": list(self.required_sources),
        }

    def get_budget_for_problem(self, problem_id: str) -> ProblemBudget:
        return self.problem_budgets.get(problem_id, self.default_budget)


def build_policy(
    *,
    default_budget: ProblemBudget | None = None,
    problem_budgets: Mapping[str, ProblemBudget] | None = None,
    capability_tiers: Sequence[str] | None = None,
    quota_reset_rules: Mapping[str, Any] | None = None,
    wait_bounds: Mapping[str, Any] | None = None,
    max_output_invalid_repairs: int = 2,
    deterministic_problem_order: str = "stable_hash",
    human_authorization_boundaries: Sequence[str] | None = None,
    required_sources: Sequence[str] | None = None,
) -> Policy:
    """Build an immutable Policy with a deterministic policy_digest."""
    d_budget = default_budget or ProblemBudget()
    p_budgets = {k: v for k, v in (problem_budgets or {}).items()}
    tiers = tuple(capability_tiers) if capability_tiers is not None else DEFAULT_CAPABILITY_TIERS
    q_rules = dict(quota_reset_rules or {})
    w_bounds = dict(wait_bounds or {"min_seconds": 5, "max_seconds": 3600})
    h_bounds = tuple(human_authorization_boundaries) if human_authorization_boundaries is not None else DEFAULT_HUMAN_BOUNDARIES
    r_sources = tuple(required_sources) if required_sources is not None else DEFAULT_REQUIRED_SOURCES

    raw_dict = {
        "default_budget": d_budget.to_dict(),
        "problem_budgets": {k: b.to_dict() for k, b in sorted(p_budgets.items())},
        "capability_tiers": list(tiers),
        "quota_reset_rules": q_rules,
        "wait_bounds": w_bounds,
        "max_output_invalid_repairs": max_output_invalid_repairs,
        "deterministic_problem_order": deterministic_problem_order,
        "human_authorization_boundaries": list(h_bounds),
        "required_sources": list(r_sources),
    }
    c_json = json.dumps(raw_dict, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(c_json.encode("utf-8")).hexdigest()

    return Policy(
        default_budget=d_budget,
        problem_budgets=p_budgets,
        capability_tiers=tiers,
        quota_reset_rules=q_rules,
        wait_bounds=w_bounds,
        max_output_invalid_repairs=max_output_invalid_repairs,
        deterministic_problem_order=deterministic_problem_order,
        human_authorization_boundaries=h_bounds,
        required_sources=r_sources,
        policy_digest=digest,
    )
