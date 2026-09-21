import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator, PlannerProtocolError, _parse_plan, _parse_plan_review


class PlannerPromptContractTests(unittest.TestCase):
    def test_planner_prompt_declares_exact_sequence_bounds(self):
        record = {
            "project_id": "p1",
            "task_id": "P15",
            "branch": "main",
            "head": "abc123",
            "next_text": "# NEXT\nStatus: **PENDING DESIGN**",
        }
        prompt = AIPlannerCoordinator._planner_prompt(
            record,
            retry_reason="planner implementation_steps must be a non-empty bounded list",
            attempt=2,
            remediation={
                "round": 1,
                "rejection": "preserve the two review blockers",
                "prior_plan": {"task_id": "P15"},
                "prior_resource": None,
            },
        )
        self.assertIn("1-24 string items", prompt)
        self.assertIn("at most 1000 characters", prompt)
        self.assertIn("[PLAN_REMEDIATION]", prompt)
        self.assertIn("[PLANNER_RETRY]", prompt)


class PlanParserCompatibilityTests(unittest.TestCase):
    def test_named_sequence_object_is_canonicalized(self):
        payload = {
            "task_id": "P14.5",
            "summary": "bounded",
            "implementation_steps": {"step_1": "first", "step_2": "second"},
            "interfaces": ["stable"],
            "validation": ["tests"],
            "risks": ["bounded"],
            "out_of_scope": ["unrelated"],
        }
        parsed = _parse_plan(json.dumps(payload), "P14.5")
        self.assertEqual(parsed["implementation_steps"], ["step_1: first", "step_2: second"])

    def test_nested_named_sequence_still_fails_closed(self):
        payload = {
            "task_id": "P14.5",
            "summary": "bounded",
            "implementation_steps": {"step_1": {"nested": "invalid"}},
            "interfaces": ["stable"],
            "validation": ["tests"],
            "risks": ["bounded"],
            "out_of_scope": ["unrelated"],
        }
        with self.assertRaisesRegex(ValueError, "non-empty bounded list"):
            _parse_plan(json.dumps(payload), "P14.5")

    def test_unique_json_object_is_extracted_from_surrounding_prose(self):
        payload = {
            "task_id": "P14.6", "summary": "bounded",
            "implementation_steps": ["one"], "interfaces": ["stable"],
            "validation": ["tests"], "risks": ["bounded"],
            "out_of_scope": ["unrelated"],
        }
        text = "Planner result follows:\n" + json.dumps(payload) + "\nEnd result."
        self.assertEqual(_parse_plan(text, "P14.6"), payload)

    def test_multiple_json_objects_fail_closed_at_extract_stage(self):
        payload = {
            "task_id": "P14.6", "summary": "bounded",
            "implementation_steps": ["one"], "interfaces": ["stable"],
            "validation": ["tests"], "risks": ["bounded"],
            "out_of_scope": ["unrelated"],
        }
        with self.assertRaises(PlannerProtocolError) as ctx:
            _parse_plan(json.dumps(payload) + "\n" + json.dumps({"extra": True}), "P14.6")
        self.assertEqual(ctx.exception.stage, "extract")

    def test_schema_and_semantic_failures_are_distinct(self):
        base = {
            "task_id": "P14.6", "summary": "bounded",
            "implementation_steps": ["one"], "interfaces": ["stable"],
            "validation": ["tests"], "risks": ["bounded"],
            "out_of_scope": ["unrelated"],
        }
        bad_schema = dict(base)
        bad_schema["validation"] = []
        with self.assertRaises(PlannerProtocolError) as schema_ctx:
            _parse_plan(json.dumps(bad_schema), "P14.6")
        self.assertEqual(schema_ctx.exception.stage, "schema")
        bad_semantic = dict(base)
        bad_semantic["task_id"] = "P14.5"
        with self.assertRaises(PlannerProtocolError) as semantic_ctx:
            _parse_plan(json.dumps(bad_semantic), "P14.6")
        self.assertEqual(semantic_ctx.exception.stage, "semantic")


class PlanReviewParserTests(unittest.TestCase):
    def test_accepts_exact_json_fence(self):
        self.assertEqual(_parse_plan_review('```json\n{"decision":"approve","reason":"ok"}\n```'), ('approve', 'ok'))

    def test_extracts_unique_json_object_from_surrounding_prose(self):
        self.assertEqual(
            _parse_plan_review('Result:\n```json\n{"decision":"approve","reason":"ok"}\n```'),
            ("approve", "ok"),
        )

    def test_multiple_json_objects_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "exactly one JSON object"):
            _parse_plan_review(
                '{"decision":"approve","reason":"ok"}\n'
                '{"decision":"reject","reason":"other"}'
            )

    def test_rejects_nested_or_multiple_fences(self):
        with self.assertRaisesRegex(ValueError, "one JSON object"):
            _parse_plan_review('```json\n{"decision":"approve","reason":"ok"}\n```\n```')


class FakePort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            payload = {
                "task_id": request.task_run_id,
                "summary": "Freeze a bounded implementation plan.",
                "implementation_steps": ["Add the interface seam", "Implement the bounded backend change"],
                "interfaces": ["Keep the existing public ABI stable"],
                "validation": ["Run the focused tests", "Run the full regression"],
                "risks": ["Do not expand scope into unrelated backends"],
                "out_of_scope": ["No unrelated refactor"],
            }
            resource = ResourceContext("planner-r", "p1", "a1", "m1")
            return self._result(request, json.dumps(payload), resource, "plan")
        resource = ResourceContext("review-r", "p2", "a2", "m2")
        return self._result(request, json.dumps({"decision":"approve","reason":"bounded and testable"}), resource, "review")

    @staticmethod
    def _result(request, output, resource, suffix):
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=output,
            dispatch_id="dispatch-" + suffix,
            decision_id="decision-" + suffix,
            execution_id="execution-" + suffix,
            resource_context=resource,
        )


