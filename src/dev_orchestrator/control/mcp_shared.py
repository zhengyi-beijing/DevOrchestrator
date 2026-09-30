"""Shared helpers for Model Context Protocol adapters."""

from __future__ import annotations

import json
from typing import Any


def checked_tool_args(args: dict[str, Any], allowed: set[str], required: tuple[str, ...]) -> None:
    """Validate tool arguments against allowed set and required non-empty tuple."""
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ValueError(f"Unknown arguments: {', '.join(unknown)}")
    for name in required:
        if name not in args or args[name] is None or (isinstance(args[name], str) and not args[name].strip()):
            raise ValueError(f"missing required argument: {name}")


_MAX_TOOL_RESULT_BYTES = 65536


def format_tool_content(result: Any, max_bytes: int = _MAX_TOOL_RESULT_BYTES) -> str:
    """Serialize tool result to JSON with max byte ceiling and truncation marker."""
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    encoded = rendered.encode("utf-8")
    if len(encoded) > max_bytes:
        truncation_payload = {
            "error": "output_truncated",
            "message": f"Tool output exceeded limit of {max_bytes} bytes...[OUTPUT_TRUNCATED]",
            "max_bytes": max_bytes,
        }
        return json.dumps(truncation_payload, ensure_ascii=False, indent=2)
    return rendered


def format_tool_result_dict(result: Any, is_error: bool = False, max_bytes: int = _MAX_TOOL_RESULT_BYTES) -> dict[str, Any]:
    """Return standard MCP tool execution content block."""
    return {
        "content": [{"type": "text", "text": format_tool_content(result, max_bytes=max_bytes)}],
        "isError": is_error,
    }
