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


PreflightClassification = str


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


PreflightRule = CapabilityConstraint


def derive_constraint_fingerprint(
    rule_id: str,
    environment_selector: Mapping[str, Any],
    operation_signature: Mapping[str, Any],
) -> str:
    """Compute deterministic SHA-256 fingerprint for a capability constraint rule."""
    raw = json.dumps(
        {
            "rule_id": rule_id,
            "environment_selector": dict(environment_selector),
            "operation_signature": dict(operation_signature),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_seeded_rules() -> tuple[CapabilityConstraint, ...]:
    """Return the representative ZXZ-PC seeded constraints."""
    # Seed 1: canonical lesson fingerprint preserved from provenance
    r1_env = {"os": "windows", "shell": "powershell_5.1"}
    r1_sig = {"contains_tokens": ["&&", "||"]}
    r1 = CapabilityConstraint(
        rule_id="rule-ps51-pipeline-chaining",
        normalized_fingerprint="281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c",
        environment_selector=r1_env,
        operation_signature=r1_sig,
        action=PreflightVerdict.REJECT,
        rewrite_template="Use semicolon sequencing or explicit $LASTEXITCODE checks",
        evidence={
            "provenance": "seed:p11b:rdc-powershell-5.1",
            "symptom": "Windows PowerShell 5.1 command fails when containing && or ||",
        },
    )

    # Seed 2: utf8NoBOM unsupported on PS 5.1
    r2_env = {"os": "windows", "shell": "powershell_5.1"}
    r2_sig = {"cmdlet": "Set-Content", "parameter": "-Encoding utf8NoBOM"}
    r2 = CapabilityConstraint(
        rule_id="rule-ps51-utf8-no-bom",
        normalized_fingerprint=derive_constraint_fingerprint("rule-ps51-utf8-no-bom", r2_env, r2_sig),
        environment_selector=r2_env,
        operation_signature=r2_sig,
        action=PreflightVerdict.REJECT,
        rewrite_template="Use utf8 encoding or python script for BOM-less UTF-8",
        evidence={"symptom": "Set-Content -Encoding utf8NoBOM is unsupported on Windows PowerShell 5.1"},
    )

    # Seed 3: npm .ps1 execution policy
    r3_env = {"os": "windows", "tool": "npm"}
    r3_sig = {"extension": ".ps1"}
    r3 = CapabilityConstraint(
        rule_id="rule-npm-ps1-exec-policy",
        normalized_fingerprint=derive_constraint_fingerprint("rule-npm-ps1-exec-policy", r3_env, r3_sig),
        environment_selector=r3_env,
        operation_signature=r3_sig,
        action=PreflightVerdict.REWRITE,
        rewrite_template="Use approved .cmd launcher or process-scope ExecutionPolicy Bypass",
        evidence={"symptom": "Script execution policy blocks npm .ps1 launchers"},
    )

    # Seed 4: Codex CLI flags capability discovery
    r4_env = {"tool": "codex"}
    r4_sig = {"cli_flags": "unverified"}
    r4 = CapabilityConstraint(
        rule_id="rule-codex-flags-discovery",
        normalized_fingerprint=derive_constraint_fingerprint("rule-codex-flags-discovery", r4_env, r4_sig),
        environment_selector=r4_env,
        operation_signature=r4_sig,
        action=PreflightVerdict.REJECT,
        rewrite_template="Discover flags from codex --help before invoking",
        evidence={"symptom": "Codex CLI flags must be capability-discovered from installed version"},
    )

    # Seed 5: Shell redirection syntax
    r5_env = {"os": "windows", "shell": "powershell"}
    r5_sig = {"redirection": "bash_syntax_2>&1"}
    r5 = CapabilityConstraint(
        rule_id="rule-shell-redirection-syntax",
        normalized_fingerprint=derive_constraint_fingerprint("rule-shell-redirection-syntax", r5_env, r5_sig),
        environment_selector=r5_env,
        operation_signature=r5_sig,
        action=PreflightVerdict.REWRITE,
        rewrite_template="Use PowerShell redirection or Tee-Object",
        evidence={"symptom": "Shell redirection syntax must match PowerShell rather than Bash"},
    )

    # Seed 6: .NET file APIs absolute paths
    r6_env = {"runtime": "dotnet_powershell"}
    r6_sig = {"path_style": "relative"}
    r6 = CapabilityConstraint(
        rule_id="rule-dotnet-apis-absolute-paths",
        normalized_fingerprint=derive_constraint_fingerprint("rule-dotnet-apis-absolute-paths", r6_env, r6_sig),
        environment_selector=r6_env,
        operation_signature=r6_sig,
        action=PreflightVerdict.REWRITE,
        rewrite_template="Resolve to absolute path before passing to .NET API",
        evidence={"symptom": ".NET file APIs do not inherit PowerShell Set-Location working directory"},
    )

    return (r1, r2, r3, r4, r5, r6)


load_seeded_preflight_rules = get_seeded_rules


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

        # 1. contains_tokens matcher
        tokens = rule.operation_signature.get("contains_tokens", [])
        for token in tokens:
            if token in cmd_text:
                return (
                    rule.action,
                    f"Command contains prohibited token {token!r} under {rule.rule_id} ({rule.rewrite_template})",
                    rule,
                )

        # 2. cmdlet + parameter matcher
        if "cmdlet" in rule.operation_signature and "parameter" in rule.operation_signature:
            cmdlet = rule.operation_signature["cmdlet"]
            param = rule.operation_signature["parameter"]
            if cmdlet.lower() in cmd_text.lower() and param.lower() in cmd_text.lower():
                return (
                    rule.action,
                    f"Command contains prohibited parameter {param!r} for {cmdlet!r} under {rule.rule_id}",
                    rule,
                )

        # 3. extension matcher (e.g. .ps1 scripts in npm)
        if "extension" in rule.operation_signature:
            ext = rule.operation_signature["extension"]
            op_ext = operation.get("extension")
            script = str(operation.get("script") or "")
            if op_ext == ext or script.endswith(ext) or (ext in cmd_text and not cmd_text.endswith(".cmd")):
                return (
                    rule.action,
                    f"Operation uses prohibited extension {ext!r} under {rule.rule_id} ({rule.rewrite_template})",
                    rule,
                )

        # 4. cli_flags matcher (e.g. unverified codex flags)
        if "cli_flags" in rule.operation_signature:
            expected_flag_status = rule.operation_signature["cli_flags"]
            if expected_flag_status == "unverified":
                if operation.get("cli_flags") == "unverified" or operation.get("flags_verified") is False or ("flags" in operation and not operation.get("flags_verified")):
                    return (
                        rule.action,
                        f"Operation uses unverified CLI flags under {rule.rule_id} ({rule.rewrite_template})",
                        rule,
                    )

        # 5. redirection matcher (e.g. bash_syntax_2>&1)
        if "redirection" in rule.operation_signature:
            redir = rule.operation_signature["redirection"]
            if operation.get("redirection") == redir or (redir == "bash_syntax_2>&1" and "2>&1" in cmd_text):
                return (
                    rule.action,
                    f"Operation uses incompatible redirection syntax {redir!r} under {rule.rule_id} ({rule.rewrite_template})",
                    rule,
                )

        # 6. path_style matcher (e.g. relative path with .NET APIs)
        if "path_style" in rule.operation_signature:
            p_style = rule.operation_signature["path_style"]
            if p_style == "relative":
                path_val = operation.get("path") or operation.get("target_path")
                is_rel = operation.get("path_style") == "relative" or (
                    path_val and not str(path_val).startswith(("\\", "/")) and ":" not in str(path_val)
                )
                if is_rel:
                    return (
                        rule.action,
                        f"Operation uses relative path style under {rule.rule_id} ({rule.rewrite_template})",
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