class FailOncePlannerPort(FakePort):
    def __init__(self):
        super().__init__()
        self.failed_once = False

    def execute(self, request):
        if request.role == "planner" and not self.failed_once:
            self.requests.append(request)
            self.failed_once = True
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="failed",
                error="claude did not emit a JSON result",
                dispatch_id="dispatch-bad",
                execution_id="execution-bad",
                resource_context=ResourceContext("bad-r", "p1", "a1", "m1"),
            )
        return super().execute(request)


class AlwaysFailPlannerPort(FakePort):
    def execute(self, request):
        self.requests.append(request)
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="failed",
            error="claude did not emit a JSON result",
            dispatch_id="dispatch-bad",
            execution_id="execution-bad",
            resource_context=ResourceContext("bad-r", "p1", "a1", "m1"),
        )


class PlannerResourceFailoverPort(FakePort):
    def __init__(self):
        super().__init__()
        self.planner_calls = 0

    def execute(self, request):
        if request.role == "planner":
            self.requests.append(request)
            self.planner_calls += 1
            if self.planner_calls == 1:
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="failed",
                    error="You've hit your session limit · resets 5:10pm (Asia/Shanghai)",
                    dispatch_id="dispatch-quota",
                    execution_id="execution-quota",
                    resource_context=ResourceContext("planner-opus", "anthropic", "default", "opus"),
                    failure_classification="quota_exhausted",
                )
            payload = {
                "task_id": request.task_run_id, "summary": "Failover plan",
                "implementation_steps": ["Step 1"], "interfaces": ["Interface 1"],
                "validation": ["Validation 1"], "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            return AIRoleResult(
                request_id=request.request_id, role_run_id=request.role_run_id,
                status="succeeded", output=json.dumps(payload),
                dispatch_id="dispatch-codex", execution_id="execution-codex",
                resource_context=ResourceContext("planner-codex", "codex", "default", "sol"),
            )
        return super().execute(request)


class LateQuotaPlannerFailoverPort(FakePort):
    """Two protocol failures, then quota failure, then a different resource succeeds."""

    def __init__(self):
        super().__init__()
        self.planner_calls = 0

    def execute(self, request):
        if request.role != "planner":
            return super().execute(request)
        self.requests.append(request)
        self.planner_calls += 1
        if self.planner_calls <= 2:
            return AIRoleResult(
                request_id=request.request_id, role_run_id=request.role_run_id,
                status="succeeded", output=json.dumps({
                    "task_id": request.task_run_id, "summary": "Malformed",
                    "implementation_steps": [], "interfaces": ["i"],
                    "validation": ["v"], "risks": ["r"], "out_of_scope": ["o"],
                }),
                resource_context=ResourceContext("planner-opus", "anthropic", "default", "opus"),
            )
        if self.planner_calls == 3:
            return AIRoleResult(
                request_id=request.request_id, role_run_id=request.role_run_id,
                status="failed", error="You've hit your session limit",
                resource_context=ResourceContext("planner-opus", "anthropic", "default", "opus"),
                failure_classification="quota_exhausted",
            )
        payload = {
            "task_id": request.task_run_id, "summary": "Recovered",
            "implementation_steps": ["Step 1"], "interfaces": ["Interface 1"],
            "validation": ["Validation 1"], "risks": ["Risk 1"],
            "out_of_scope": ["Scope 1"],
        }
        return AIRoleResult(
            request_id=request.request_id, role_run_id=request.role_run_id,
            status="succeeded", output=json.dumps(payload),
            resource_context=ResourceContext("planner-codex", "codex", "default", "sol"),
        )


class RecordingProgressChannel:
    def __init__(self):
        self.emissions = []

    def emit(self, project, milestone, *, task_id=None, occurrence_key=None, details=None, level=None, message=None):
        self.emissions.append({
            "project": project,
            "milestone": milestone,
            "task_id": task_id,
            "occurrence_key": occurrence_key,
            "details": details or {},
        })
        return None

    def register_project(self, project):
        pass


class RejectOnceReviewerPort:
    def __init__(self):
        self.requests = []
        self.review_count = 0
        self.plan_count = 0

    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            self.plan_count += 1
            payload = {
                "task_id": request.task_run_id,
                "summary": f"Plan version {self.plan_count}",
                "implementation_steps": ["Step 1", "Step 2"],
                "interfaces": ["Interface 1"],
                "validation": ["Validation 1"],
                "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            resource = ResourceContext(f"planner-r-{self.plan_count}", "p1", "a1", "m1")
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps(payload),
                dispatch_id=f"dispatch-plan-{self.plan_count}",
                decision_id=f"decision-plan-{self.plan_count}",
                execution_id=f"execution-plan-{self.plan_count}",
                resource_context=resource,
            )
        self.review_count += 1
        resource = ResourceContext(f"review-r-{self.review_count}", "p2", "a2", "m2")
        if self.review_count == 1:
            decision_payload = {
                "decision": "reject",
                "reason": "implementation steps lack error handling",
            }
        else:
            decision_payload = {
                "decision": "approve",
                "reason": "remediated error handling is acceptable",
            }
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps(decision_payload),
            dispatch_id=f"dispatch-review-{self.review_count}",
            decision_id=f"decision-review-{self.review_count}",
            execution_id=f"execution-review-{self.review_count}",
            resource_context=resource,
        )


class PlanReviewerProtocolRepairPort(FakePort):
    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            payload = {
                "task_id": request.task_run_id,
                "summary": "Protocol repair fixture",
                "implementation_steps": ["Step 1"],
                "interfaces": ["Interface 1"],
                "validation": ["Validation 1"],
                "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            return self._result(
                request, json.dumps(payload),
                ResourceContext("planner-r", "planner-provider", "a1", "m1"),
                "plan",
            )
        resource = ResourceContext("review-r", "review-provider", "a2", "m2")
        if request.metadata.get("protocol_repair"):
            return self._result(
                request,
                json.dumps({"decision": "approve", "reason": "same judgment, repaired format"}),
                resource,
                "review-repair",
            )
        return self._result(
            request,
            '{decision: "approve", reason: "same judgment, repaired format"}',
            resource,
            "review-invalid",
        )


