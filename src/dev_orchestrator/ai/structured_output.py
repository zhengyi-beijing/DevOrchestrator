"""Shared structured-output protocol helpers for AI role responses."""
from __future__ import annotations

import json
from typing import Any, Iterable


class StructuredOutputError(ValueError):
    """Structured-output contract failure with a machine-readable stage."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _strip_exact_json_fence(text: str, *, label: str) -> str:
    candidate = text.strip()
    if not candidate.startswith("```"):
        return candidate
    lines = candidate.splitlines()
    if (
        len(lines) < 3
        or lines[0].strip().casefold() not in {"```", "```json"}
        or lines[-1].strip() != "```"
    ):
        raise StructuredOutputError("extract", f"{label} output must contain one JSON object")
    body = "\n".join(lines[1:-1]).strip()
    if "```" in body:
        raise StructuredOutputError("extract", f"{label} output must contain one JSON object")
    return body


def _top_level_object_spans(text: str) -> list[str]:
    spans: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start:index + 1])
                start = None
    return spans


def extract_unique_json_object(text: str | None, *, label: str = "AI") -> dict[str, Any]:
    """Extract exactly one unambiguous JSON object from model output."""
    if not isinstance(text, str) or not text.strip():
        raise StructuredOutputError("extract", f"{label} output is empty")
    candidate = _strip_exact_json_fence(text, label=label)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        decoded: list[dict[str, Any]] = []
        for span in _top_level_object_spans(candidate):
            try:
                value = json.loads(span)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                decoded.append(value)
        if len(decoded) != 1:
            raise StructuredOutputError(
                "extract", f"{label} output must contain exactly one JSON object"
            )
        payload = decoded[0]
    if not isinstance(payload, dict):
        raise StructuredOutputError("schema", f"{label} JSON root must be an object")
    return payload


def require_exact_keys(payload: dict[str, Any], keys: Iterable[str], *, label: str) -> None:
    expected = set(keys)
    if set(payload) != expected:
        raise StructuredOutputError(
            "schema",
            f"{label} JSON must contain exactly {', '.join(sorted(expected))}",
        )


def protocol_repair_prompt(
    *,
    raw_output: str,
    failure: str,
    schema_example: str,
    label: str,
) -> str:
    """Create a narrow format-only repair prompt."""
    return (
        f"You are repairing only the machine-readable format of a prior {label} output. "
        "Do not inspect or modify repository files. Do not reconsider, strengthen, weaken, "
        "or replace the prior technical judgment. Preserve its decision and reason exactly "
        "in meaning. Convert only the prior output into the required JSON schema. "
        "Return exactly one JSON object and no markdown, prose, or code fences.\n\n"
        f"Required schema example:\n{schema_example}\n\n"
        f"Protocol failure:\n{failure[:1000]}\n\n"
        "Prior raw output:\n---BEGIN RAW OUTPUT---\n"
        + raw_output
        + "\n---END RAW OUTPUT---\n"
    )
