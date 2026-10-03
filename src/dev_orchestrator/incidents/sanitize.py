"""Ingress secret sanitization, credential redaction, and bounded evidence reference builder."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

from dev_orchestrator.control.logs import redact_secrets
from dev_orchestrator.storage.json_store import utc_now_iso

_EMBEDDED_ASSIGNMENT_RE = re.compile(
    r"\b((?:[A-Za-z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|BEARER|CREDENTIAL)[A-Za-z0-9_]*))\s*=\s*['\"]?([A-Za-z0-9._~+/-]+)['\"]?",
    re.IGNORECASE,
)
_MAX_PROMPT_CHARS = 1024
_TRUNCATED_MARKER = "...[TRUNCATED]"


def sanitize_incident_payload(value: Any) -> Any:
    """Recursively sanitize data before persistence into incident packets, index, journal, or candidates.

    Redacts API keys, Bearer tokens, passwords, private keys, embedded assignments, and truncates full
    prompt or environment dumps.
    """
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            key_lower = key_str.lower()
            if key_lower in ("prompt", "user_prompt", "system_prompt", "full_prompt"):
                # Bound prompt length and redact embedded secrets
                text_val = str(v)
                if len(text_val) > _MAX_PROMPT_CHARS:
                    prefix_len = _MAX_PROMPT_CHARS - len(_TRUNCATED_MARKER)
                    text_val = text_val[:prefix_len] + _TRUNCATED_MARKER
                result[key_str] = sanitize_incident_payload(text_val)
            elif key_lower in ("env", "environment", "env_dump"):
                # Nested environment dump: sanitize recursively and redact keys
                if isinstance(v, Mapping):
                    sub_env = {}
                    for ek, ev in v.items():
                        ek_lower = str(ek).lower()
                        if any(s in ek_lower for s in ("key", "token", "secret", "password", "auth", "cred")):
                            sub_env[str(ek)] = "[REDACTED]"
                        else:
                            sub_env[str(ek)] = sanitize_incident_payload(ev)
                    result[key_str] = sub_env
                else:
                    result[key_str] = sanitize_incident_payload(v)
            else:
                result[key_str] = sanitize_incident_payload(v)
        # Apply canonical redact_secrets on top
        return redact_secrets(result)

    if isinstance(value, list):
        return [sanitize_incident_payload(item) for item in value]

    if isinstance(value, str):
        # Redact secrets via canonical control logger
        cleaned = redact_secrets(value)
        # Redact embedded KEY=val assignments
        cleaned = _EMBEDDED_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", cleaned)
        return cleaned

    return value


def build_evidence_reference(
    occurrence_key: str,
    evidence: dict[str, Any] | None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Construct a bounded, sanitized evidence reference for incident families."""
    rec_time = recorded_at or utc_now_iso()
    ev_hash = None
    if isinstance(evidence, dict):
        sanitized_ev = sanitize_incident_payload(evidence)
        ev_hash = sanitized_ev.get("evidence_hash")
        if not ev_hash:
            canonical = json.dumps(sanitized_ev, sort_keys=True, separators=(",", ":"))
            ev_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    return {
        "occurrence_key": str(occurrence_key),
        "recorded_at": rec_time,
        "evidence_hash": ev_hash,
    }