class AlwaysRejectReviewerPort:
    def __init__(self):
        self.requests = []
        self.review_count = 0
        self.plan_count = 0

    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            self.plan_count += 1
            payload = {
                "task_id": request.task_run_id,
                "summary": f"Plan version {self.plan_count}",
                "implementation_steps": ["Step 1"],
                "interfaces": ["Interface 1"],
                "validation": ["Validation 1"],
                "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            resource = ResourceContext(f"planner-r-{self.plan_count}", "p1", "a1", "m1")
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps(payload),
                dispatch_id=f"dispatch-plan-{self.plan_count}",
                decision_id=f"decision-plan-{self.plan_count}",
                execution_id=f"execution-plan-{self.plan_count}",
                resource_context=resource,
            )
        self.review_count += 1
        resource = ResourceContext(f"review-r-{self.review_count}", "p2", "a2", "m2")
        decision_payload = {
            "decision": "reject",
            "reason": f"plan rejected at round {self.review_count - 1}",
        }
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps(decision_payload),
            dispatch_id=f"dispatch-review-{self.review_count}",
            decision_id=f"decision-review-{self.review_count}",
            execution_id=f"execution-review-{self.review_count}",
            resource_context=resource,
        )


class RecoveryAfterBoundPort:
    def __init__(self):
        self.requests = []
        self.review_count = 0
        self.plan_count = 0

    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            self.plan_count += 1
            recovered = "planner-a" in request.excluded_resource_ids
            resource_id = "planner-b" if recovered else "planner-a"
            payload = {
                "task_id": request.task_run_id,
                "summary": "Recovered plan" if recovered else f"Plan {self.plan_count}",
                "implementation_steps": ["Step 1"],
                "interfaces": ["Interface 1"],
                "validation": ["Validation 1"],
                "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps(payload),
                dispatch_id=f"dispatch-plan-{self.plan_count}",
                decision_id=f"decision-plan-{self.plan_count}",
                execution_id=f"execution-plan-{self.plan_count}",
                resource_context=ResourceContext(resource_id, "planner-provider", "default", resource_id),
            )
        self.review_count += 1
        decision = "approve" if self.review_count >= 3 else "reject"
        reason = "recovery plan is acceptable" if decision == "approve" else f"technical blocker {self.review_count}"
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({"decision": decision, "reason": reason}),
            dispatch_id=f"dispatch-review-{self.review_count}",
            decision_id=f"decision-review-{self.review_count}",
            execution_id=f"execution-review-{self.review_count}",
            resource_context=ResourceContext(f"review-r-{self.review_count}", "review-provider", "default", "review"),
        )


class FailReviewOnRemediationPort:
    def __init__(self):
        self.requests = []
        self.review_count = 0

    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            payload = {
                "task_id": request.task_run_id,
                "summary": "Plan",
                "implementation_steps": ["Step 1"],
                "interfaces": ["Interface 1"],
                "validation": ["Validation 1"],
                "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps(payload),
                dispatch_id="dispatch-plan",
                decision_id="decision-plan",
                execution_id="execution-plan",
                resource_context=ResourceContext("planner-r", "p1", "a1", "m1"),
            )
        self.review_count += 1
        if self.review_count == 1:
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps({"decision": "reject", "reason": "first attempt rejected"}),
                dispatch_id="dispatch-review-1",
                decision_id="decision-review-1",
                execution_id="execution-review-1",
                resource_context=ResourceContext("review-r-1", "p2", "a2", "m2"),
            )
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="failed",
            error="connection reset by peer",
            dispatch_id="dispatch-review-2",
            decision_id="decision-review-2",
            execution_id="execution-review-2",
            resource_context=None,
        )


class ReviewerTransportTimeoutFailoverPort(FakePort):
    def __init__(self):
        super().__init__()
        self.review_calls = 0

    def execute(self, request):
        if request.role == "planner":
            return super().execute(request)
        self.requests.append(request)
        self.review_calls += 1
        if self.review_calls == 1:
            raise RuntimeError("broker service invocation failed for http://127.0.0.1:8875/api/dispatch: timed out")
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({"decision": "approve", "reason": "fallback reviewer approved"}),
            dispatch_id="dispatch-review-fallback",
            decision_id="decision-review-fallback",
            execution_id="execution-review-fallback",
            resource_context=ResourceContext("reviewer-gemini", "agy", "agy-1", "gemini-pro"),
        )

    def status(self, request_id):
        if request_id.endswith(":reviewer"):
            return {
                "dispatch_id": "dispatch-review-websol",
                "request_id": request_id,
                "decision_id": "decision-review-websol",
                "execution_id": "execution-review-websol",
                "resource_id": "chatgpt/default/sol",
                "started_at": "2026-09-18T08:42:07+00:00",
                "finished_at": "2026-09-18T08:57:07+00:00",
                "status": "failed",
                "execution_status": "failed",
                "execution_finished_at": "2026-09-18T08:57:07+00:00",
                "execution_session_id": "session-websol",
                "execution_error": "execution timed out after 900.0 seconds",
                "provider": "chatgpt",
                "account": "default",
                "model": "sol",
            }
        return None


