"""AIResourceBroker client integration for benchmark execution and resource pinning."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from dev_orchestrator.ai.contracts import (
    AIRoleRequest,
    AIRoleResult,
    ResourceContext,
    ROLE_RESOURCE_FAILURES,
)
from dev_orchestrator.ai.execution_port import AIExecutionPort

from .contracts import (
    BrokerAttempt,
    ResourceSnapshot,
    canonical_json,
    sha256_bytes,
    utc_now_iso,
)


def _sanitize_resource_entry(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize resource metadata by omitting any potential secrets or local paths."""
    clean = {
        "resource_id": str(raw.get("resource_id", "")),
        "provider": str(raw.get("provider", "")),
        "account": str(raw.get("account", "")),
        "model": str(raw.get("model", "")),
        "backend": str(raw.get("backend_type", raw.get("backend", ""))),
        "enabled": bool(raw.get("enabled", True)),
    }
    # Carry non-secret metadata if present
    meta = raw.get("metadata")
    if isinstance(meta, Mapping):
        clean["dispatch_roles"] = list(meta.get("dispatch_roles", []))
        clean["dispatch_qualities"] = list(meta.get("dispatch_qualities", []))
    return clean


class BrokerBenchmarkClient:
    """Invokes AIBroker trials with exact-resource pinning and reliability chains."""

    def __init__(
        self,
        port: AIExecutionPort,
        config_path: Path | str | None = None,
        service_url: str | None = None,
    ) -> None:
        self.port = port
        self.config_path = Path(config_path) if config_path else None
        self.service_url = service_url

    def snapshot_resources(self, source_override: str | None = None) -> ResourceSnapshot:
        """Snapshot current available resources from broker service or resources YAML."""
        resources_list: list[dict[str, Any]] = []
        source_used = "unknown"

        # Try live broker service endpoint if available and not explicitly overridden
        if source_override != "resources_file" and self.service_url:
            try:
                req = urllib.request.Request(
                    self.service_url.rstrip("/") + "/api/resources",
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status == 200:
                        payload = json.loads(resp.read().decode("utf-8"))
                        if isinstance(payload, dict) and "resources" in payload:
                            raw_resources = payload["resources"]
                            if isinstance(raw_resources, list):
                                for r in raw_resources:
                                    if isinstance(r, dict) and r.get("enabled", True):
                                        resources_list.append(_sanitize_resource_entry(r))
                                source_used = "broker_api"
            except Exception:
                pass

        # Fall back to reading resources YAML file
        if not resources_list and self.config_path and self.config_path.is_file():
            try:
                content = self.config_path.read_text(encoding="utf-8")
                parsed = yaml.safe_load(content)
                if isinstance(parsed, dict) and "resources" in parsed:
                    for r in parsed["resources"]:
                        if isinstance(r, dict) and r.get("enabled", True):
                            resources_list.append(_sanitize_resource_entry(r))
                    source_used = "resources_file"
            except Exception:
                pass

        # Compute registry digest
        if self.config_path and self.config_path.is_file():
            raw_bytes = self.config_path.read_bytes().replace(b"\r\n", b"\n")
            registry_digest = sha256_bytes(raw_bytes)
        else:
            registry_digest = sha256_bytes(canonical_json(resources_list).encode("utf-8"))

        snapshot_id = f"snap_{registry_digest[:12]}"
        return ResourceSnapshot(
            snapshot_id=snapshot_id,
            source=source_used,
            registry_digest=registry_digest,
            timestamp=utc_now_iso(),
            resources=tuple(resources_list),
        )

    def execute_exact(
        self,
        target_resource_id: str,
        request: AIRoleRequest,
        snapshot: ResourceSnapshot,
    ) -> BrokerAttempt:
        """Dispatch request to exact target resource by excluding all other snapshot resources."""
        known_ids = {r["resource_id"] for r in snapshot.resources}
        if target_resource_id not in known_ids:
            raise ValueError(f"target resource {target_resource_id!r} not in snapshot resources")

        # Exclude all other resources from snapshot
        all_other_resources = sorted(list(known_ids - {target_resource_id}))
        pinned_request = replace(
            request,
            excluded_resource_ids=tuple(all_other_resources),
        )

        result = self.port.execute(pinned_request)

        # Enforce exact return resource identity
        if result.resource_context is not None and result.resource_context.resource_id:
            returned_id = result.resource_context.resource_id
            if returned_id != target_resource_id:
                raise RuntimeError(
                    f"exact resource mismatch: requested {target_resource_id!r} but broker selected {returned_id!r}"
                )

        return self._result_to_attempt(result)

    def execute_reliability_chain(
        self,
        request: AIRoleRequest,
        max_attempts: int = 3,
    ) -> tuple[BrokerAttempt, ...]:
        """Execute bounded reliability chain retrying only explicit provider/quota failures."""
        attempts: list[BrokerAttempt] = []
        excluded: list[str] = list(request.excluded_resource_ids)

        for attempt_idx in range(max_attempts):
            curr_req = replace(request, excluded_resource_ids=tuple(excluded))
            result = self.port.execute(curr_req)
            attempt = self._result_to_attempt(result)
            attempts.append(attempt)

            # If succeeded or error is not a retryable resource failure, stop
            if attempt.status == "succeeded":
                break
            if attempt.failure_classification not in ROLE_RESOURCE_FAILURES:
                break

            # If resource failed on quota/rate-limit/transient error, exclude it and retry
            if attempt.resource_id and attempt.resource_id not in excluded:
                excluded.append(attempt.resource_id)

        return tuple(attempts)

    @staticmethod
    def _result_to_attempt(result: AIRoleResult) -> BrokerAttempt:
        ctx = result.resource_context
        return BrokerAttempt(
            request_id=result.request_id,
            dispatch_id=result.dispatch_id,
            decision_id=result.decision_id,
            execution_id=result.execution_id,
            session_id=result.session_id,
            resource_id=ctx.resource_id if ctx else None,
            provider=ctx.provider if ctx else None,
            account=ctx.account if ctx else None,
            model=ctx.model if ctx else None,
            status=result.status,
            output=result.output,
            error=result.error,
            usage=dict(result.usage) if result.usage else None,
            usage_source=result.usage_source,
            started_at=result.started_at,
            finished_at=result.finished_at,
            first_output_at=result.first_output_at,
            quota_observation=dict(result.quota_observation) if result.quota_observation else None,
            rate_limit_observation=dict(result.rate_limit_observation) if result.rate_limit_observation else None,
            failure_classification=result.failure_classification,
        )


class MockExecutionPort(AIExecutionPort):
    """Standalone mock execution port for testing and simulated execution."""

    def __init__(self, available_resources: tuple[str, ...] | list[str] | None = None) -> None:
        self.recorded_requests: list[AIRoleRequest] = []
        self.next_result: AIRoleResult | None = None
        self.fail_then_succeed_target: str | None = None
        self._call_count = 0
        self.available_resources = list(available_resources) if available_resources else None

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.recorded_requests.append(request)
        self._call_count += 1

        if self.fail_then_succeed_target and self._call_count == 1:
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="failed",
                error="quota exceeded on primary resource",
                resource_context=ResourceContext(resource_id=self.fail_then_succeed_target),
                failure_classification="quota_exhausted",
            )

        if self.next_result is not None:
            res = self.next_result
            self.next_result = None
            return res

        # Default success echoing target resource if not excluded
        if self.available_resources:
            candidates = list(self.available_resources)
        elif any("/" in str(ex) for ex in request.excluded_resource_ids):
            candidates = [
                "agy/agy-1/gemini-3.8-flash-high",
                "copilot/default/claude-sonnet-4.6",
                "claude/default/opus",
            ]
        else:
            candidates = [
                "res_1",
                "res_2",
                "res_3",
                "res_4",
            ]

        selected_res = candidates[0]
        for candidate in candidates:
            if candidate not in request.excluded_resource_ids:
                selected_res = candidate
                break

        # Generate realistic output with valid citations and findings based on role
        if request.role == "planner":
            output = json.dumps({
                "summary": "Completed architecture ownership and dependency flow analysis for synth_app",
                "ownership_matrix": [
                    {"domain": "Security & Auth", "team": "Security Infrastructure Team", "file": "src/synth_app/core/auth.py"},
                    {"domain": "Performance / Caching", "team": "Platform Performance Team", "file": "src/synth_app/core/cache.py"},
                    {"domain": "Billing Logic", "team": "Core Business Logic Team", "file": "src/synth_app/services/billing.py"},
                    {"domain": "Data Layer", "team": "Data Layer Team", "file": "src/synth_app/repository/account_repo.py"},
                    {"domain": "API Gateway", "team": "Application API Gateway Team", "file": "src/synth_app/handlers/api.py"},
                ],
                "dependency_flow": "handlers/api.py -> services/billing.py -> repository/account_repo.py",
                "citations": [
                    {"path": "docs/architecture.md", "start_line": 1, "end_line": 17},
                ],
            })
        elif request.role == "reviewer":
            output = json.dumps({
                "summary": "Completed legacy API compatibility and deprecation review for synth_app",
                "parameter_mappings": [
                    {"v1": "acc_id", "v2": "account_id"},
                    {"v1": "val", "v2": "amount"},
                    {"v1": "cur", "v2": "currency"},
                ],
                "deprecation_horizon": "v3.0 scheduled for 2027 with DeprecationWarning",
                "findings": [
                    "V1 parameter mappings map acc_id, val, and cur to v2 schema fields",
                    "Emits DeprecationWarning when legacy query keys are used",
                    "Deprecation timeline horizon targets removal in v3.0 in 2027",
                ],
                "citations": [
                    {"path": "docs/compatibility.md", "start_line": 1, "end_line": 7},
                ],
            })
        elif request.role == "worker":
            patch = (
                "--- a/src/synth_app/core/cache.py\n"
                "+++ b/src/synth_app/core/cache.py\n"
                "@@ -69,4 +69,9 @@\n"
                "     # NOTE FOR BENCHMARK WORKER:\n"
                "     # get_or_set(self, key: str, default_fn: Callable[[], Any], ttl: float | None = None) -> Any\n"
                "     # is intentionally not implemented in this revision.\n"
                "+    def get_or_set(self, key: str, default_fn: Any, ttl: float | None = None) -> Any:\n"
                "+        val = self.get(key)\n"
                "+        if val is None:\n"
                "+            val = default_fn()\n"
                "+            self.set(key, val, ttl)\n"
                "+        return val\n"
            )
            output = json.dumps({
                "summary": "Implemented missing get_or_set method on LRUCache using default_fn callback",
                "patch": patch,
                "citations": [
                    {"path": "src/synth_app/core/cache.py", "start_line": 65, "end_line": 71},
                ],
            })
        elif request.role == "debugger":
            output = json.dumps({
                "summary": "Diagnosed root cause: TransactionHandler reads curr instead of currency resulting in None passed to BillingService and AccountRepository",
                "root_cause_file": "src/synth_app/handlers/api.py",
                "root_cause_line_span": [34, 36],
                "propagation_path": [
                    "TransactionHandler.handle_charge reads curr from payload and passes None currency",
                    "BillingService.process_charge receives None currency and delegates to AccountRepository",
                    "AccountRepository.charge_account raises CurrencyConversionError for unsupported currency None",
                ],
                "fix_recommendation": "Change payload.get('curr') to payload.get('currency', 'USD') in TransactionHandler.handle_charge",
                "citations": [
                    {"path": "src/synth_app/handlers/api.py", "start_line": 20, "end_line": 40},
                ],
            })
        else:
            output = json.dumps({"summary": "mock task output", "citations": []})

        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=output,
            resource_context=ResourceContext(resource_id=selected_res, provider="mock", account="acc", model="mod"),
            usage={"total_tokens": 120, "tool_calls": 1},
            usage_source="reported",
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
        )

    def status(self, request_id: str) -> dict[str, Any] | None:
        return {"status": "succeeded"}

    def interrupt(self, request_id: str, reason: str) -> dict[str, Any] | None:
        return {"status": "interrupted"}
