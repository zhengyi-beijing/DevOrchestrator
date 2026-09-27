"""Closed-schema structured findings and role output parsing.

Eliminates prose parsing for severity; strictly differentiates absent findings
from malformed/invalid output (which becomes OUTPUT_INVALID).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from dev_orchestrator.convergence.problems import FailureClass


class FindingSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    NON_BLOCKING = "NON_BLOCKING"
    INFO = "INFO"


class OutputInvalidError(ValueError):
    """Raised when structured role output is malformed or invalid."""


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: FindingSeverity
    failure_class: FailureClass
    target: dict[str, Any]
    scope_claim: str
    machine_code: str
    human_explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity.value,
            "failure_class": self.failure_class.value,
            "target": dict(self.target),
            "scope_claim": self.scope_claim,
            "machine_code": self.machine_code,
            "human_explanation": self.human_explanation,
        }


def parse_finding_dict(raw: Mapping[str, Any]) -> Finding:
    """Validate a single finding dictionary against the closed schema."""
    if not isinstance(raw, Mapping):
        raise OutputInvalidError(f"Finding must be a mapping, got {type(raw).__name__}")

    fid = raw.get("finding_id")
    if not isinstance(fid, str) or not fid.strip():
        raise OutputInvalidError("finding_id must be a non-empty string")

    sev = raw.get("severity")
    if isinstance(sev, str):
        sev_clean = sev.strip().upper()
        if sev_clean not in FindingSeverity._value2member_map_:
            raise OutputInvalidError(f"Invalid finding severity: {sev!r}. Must be one of {list(FindingSeverity._value2member_map_)}")
        severity = FindingSeverity(sev_clean)
    else:
        raise OutputInvalidError(f"severity must be a string, got {type(sev).__name__}")

    fc = raw.get("failure_class")
    if isinstance(fc, str):
        fc_clean = fc.strip().upper()
        if fc_clean not in FailureClass._value2member_map_:
            raise OutputInvalidError(f"Invalid failure_class: {fc!r}. Must be one of {list(FailureClass._value2member_map_)}")
        failure_class = FailureClass(fc_clean)
    elif isinstance(fc, FailureClass):
        failure_class = fc
    else:
        raise OutputInvalidError(f"failure_class must be a string or FailureClass, got {type(fc).__name__}")

    target = raw.get("target")
    if not isinstance(target, Mapping):
        raise OutputInvalidError(f"target must be a mapping, got {type(target).__name__}")
    if "file" not in target:
        raise OutputInvalidError("target must specify 'file'")

    scope_claim = str(raw.get("scope_claim") or "task")
    machine_code = raw.get("machine_code")
    if not isinstance(machine_code, str) or not machine_code.strip():
        raise OutputInvalidError("machine_code must be a non-empty string")

    human_explanation = str(raw.get("human_explanation") or "")

    return Finding(
        finding_id=fid.strip(),
        severity=severity,
        failure_class=failure_class,
        target=dict(target),
        scope_claim=scope_claim,
        machine_code=machine_code.strip(),
        human_explanation=human_explanation,
    )


def parse_structured_findings(raw_output: Any) -> tuple[Finding, ...]:
    """Parse a sequence of structured findings.

    Returns a tuple of Finding objects.
    Raises OutputInvalidError on any malformation. Never silently treats malformed
    output as empty findings.
    """
    if raw_output is None:
        raise OutputInvalidError("Structured output is null/None")

    if isinstance(raw_output, Mapping):
        # Could be an envelope like {"findings": [...]} or a single finding
        if "findings" in raw_output:
            raw_output = raw_output["findings"]
        else:
            return (parse_finding_dict(raw_output),)

    if not isinstance(raw_output, (list, tuple)):
        raise OutputInvalidError(f"Structured findings must be a sequence, got {type(raw_output).__name__}")

    parsed = []
    for item in raw_output:
        parsed.append(parse_finding_dict(item))

    return tuple(parsed)