class ReviewerResourceFailoverPort:
    def __init__(self, failures: int = 1, classification: str | None = "quota_exhausted"):
        self.requests = []
        self.planner_calls = 0
        self.review_calls = 0
        self.failures = failures
        self.classification = classification

    def execute(self, request):
        self.requests.append(request)
        if request.role == "planner":
            self.planner_calls += 1
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps({
                    "task_id": request.task_run_id, "summary": "Original bounded plan",
                    "implementation_steps": ["Step 1"], "interfaces": ["Interface 1"],
                    "validation": ["Validation 1"], "risks": ["Risk 1"],
                    "out_of_scope": ["Scope 1"],
                }),
                dispatch_id="dispatch-plan", decision_id="decision-plan", execution_id="execution-plan",
                resource_context=ResourceContext("planner-r", "planner-provider", "planner-account", "planner-model"),
            )
        self.review_calls += 1
        resource = ResourceContext(
            f"reviewer-r-{self.review_calls}", f"provider-{self.review_calls}",
            f"account-{self.review_calls}", f"model-{self.review_calls}",
        )
        if self.review_calls <= self.failures:
            return AIRoleResult(
                request_id=request.request_id, role_run_id=request.role_run_id, status="failed",
                error="You've hit your usage limit; try again later.",
                dispatch_id=f"dispatch-review-{self.review_calls}",
                decision_id=f"decision-review-{self.review_calls}",
                execution_id=f"execution-review-{self.review_calls}", resource_context=resource,
                failure_classification=self.classification,
            )
        return AIRoleResult(
            request_id=request.request_id, role_run_id=request.role_run_id, status="succeeded",
            output=json.dumps({"decision": "approve", "reason": "same plan reviewed"}),
            dispatch_id=f"dispatch-review-{self.review_calls}",
            decision_id=f"decision-review-{self.review_calls}",
            execution_id=f"execution-review-{self.review_calls}", resource_context=resource,
        )


