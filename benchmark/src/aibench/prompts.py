"""Task definitions and immutable prompt generation for aibench."""
from __future__ import annotations

import json
from .contracts import (
    BenchmarkTask,
    FrozenPrompt,
    ROLE_CLASSES,
    canonical_json,
    sha256_bytes,
)

COMMON_RETRIEVAL_INSTRUCTION = """[TOOL DISCOVERY & VERIFICATION DIRECTIVE]
1. Initial Discovery: When '.aibench/tools/zvec-grep' is present in this workspace, you may invoke it for initial codebase discovery. When absent, use native search tools (grep, find, read).
2. Ground Truth & Exact Reads: Regardless of tool, ALWAYS verify citations through exact source file reads. Never cite unverified memory.
3. Response Schema: You must output ONLY a valid JSON object conforming strictly to the requested schema. No conversational wrapper or markdown preamble outside the JSON block.
"""

_TASK_ARCH_PROMPT = """Analyze the architecture ownership boundaries and module dependencies across this repository.
Identify the domain and responsible team for every major module under 'src/synth_app/'.
Cite exact files and line spans for each ownership claim.
Describe the dependency flow between handlers, services, and repository layers.
State which modules provide cross-cutting foundation without downstream dependencies.
"""

_TASK_COMPAT_PROMPT = """Review the legacy v1 API compatibility layer and rationale.
Inspect 'src/synth_app/compat/legacy_api.py' and 'docs/compatibility.md'.
Identify all parameter mappings from v1 query keys to canonical v2 schema keys.
Verify whether deprecation warnings are emitted and identify the deprecation timeline/horizon.
Cite the exact source files and line spans proving these compatibility paths and rationale.
"""

_TASK_CACHE_PROMPT = """Implement the missing 'get_or_set' method on 'LRUCache' in 'src/synth_app/core/cache.py'.
The signature must be:
    def get_or_set(self, key: str, default_fn: Callable[[], Any], ttl: float | None = None) -> Any
It must check if 'key' exists and is not expired; if so, return the cached value.
Otherwise, it must call 'default_fn()', store the result under 'key' with optional 'ttl', and return that value.
Ensure 'tests/test_cache.py' passes completely. Provide your solution as a unified diff patch in the 'patch' field.
Cite the exact source file and line spans where your changes apply.
"""

_TASK_DEBUG_PROMPT = """Diagnose the root cause of the cross-file defect reproduced in 'tests/test_api.py::test_reproduce_cross_file_currency_defect'.
The client passes 'currency': 'USD', but the application fails with 'CurrencyConversionError: unsupported currency None'.
Trace the parameter flow across:
  - 'src/synth_app/handlers/api.py'
  - 'src/synth_app/services/billing.py'
  - 'src/synth_app/repository/account_repo.py'
Identify the exact root-cause file and line span where the bug originates, explain why 'None' is passed, and recommend a fix.
Cite exact source files and line spans for each step in the propagation path.
"""

_SCHEMA_ARCH = {
    "type": "object",
    "required": ["summary", "ownership_matrix", "dependency_flow", "citations"],
    "properties": {
        "summary": {"type": "string"},
        "ownership_matrix": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["domain", "team", "file"],
                "properties": {
                    "domain": {"type": "string"},
                    "team": {"type": "string"},
                    "file": {"type": "string"},
                },
            },
        },
        "dependency_flow": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "start_line", "end_line"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
            },
        },
    },
}

_SCHEMA_COMPAT = {
    "type": "object",
    "required": ["summary", "parameter_mappings", "deprecation_horizon", "findings", "citations"],
    "properties": {
        "summary": {"type": "string"},
        "parameter_mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["v1", "v2"],
                "properties": {
                    "v1": {"type": "string"},
                    "v2": {"type": "string"},
                },
            },
        },
        "deprecation_horizon": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "start_line", "end_line"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
            },
        },
    },
}

_SCHEMA_WORKER = {
    "type": "object",
    "required": ["summary", "patch", "citations"],
    "properties": {
        "summary": {"type": "string"},
        "patch": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "start_line", "end_line"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
            },
        },
    },
}

_SCHEMA_DEBUG = {
    "type": "object",
    "required": ["summary", "root_cause_file", "root_cause_line_span", "propagation_path", "fix_recommendation", "citations"],
    "properties": {
        "summary": {"type": "string"},
        "root_cause_file": {"type": "string"},
        "root_cause_line_span": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        "propagation_path": {"type": "array", "items": {"type": "string"}},
        "fix_recommendation": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "start_line", "end_line"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
            },
        },
    },
}


