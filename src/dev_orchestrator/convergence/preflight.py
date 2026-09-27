"""Deterministic capability preflight from learned environment constraints.

Rejects or rewrites incompatible operations before execution; detects learning regressions
when an already learned rule is violated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


class PreflightVerdict(str, Enum):
    ALLOW = "ALLOW"
    REWRITE = "REWRITE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class CapabilityConstraint:
    rule_id: str
    normalized_fingerprint: str
    environment_selector: dict[str, Any]
    operation_signature: dict[str, Any]
    action: PreflightVerdict
    rewrite_template: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    recurrence_count: int = 0
    last_verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "normalized_fingerprint": self.normalized_fingerprint,
            "environment_selector": dict(self.environment_selector),
            "operation_signature": dict(self.operation_signature),
            "action": self.action.value,
            "rewrite_template": self.rewrite_template,
            "evidence": dict(self.evidence),
            "created_at": self.created_at,
            "recurrence_count": self.recurrence_count,
            "last_verified_at": self.last_verified_at,
        }


def get_seeded_rules() -> tuple[CapabilityConstraint, ...]:
    """Return the representative ZXZ-PC seeded constraints."""
    return (
        # 1. PowerShell 5.1 && / || pipeline chaining
        CapabilityConstraint(
            rule_id="rule-ps51-pipeline-chaining",
            normalized_fingerprint="281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c",
            environment_selector={"os": "windows", "shell": "powershell_5.1"},
            operation_signature={"contains_tokens": ["&&", "||"]},
            action=PreflightVerdict.REJECT,
            rewrite_template="Use semicolon sequencing or explicit $LASTEXITCODE checks",
            evidence={
                "provenance": "seed:p11b:rdc-powershell-5.1",
                "symptom": "Windows PowerShell 5.1 command fails when containing && or ||",
            },
        ),
        # 2. utf8NoBOM unsupported on PS 5.1
        CapabilityConstraint(
            rule_id="rule-ps51-utf8-no-bom",
            normalized_fingerprint="a74b12398efc41098bca12984712093847012938470129837401928374019283",
            environment_selector={"os": "windows", "shell": "powershell_5.1"},
            operation_signature={"cmdlet": "Set-Content", "parameter": "-Encoding utf8NoBOM"},
            action=PreflightVerdict.REJECT,
            rewrite_template="Use utf8 encoding or python script for BOM-less UTF-8",
            evidence={"symptom": "Set-Content -Encoding utf8NoBOM is unsupported on Windows PowerShell 5.1"},
        ),
        # 3. npm .ps1 execution policy
        CapabilityConstraint(
            rule_id="rule-npm-ps1-exec-policy",
            normalized_fingerprint="b85c234091a141098bca12984712093847012938470129837401928374019284",
            environment_selector={"os": "windows", "tool": "npm"},
            operation_signature={"extension": ".ps1"},
            action=PreflightVerdict.REWRITE,
            rewrite_template="Use approved .cmd launcher or process-scope ExecutionPolicy Bypass",
            evidence={"symptom": "Script execution policy blocks npm .ps1 launchers"},
        ),
        # 4. Codex CLI flags capability discovery
        CapabilityConstraint(
            rule_id="rule-codex-flags-discovery",
            normalized_fingerprint="c96d345102b241098bca12984712093847012938470129837401928374019285",
            environment_selector={"tool": "codex"},
            operation_signature={"cli_flags": "unverified"},
            action=PreflightVerdict.REJECT,
            rewrite_template="Discover flags from codex --help before invoking",
            evidence={"symptom": "Codex CLI flags must be capability-discovered from installed version"},
        ),
        # 5. Shell redirection syntax
        CapabilityConstraint(
            rule_id="rule-shell-redirection-syntax",
            normalized_fingerprint="d07e456213c341098bca12984712093847012938470129837401928374019286",
            environment_selector={"os": "windows", "shell": "powershell"},
            operation_signature={"redirection": "bash_syntax_2>&1"},
            action=PreflightVerdict.REWRITE,
            rewrite_template="Use PowerShell redirection or Tee-Object",
            evidence={"symptom": "Shell redirection syntax must match PowerShell rather than Bash"},
        ),
        # 6. .NET file APIs absolute paths
        CapabilityConstraint(
            rule_id="rule-dotnet-apis-absolute-paths",
            normalized_fingerprint="e18f567324d441098bca12984712093847012938470129837401928374019287",
            environment_selector={"runtime": "dotnet_powershell"},
            operation_signature={"path_style": "relative"},
            action=PreflightVerdict.REWRITE,
            rewrite_template="Resolve to absolute path before passing to .NET API",
            evidence={"symptom": ".NET file APIs do not inherit PowerShell Set-Location working directory"},
        ),
    )


def evaluate_preflight(
    operation: Mapping[str, Any],
    environment: Mapping[str, Any],
    rules: Sequence[CapabilityConstraint] | None = None,
) -> tuple[PreflightVerdict, str, CapabilityConstraint | None]:
    """Evaluate an operation against loaded capability constraints.

    Returns (verdict, explanation, matched_rule).
    """
    active_rules = rules if rules is not None else get_seeded_rules()
    cmd_text = str(operation.get("command") or operation.get("text") or "")

    for rule in active_rules:
        # Check environment selector match
        env_match = True
        for env_k, env_v in rule.environment_selector.items():
            if environment.get(env_k) != env_v:
                env_match = False
                break
        if not env_match:
            continue

        # Check operation signature match
        tokens = rule.operation_signature.get("contains_tokens", [])
        for token in tokens:
            if token in cmd_text:
                return (
                    rule.action,
                    f"Command contains prohibited token {token!r} under {rule.rule_id} ({rule.rewrite_template})",
                    rule,
                )

        if "cmdlet" in rule.operation_signature and "parameter" in rule.operation_signature:
            cmdlet = rule.operation_signature["cmdlet"]
            param = rule.operation_signature["parameter"]
            if cmdlet in cmd_text and param in cmd_text:
                return (
                    rule.action,
                    f"Command contains prohibited parameter {param!r} for {cmdlet!r} under {rule.rule_id}",
                    rule,
                )

    return PreflightVerdict.ALLOW, "Operation conforms to all environment constraints", None


def classify_recurrence(
    executed_operation: Mapping[str, Any],
    environment: Mapping[str, Any],
    rules: Sequence[CapabilityConstraint],
) -> tuple[str, str]:
    """Classify the failure of an executed operation.

    If an applicable learned rule was loaded and the incompatible operation was
    nevertheless executed, emits LEARNING_REGRESSION / CONTROL_PLANE_DEFECT.
    """
    verdict, reason, rule = evaluate_preflight(executed_operation, environment, rules)
    if verdict in (PreflightVerdict.REJECT, PreflightVerdict.REWRITE) and rule is not None:
        return (
            "LEARNING_REGRESSION",
            f"Control-plane defect: Operation violating known learned rule {rule.rule_id!r} "
            f"(provenance {rule.evidence.get('provenance', 'unknown')}) was executed: {reason}",
        )
    return ("NEW_FAILURE", "Failure does not match existing learned constraints")