def make_repo(repo: Path) -> None:
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "next.md").write_text(
        "# P14 Example\n\nStatus: **PENDING DESIGN**\n\nGoal: implement the bounded change.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)


def wait_terminal(coordinator: AIPlannerCoordinator, plan_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        row = coordinator.state()["plans"].get(plan_id)
        if row and row.get("state") not in {"planning", "reviewing", "remediating", "applying"}:
            return row
        time.sleep(0.02)
    raise AssertionError("planner lifecycle did not become terminal")


class AIPlannerTests(unittest.TestCase):
    def test_plan_review_freezes_ready_to_run_commit(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "quality": "high", "review_quality": "high",
                    "review_independence": "resource",
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, reason = coordinator.start(project, snapshot, "command-1")
            self.assertEqual(plan_id, "ai_plan:command-1")
            self.assertIn("started", reason)
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual(row["planner_resource"]["resource_id"], "planner-r")
            self.assertEqual(row["review_resource"]["resource_id"], "review-r")
            self.assertEqual([request.role for request in port.requests], ["planner", "reviewer"])
            self.assertEqual(port.requests[1].independence, "resource")
            self.assertEqual(port.requests[1].previous_resource_context.resource_id, "planner-r")
            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            self.assertIn("Status: **READY_TO_RUN**", next_text)
            self.assertIn("## Approved executable design", next_text)
            status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True)
            self.assertEqual(status.stdout.strip(), "")
            log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--pretty=%s"], capture_output=True, text=True, check=True)
            self.assertEqual(log.stdout.strip(), "plan(P14): freeze executable design")

    def test_reviewer_resource_failure_fails_over_same_plan_with_audit_history(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = ReviewerResourceFailoverPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True, "review_independence": "provider"}},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-reviewer-failover")
            row = wait_terminal(coordinator, plan_id)

            self.assertEqual(row["state"], "ready", row)
            self.assertEqual(port.planner_calls, 1)
            reviews = [request for request in port.requests if request.role == "reviewer"]
            self.assertEqual(len(reviews), 2)
            self.assertEqual(reviews[0].prompt, reviews[1].prompt)
            self.assertEqual(reviews[0].previous_resource_context.resource_id, "planner-r")
            self.assertEqual(reviews[1].previous_resource_context.resource_id, "planner-r")
            self.assertEqual(reviews[1].independence, "provider")
            self.assertEqual(reviews[1].excluded_resource_ids, ("reviewer-r-1",))
            self.assertTrue(reviews[1].request_id.endswith(":reviewer:failover-1"))
            self.assertEqual(row["review_resource"]["resource_id"], "reviewer-r-2")
            self.assertEqual(row["reviewer_attempt_count"], 2)
            self.assertEqual(row["reviewer_failover_count"], 1)
            self.assertEqual(len(row["reviewer_attempts"]), 2)
            failed, accepted = row["reviewer_attempts"]
            self.assertEqual(failed["resource"]["resource_id"], "reviewer-r-1")
            self.assertEqual(failed["failure_classification"], "quota_exhausted")
            self.assertEqual(failed["dispatch_id"], "dispatch-review-1")
            self.assertEqual(accepted["resource"]["resource_id"], "reviewer-r-2")
            self.assertEqual(accepted["excluded_resource_ids"], ["reviewer-r-1"])
            self.assertEqual(accepted["failover_from_resource_ids"], ["reviewer-r-1"])

    def test_reviewer_transport_timeout_reconciles_terminal_failure_and_fails_over(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = ReviewerTransportTimeoutFailoverPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True, "review_independence": "provider"}},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-reviewer-timeout-failover")
            row = wait_terminal(coordinator, plan_id)

            self.assertEqual(row["state"], "ready", row)
            reviews = [request for request in port.requests if request.role == "reviewer"]
            self.assertEqual(len(reviews), 2)
            self.assertEqual(reviews[1].excluded_resource_ids, ("chatgpt/default/sol",))
            self.assertEqual(row["reviewer_attempt_count"], 2)
            self.assertEqual(row["reviewer_failover_count"], 1)
            failed, accepted = row["reviewer_attempts"]
            self.assertEqual(failed["resource"]["resource_id"], "chatgpt/default/sol")
            self.assertEqual(failed["failure_classification"], "provider_temporarily_unavailable")
            self.assertIn("timed out", failed["error"])
            self.assertEqual(accepted["resource"]["resource_id"], "reviewer-gemini")

    def test_reviewer_non_resource_failure_does_not_fail_over(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = ReviewerResourceFailoverPort(failures=1, classification=None)
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo), "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True}},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-non-resource-review")
            row = wait_terminal(coordinator, plan_id)

            self.assertEqual(row["state"], "failed", row)
            self.assertEqual(port.planner_calls, 1)
            self.assertEqual(port.review_calls, 1)
            self.assertIn("usage limit", row["reason"])

    def test_reviewer_resource_failover_is_bounded_when_all_candidates_fail(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = ReviewerResourceFailoverPort(failures=3)
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo), "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True, "max_reviewer_resource_failovers": 2}},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-reviewer-exhaust")
            row = wait_terminal(coordinator, plan_id)

            self.assertEqual(row["state"], "failed", row)
            self.assertIn("all eligible reviewer resources exhausted/unavailable", row["reason"])
            self.assertEqual(port.planner_calls, 1)
            self.assertEqual(port.review_calls, 3)
            self.assertEqual(row["reviewer_attempt_count"], 3)
            self.assertEqual(row["reviewer_failover_count"], 2)
            self.assertEqual(
                [entry["resource"]["resource_id"] for entry in row["reviewer_attempts"]],
                ["reviewer-r-1", "reviewer-r-2", "reviewer-r-3"],
            )

    def test_failed_plan_review_retry_reuses_existing_plan_without_planner(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True, check=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            plan = {
                "task_id": "P14", "summary": "Reuse this already generated plan.",
                "implementation_steps": ["Step 1"], "interfaces": ["Interface 1"],
                "validation": ["Validation 1"], "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            runtime.mkdir(parents=True)
            (runtime / "ai-planner.json").write_text(json.dumps({
                "version": 1,
                "plans": {
                    "ai_plan:failed-review": {
                        "plan_id": "ai_plan:failed-review",
                        "command_id": "failed-review",
                        "project_id": "p1",
                        "task_id": "P14",
                        "repo_path": str(repo),
                        "branch": branch,
                        "head": head,
                        "state": "failed",
                        "started_at": "2026-09-18T00:00:00+00:00",
                        "completed_at": "2026-09-18T00:01:00+00:00",
                        "reason": "plan review failed: no eligible resource",
                        "next_text": next_text,
                        "plan": plan,
                        "planner_dispatch_id": "dispatch-existing-plan",
                        "planner_execution_id": "execution-existing-plan",
                        "planner_completed_at": "2026-09-18T00:00:30+00:00",
                        "planner_resource": {
                            "resource_id": "codex/default/gpt-5.6-sol",
                            "provider": "codex",
                            "account": "default",
                            "model": "gpt-5.6-sol",
                        },
                    }
                },
            }), encoding="utf-8")
            (repo / "unrelated.txt").write_text("new unrelated commit\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "unrelated.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "unrelated change"], check=True)
            current_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            self.assertNotEqual(head, current_head)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "quality": "high", "review_quality": "high",
                    "review_independence": "provider",
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "PLAN_FAILED",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"},
            }
            handled, plan_id, reason = coordinator.continue_failed_plan_review(
                project, snapshot, "review-only-retry"
            )
            self.assertTrue(handled)
            self.assertEqual(plan_id, "ai_plan:review-only-retry")
            self.assertIn("existing planner output", reason)
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual([request.role for request in port.requests], ["reviewer"])
            self.assertEqual(
                port.requests[0].previous_resource_context.resource_id,
                "codex/default/gpt-5.6-sol",
            )
            self.assertEqual(row["planner_reused_from"], "ai_plan:failed-review")
            self.assertEqual(row["reused_plan_source_head"], head)
            self.assertEqual(row["reused_plan_reanchored_head"], current_head)
            self.assertEqual(row["planner_resource"]["resource_id"], "codex/default/gpt-5.6-sol")
            self.assertEqual(row["plan_review_recovery_count"], 1)

    def test_failed_plan_review_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True, check=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            plan = {
                "task_id": "P14", "summary": "Reuse this already generated plan.",
                "implementation_steps": ["Step 1"], "interfaces": ["Interface 1"],
                "validation": ["Validation 1"], "risks": ["Risk 1"], "out_of_scope": ["Scope 1"],
            }
            runtime.mkdir(parents=True)
            (runtime / "ai-planner.json").write_text(json.dumps({
                "version": 1, "plans": {"ai_plan:failed-review": {
                    "plan_id": "ai_plan:failed-review", "command_id": "failed-review",
                    "project_id": "p1", "task_id": "P14", "repo_path": str(repo),
                    "branch": branch, "head": head, "state": "failed",
                    "completed_at": "2026-09-18T00:01:00+00:00",
                    "reason": "plan review failed: broker service invocation failed: timed out",
                    "next_text": next_text, "plan": plan, "plan_review_recovery_count": 3,
                    "planner_completed_at": "2026-09-18T00:00:30+00:00",
                    "planner_resource": {"resource_id": "planner-r", "provider": "codex", "account": "default", "model": "gpt"},
                }}
            }), encoding="utf-8")
            coordinator = AIPlannerCoordinator(runtime, FakePort())
            project = {
                "project_id": "p1", "repo_path": str(repo), "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True, "max_attempts": 3}},
            }
            snapshot = {"project_id": "p1", "state": "PLAN_FAILED", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            handled, plan_id, reason = coordinator.continue_failed_plan_review(project, snapshot, "bounded-review-retry")
            self.assertTrue(handled)
            self.assertIsNone(plan_id)
            self.assertIn("retry limit reached (3)", reason)
            self.assertEqual(coordinator.port.requests, [])

    def test_planner_resource_failure_excludes_failed_resource_and_fails_over(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = PlannerResourceFailoverPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "review_independence": "provider", "max_attempts": 3,
                }},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-planner-failover")
            row = wait_terminal(coordinator, plan_id)

            self.assertEqual(row["state"], "ready", row)
            planners = [r for r in port.requests if r.role == "planner"]
            self.assertEqual(len(planners), 2)
            self.assertEqual(planners[0].excluded_resource_ids, ())
            self.assertEqual(planners[1].excluded_resource_ids, ("planner-opus",))
            self.assertEqual(planners[1].metadata["planner_failover_from_resource_ids"], ["planner-opus"])
            self.assertEqual(row["planner_attempts"][0]["failure_classification"], "quota_exhausted")
            self.assertEqual(row["planner_attempts"][1]["excluded_resource_ids"], ["planner-opus"])
            self.assertEqual(row["planner_resource"]["resource_id"], "planner-codex")

    def test_late_quota_failure_still_fails_over_after_protocol_retries(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = LateQuotaPlannerFailoverPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "review_independence": "provider", "max_attempts": 3,
                }},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-late-quota")
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "ready", row)
            planners = [r for r in port.requests if r.role == "planner"]
            self.assertEqual(len(planners), 4)
            self.assertEqual(planners[1].metadata["planner_format_repairs"], 1)
            self.assertEqual(planners[1].metadata["planner_semantic_failures"], 0)
            self.assertEqual(planners[2].metadata["planner_semantic_failures"], 1)
            self.assertEqual(row["planner_attempts"][0]["failure_classification"], "planner_schema_repair")
            self.assertEqual(row["planner_attempts"][1]["failure_classification"], "planner_schema_error")
            self.assertEqual(planners[3].excluded_resource_ids, ("planner-opus",))
            self.assertEqual(row["planner_resource"]["resource_id"], "planner-codex")
            self.assertEqual(row["planner_attempts"][2]["failure_classification"], "quota_exhausted")

    def test_planner_failure_retries_and_recovers_without_new_control(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = FailOncePlannerPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "quality": "high", "review_quality": "high",
                    "review_independence": "resource", "max_attempts": 3,
                }},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-retry")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual([r.role for r in port.requests], ["planner", "planner", "reviewer"])
            self.assertTrue(port.requests[1].request_id.endswith(":planner:retry-1"))
            self.assertEqual(port.requests[1].previous_resource_context.resource_id, "bad-r")
            self.assertIn("[PLANNER_RETRY]", port.requests[1].prompt)
            self.assertIn("claude did not emit a JSON result", port.requests[1].prompt)
            self.assertEqual(row["planner_attempt_count"], 2)
            self.assertEqual(row["planner_retry_count"], 1)
            self.assertEqual(len(row["planner_attempts"]), 2)
            self.assertEqual(row["planner_attempts"][0]["reason"], "claude did not emit a JSON result")
            self.assertIsNone(row["planner_attempts"][1]["reason"])

    def test_planner_retry_exhaustion_fails_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = AlwaysFailPlannerPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True, "max_attempts": 2}},
            }
            snapshot = {"project_id": "p1", "state": "IDLE", "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P14"}}
            plan_id, _ = coordinator.start(project, snapshot, "command-exhaust")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)
            self.assertEqual(row["state"], "failed", row)
            self.assertIn("planner failed after 2 attempts", row["reason"])
            self.assertEqual([r.role for r in port.requests], ["planner", "planner"])
            self.assertEqual(row["planner_attempt_count"], 2)
            self.assertEqual(row["planner_retry_count"], 1)
            self.assertEqual(len(row["planner_attempts"]), 2)
            self.assertIn("PENDING DESIGN", (repo / "agent" / "next.md").read_text(encoding="utf-8"))


    def test_restart_marks_active_plan_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "ai-planner.json").write_text(json.dumps({"version":1,"plans":{"p":{"state":"reviewing"}}}), encoding="utf-8")
            coordinator = AIPlannerCoordinator(runtime, None)
            row = coordinator.state()["plans"]["p"]
            self.assertEqual(row["state"], "recovery_required")
            self.assertIn("automatic replay forbidden", row["reason"])

    def test_project_context_injected_into_planner_and_reviewer_prompts(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            ctx_payload = {
                "schema_version": 1,
                "project_id": "p1",
                "goals": ["Build durable context"],
                "architecture": ["Core context module"],
                "protected_scope": ["Production infra"],
                "safety_constraints": ["Fail closed"],
                "validation_commands": ["unittest"],
                "runtime_assumptions": ["Python 3.11"],
                "key_decisions": ["D1 schema"],
            }
            (repo / "agent" / "project-context.json").write_text(json.dumps(ctx_payload), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "context"], check=True)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "quality": "high", "review_quality": "high",
                    "review_independence": "resource",
                }},
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "require_valid": True,
                },
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, reason = coordinator.start(project, snapshot, "command-ctx")
            self.assertEqual(plan_id, "ai_plan:command-ctx")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready")
            self.assertEqual(row["context_state"], "ready")
            self.assertIsNotNone(row["context_digest"])

            # Verify both planner and reviewer prompts contain [PROJECT_CONTEXT_BEGIN]
            planner_prompt = port.requests[0].prompt
            reviewer_prompt = port.requests[1].prompt
            self.assertIn("[PROJECT_CONTEXT_BEGIN]", planner_prompt)
            self.assertIn("## goals\n- Build durable context", planner_prompt)
            self.assertIn("[PROJECT_CONTEXT_END]", planner_prompt)
            self.assertIn("[PROJECT_CONTEXT_BEGIN]", reviewer_prompt)
            self.assertIn("## goals\n- Build durable context", reviewer_prompt)
            self.assertIn("[PROJECT_CONTEXT_END]", reviewer_prompt)

    def test_invalid_context_refuses_planner_start_when_required(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            # Create invalid context file (schema_version 99)
            (repo / "agent" / "project-context.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "invalid-context"], check=True)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True}},
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "require_valid": True,
                },
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, reason = coordinator.start(project, snapshot, "command-fail")
            self.assertIsNone(plan_id)
            self.assertIn("durable project context is invalid", reason)

    def test_absent_context_leaves_prompts_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True}},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "command-absent")
            deadline = time.time() + 5
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)
            self.assertNotIn("[PROJECT_CONTEXT_BEGIN]", port.requests[0].prompt)
            self.assertNotIn("[PROJECT_CONTEXT_BEGIN]", port.requests[1].prompt)

    def test_plan_review_rejection_remediates_and_approves(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = RejectOnceReviewerPort()
            progress = RecordingProgressChannel()
            coordinator = AIPlannerCoordinator(runtime, port, progress_channel=progress)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "quality": "high", "review_quality": "high",
                    "review_independence": "resource", "max_plan_remediation_rounds": 3,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, reason = coordinator.start(project, snapshot, "command-rem-ok")
            self.assertEqual(plan_id, "ai_plan:command-rem-ok")
            self.assertIn("started", reason)
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "remediating", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual(row["remediation_round"], 1)
            self.assertEqual(len(row["rejection_chain"]), 1)
            rejection = row["rejection_chain"][0]
            self.assertEqual(rejection["round"], 0)
            self.assertEqual(rejection["reason"], "implementation steps lack error handling")
            self.assertIn("Plan version 1", rejection["prior_plan"]["summary"])
            self.assertEqual(rejection["planner_dispatch_id"], "dispatch-plan-1")
            self.assertEqual(rejection["reviewer_dispatch_id"], "dispatch-review-1")
            self.assertEqual(rejection["planner_resource"]["resource_id"], "planner-r-1")
            self.assertEqual(rejection["reviewer_resource"]["resource_id"], "review-r-1")
            self.assertIsNotNone(rejection["rejected_at"])

            remediate_events = [e for e in progress.emissions if e["milestone"] == "REMEDIATE"]
            self.assertEqual(len(remediate_events), 1)
            self.assertEqual(remediate_events[0]["details"]["round"], 1)
            self.assertEqual(remediate_events[0]["details"]["reason"], "implementation steps lack error handling")
            self.assertEqual(remediate_events[0]["details"]["plan_id"], plan_id)

            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            self.assertIn("Status: **READY_TO_RUN**", next_text)
            self.assertIn("## Approved executable design", next_text)
            self.assertIn("remediated error handling is acceptable", next_text)

    def test_plan_review_rejection_exhausts_bound_to_owner_gate(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = AlwaysRejectReviewerPort()
            progress = RecordingProgressChannel()
            coordinator = AIPlannerCoordinator(runtime, port, progress_channel=progress)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "max_plan_remediation_rounds": 3,
                    "max_plan_recovery_cycles": 0, "hard_total_review_rejects": 10,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "command-rem-exhaust")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "remediating", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "owner_gate", row)
            self.assertIn("bound", row["reason"].lower())
            self.assertIn("exhausted", row["reason"].lower())
            self.assertIn("3", row["reason"])

            planner_requests = [r for r in port.requests if r.role == "planner"]
            reviewer_requests = [r for r in port.requests if r.role == "reviewer"]
            self.assertEqual(len(planner_requests), 4)  # 1 initial + 3 remediation calls
            self.assertEqual(len(reviewer_requests), 4) # 4 reviews total
            self.assertEqual(len(row["rejection_chain"]), 4)
            self.assertEqual([e["round"] for e in row["rejection_chain"]], [0, 1, 2, 3])

            remediate_events = [e for e in progress.emissions if e["milestone"] == "REMEDIATE"]
            self.assertEqual(len(remediate_events), 3)
            owner_gate_events = [e for e in progress.emissions if e["milestone"] == "OWNER_GATE"]
            self.assertEqual(len(owner_gate_events), 1)
            self.assertEqual(owner_gate_events[0]["details"]["rejection_chain_length"], 4)

    def test_plan_reviewer_protocol_repair_does_not_consume_semantic_remediation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = PlanReviewerProtocolRepairPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 2,
                    "max_plan_recovery_cycles": 1,
                    "hard_total_review_rejects": 5,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P15"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "protocol-repair")
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual(row.get("remediation_round"), 0)
            self.assertEqual(len(row.get("rejection_chain") or []), 0)
            repair_requests = [
                req for req in port.requests if req.metadata.get("protocol_repair")
            ]
            self.assertEqual(len(repair_requests), 1)
            captures = row.get("reviewer_raw_outputs") or []
            self.assertEqual(len(captures), 2)
            self.assertEqual(captures[0]["repair_index"], 0)
            self.assertEqual(captures[1]["repair_index"], 1)
            self.assertIn("Do not reconsider", repair_requests[0].prompt)

    def test_plan_rejection_soft_bound_enters_recovery_cycle_and_switches_planner(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = RecoveryAfterBoundPort()
            progress = RecordingProgressChannel()
            coordinator = AIPlannerCoordinator(runtime, port, progress_channel=progress)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 1,
                    "max_plan_recovery_cycles": 1,
                    "hard_total_review_rejects": 4,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "command-recovery-cycle")
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual(row["recovery_cycle"], 1)
            self.assertEqual(len(row["rejection_chain"]), 2)
            recovery_planners = [
                req for req in port.requests
                if req.role == "planner" and ":recovery-1" in req.request_id
            ]
            self.assertEqual(len(recovery_planners), 1)
            self.assertIn("planner-a", recovery_planners[0].excluded_resource_ids)
            self.assertEqual(row["planner_resource"]["resource_id"], "planner-b")
            recovery_events = [e for e in progress.emissions if e["milestone"] == "PLAN_RECOVERY"]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(recovery_events[0]["details"]["recovery_cycle"], 1)
            self.assertEqual(recovery_events[0]["details"]["rejection_chain_length"], 2)

    def test_legacy_exhausted_gate_can_resume_into_recovery_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = RecoveryAfterBoundPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            initial_project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 1,
                    "max_plan_recovery_cycles": 0,
                    "hard_total_review_rejects": 10,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(initial_project, snapshot, "legacy-exhausted")
            gated = wait_terminal(coordinator, plan_id)
            self.assertEqual(gated["state"], "owner_gate", gated)
            self.assertEqual(len(gated["rejection_chain"]), 2)

            recovery_project = {
                **initial_project,
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 1,
                    "max_plan_recovery_cycles": 1,
                    "hard_total_review_rejects": 4,
                }},
            }
            resumed, reason = coordinator.resume_exhausted_technical_gate(
                recovery_project, snapshot, plan_id
            )
            self.assertTrue(resumed, reason)
            self.assertIn("recovery cycle 1", reason)
            recovered = wait_terminal(coordinator, plan_id)
            self.assertEqual(recovered["state"], "ready", recovered)
            self.assertEqual(recovered["recovery_cycle"], 1)
            self.assertEqual(len(recovered["rejection_chain"]), 2)
            recovery_planners = [
                req for req in port.requests
                if req.role == "planner" and ":recovery-1" in req.request_id
            ]
            self.assertEqual(len(recovery_planners), 1)
            self.assertIn("planner-a", recovery_planners[0].excluded_resource_ids)
            self.assertEqual(recovered["planner_resource"]["resource_id"], "planner-b")

    def test_legacy_exhausted_gate_reanchors_clean_descendant_with_unchanged_task_spec(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = RecoveryAfterBoundPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            initial_project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 1,
                    "max_plan_recovery_cycles": 0,
                    "hard_total_review_rejects": 10,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(initial_project, snapshot, "legacy-descendant")
            gated = wait_terminal(coordinator, plan_id)
            self.assertEqual(gated["state"], "owner_gate", gated)
            old_head = gated["head"]

            (repo / "policy.txt").write_text("orchestrator recovery policy only\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "policy.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "policy recovery"], check=True)
            new_head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertNotEqual(old_head, new_head)

            recovery_project = {
                **initial_project,
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 1,
                    "max_plan_recovery_cycles": 1,
                    "hard_total_review_rejects": 4,
                }},
            }
            resumed, reason = coordinator.resume_exhausted_technical_gate(
                recovery_project, snapshot, plan_id
            )
            self.assertTrue(resumed, reason)
            recovered = wait_terminal(coordinator, plan_id)
            self.assertEqual(recovered["state"], "ready", recovered)
            self.assertEqual(recovered["reanchored_from_head"], old_head)
            self.assertEqual(recovered["head"], new_head)
            self.assertIsNotNone(recovered["reanchored_at"])

    def test_plan_rejection_hard_total_limit_still_owner_gates(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = AlwaysRejectReviewerPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True,
                    "max_plan_remediation_rounds": 1,
                    "max_plan_recovery_cycles": 2,
                    "hard_total_review_rejects": 3,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "command-hard-limit")
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "owner_gate", row)
            self.assertIn("hard reject limit reached (3)", row["reason"])
            self.assertEqual(len(row["rejection_chain"]), 3)
            self.assertEqual(row["recovery_cycle"], 1)

    def test_remediation_prompt_and_resource_propagation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = RejectOnceReviewerPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "max_plan_remediation_rounds": 3,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "command-rem-prompt")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "remediating", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready")

            # Verify remediation prompt (index 2: planner round 1)
            rem_req = port.requests[2]
            self.assertEqual(rem_req.role, "planner")
            self.assertIn("[PLAN_REMEDIATION]", rem_req.prompt)
            self.assertIn("implementation steps lack error handling", rem_req.prompt)
            self.assertIn("Plan version 1", rem_req.prompt)
            self.assertIn("Prior planner resource:", rem_req.prompt)
            self.assertIn("planner-r-1", rem_req.prompt)
            self.assertIsNotNone(rem_req.previous_resource_context)
            self.assertEqual(rem_req.previous_resource_context.resource_id, "planner-r-1")

            # Verify uniqueness of request ids
            req_ids = [r.request_id for r in port.requests]
            self.assertEqual(len(req_ids), len(set(req_ids)))
            self.assertEqual(port.requests[0].request_id, f"{plan_id}:planner")
            self.assertEqual(port.requests[1].request_id, f"{plan_id}:reviewer")
            self.assertEqual(port.requests[2].request_id, f"{plan_id}:planner:remediate-1")
            self.assertEqual(port.requests[3].request_id, f"{plan_id}:reviewer:remediate-1")

    def test_restart_marks_remediating_plan_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "ai-planner.json").write_text(
                json.dumps({"version": 1, "plans": {"p": {"state": "remediating"}}}),
                encoding="utf-8",
            )
            coordinator = AIPlannerCoordinator(runtime, None)
            row = coordinator.state()["plans"]["p"]
            self.assertEqual(row["state"], "recovery_required")
            self.assertIn("automatic replay forbidden", row["reason"])

    def test_invalid_max_plan_remediation_rounds_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            for invalid_val in [0, 6, True, "3"]:
                project = {
                    "project_id": "p1", "repo_path": str(repo),
                    "execution": {"engine": "aibroker"},
                    "ai_roles": {"planner": {
                        "enabled": True,
                        "max_plan_remediation_rounds": invalid_val,
                    }},
                }
                plan_id, reason = coordinator.start(project, snapshot, f"command-inv-{invalid_val}")
                self.assertIsNone(plan_id, f"Expected None plan_id for {invalid_val}")
                self.assertIn("planner max_plan_remediation_rounds must be an integer from 1 to 5", reason)

    def test_reviewer_transport_failure_during_remediation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = FailReviewOnRemediationPort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "max_plan_remediation_rounds": 3,
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, _ = coordinator.start(project, snapshot, "command-rem-fail")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "remediating", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "failed")
            self.assertIn("plan review failed", row["reason"])


if __name__ == "__main__":
    unittest.main()