def get_canonical_tasks() -> tuple[BenchmarkTask, ...]:
    """Return the four frozen canonical benchmark tasks."""
    task_arch = BenchmarkTask(
        task_id="task_arch_ownership",
        role="planner",
        title="Architecture Ownership and Dependency Flow Analysis",
        description="Identify module ownership boundaries, teams, and flow in synth_app",
        prompt_template=_TASK_ARCH_PROMPT,
        timeout_seconds=300.0,
        response_schema=_SCHEMA_ARCH,
        rubric_ref="rubric_arch_v1",
        retrieval_sensitive=True,
        ground_truth={
            "expected_files": [
                "docs/architecture.md",
                "src/synth_app/core/auth.py",
                "src/synth_app/core/cache.py",
                "src/synth_app/services/billing.py",
                "src/synth_app/repository/account_repo.py",
                "src/synth_app/handlers/api.py",
                "src/synth_app/compat/legacy_api.py",
            ],
            "required_findings": [
                "Security Infrastructure Team",
                "Platform Performance Team",
                "Core Business Logic Team",
                "Data Layer Team",
                "Application API Gateway Team",
            ],
            "prohibited_findings": [
                "Payments Team",
                "DevOps Team",
                "Frontend Team",
                "unknown_team",
            ],
        },
    )

    task_compat = BenchmarkTask(
        task_id="task_compat_review",
        role="reviewer",
        title="Legacy API Compatibility and Deprecation Review",
        description="Verify legacy v1 parameter mapping, rationale, and deprecation timeline",
        prompt_template=_TASK_COMPAT_PROMPT,
        timeout_seconds=300.0,
        response_schema=_SCHEMA_COMPAT,
        rubric_ref="rubric_compat_v1",
        retrieval_sensitive=True,
        ground_truth={
            "expected_files": [
                "src/synth_app/compat/legacy_api.py",
                "docs/compatibility.md",
            ],
            "required_findings": [
                "acc_id",
                "val",
                "cur",
                "DeprecationWarning",
                "v3.0",
                "2027",
            ],
            "prohibited_findings": [
                "breaking change",
                "deleted endpoint",
            ],
        },
    )

    task_worker = BenchmarkTask(
        task_id="task_cache_worker",
        role="worker",
        title="Implement LRUCache.get_or_set Method",
        description="Implement missing get_or_set method to pass tests/test_cache.py",
        prompt_template=_TASK_CACHE_PROMPT,
        timeout_seconds=600.0,
        response_schema=_SCHEMA_WORKER,
        rubric_ref="rubric_worker_v1",
        retrieval_sensitive=False,
        ground_truth={
            "target_file": "src/synth_app/core/cache.py",
            "test_file": "tests/test_cache.py",
            "required_findings": [
                "get_or_set",
                "default_fn",
            ],
            "prohibited_findings": [],
        },
    )

    task_debug = BenchmarkTask(
        task_id="task_cross_file_debug",
        role="debugger",
        title="Cross-File Currency Defect Root Cause Diagnosis",
        description="Trace currency parameter across api, billing, and repo layers",
        prompt_template=_TASK_DEBUG_PROMPT,
        timeout_seconds=300.0,
        response_schema=_SCHEMA_DEBUG,
        rubric_ref="rubric_debug_v1",
        retrieval_sensitive=True,
        ground_truth={
            "expected_files": [
                "src/synth_app/handlers/api.py",
                "src/synth_app/services/billing.py",
                "src/synth_app/repository/account_repo.py",
            ],
            "root_cause_file": "src/synth_app/handlers/api.py",
            "required_findings": [
                "curr",
                "currency",
                "None",
                "TransactionHandler",
                "BillingService",
                "AccountRepository",
            ],
            "prohibited_findings": [
                "network timeout",
                "database down",
            ],
        },
    )

    return (task_arch, task_compat, task_worker, task_debug)


def render_task_prompt(task: BenchmarkTask) -> FrozenPrompt:
    """Render immutable byte-sequence prompt containing the task and directives."""
    schema_json = json.dumps(task.response_schema, indent=2)
    prompt_text = (
        f"{task.prompt_template.strip()}\n\n"
        f"{COMMON_RETRIEVAL_INSTRUCTION}\n"
        f"[EXPECTED_JSON_SCHEMA]\n{schema_json}\n"
    )
    # LF line endings for deterministic hashing
    normalized_text = prompt_text.replace("\r\n", "\n")
    pbytes = normalized_text.encode("utf-8")
    phash = sha256_bytes(pbytes)
    return FrozenPrompt(
        task_id=task.task_id,
        prompt_bytes=pbytes,
        prompt_hash=phash,
        prompt_text=normalized_text,
    )
