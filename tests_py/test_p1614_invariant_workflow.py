"""P16.14 Invariant-driven control-plane development and validation tests.

Validates task-level classification, canonical declaration grammar,
committed-artifact loading, authoritative owner gate transitions and repair,
different-HEAD replay, prompt injection, review scope-gap analysis,
fault registry integrity, and read-only convergence/traversal evidence projections.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock
import unittest.mock

from dev_orchestrator.core.control_plane_contract import (
    CONTROL_PLANE_RUNTIME_SURFACES,
    ControlPlaneDeclaration,
    ControlPlaneGate,
    ControlPlaneScope,
    classify_control_plane_task,
    convergence_evidence,
    declared_scope_gap,
    evaluate_launch_declaration,
    inject_control_plane_contract,
    load_control_plane_declaration,
    parse_control_plane_declaration,
    render_declaration_section,
    require_control_plane_declaration,
    traversal_evidence,
)
from dev_orchestrator.core.control_plane_faults import (
    FaultScenario,
    fault_scenarios,
    get_fault_scenario,
    required_scenarios_for,
    scenarios_for_invariant,
    validate_fault_registry,
)
from dev_orchestrator.core.lifecycle_authority import (
    INVARIANT_CODES,
    new_authority,
    open_declaration_gate,
    resolve_declaration_gate,
)
from dev_orchestrator.core.transition_executor import (
    TransitionExecutor,
    _is_declaration_gate_replayable,
)
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.workflow_policy import (
    inject_control_plane_contract as policy_inject_cp,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True, text=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "agent").mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _commit(repo, "initial commit")
    return repo


class ControlPlaneDeclarationGrammarTests(unittest.TestCase):
    """Unit tests for canonical declaration parsing and validation."""

    def test_valid_canonical_declaration(self):
        text = """# Task P16.14
Status: **READY_TO_RUN**

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: plan_freeze, worker_launch
- Fault scenarios: CPF-01, CPF-08
- Convergence evidence: Single authoritative owner converges without manual continue.

## Approved executable design
Implementation details...
"""
        decl = parse_control_plane_declaration(text, task_id="P16.14")
        self.assertEqual(decl.kind, "declared")
        self.assertEqual(decl.invariants, ("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION", "SINGLE_ACTIVE_LIFECYCLE_OWNER"))
        self.assertEqual(decl.transition_boundaries, ("plan_freeze", "worker_launch"))
        self.assertEqual(decl.fault_scenarios, ("CPF-01", "CPF-08"))
        self.assertTrue(len(decl.content_hash) > 10)
        self.assertIn("Single authoritative owner", decl.convergence_evidence)

    def test_missing_declaration_is_absent(self):
        text = "# Task Ordinary\n\nStatus: **READY_TO_RUN**\n\n## Approved executable design\n"
        decl = parse_control_plane_declaration(text)
        self.assertEqual(decl.kind, "absent")

    def test_multiple_sections_is_ambiguous(self):
        text = """## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: plan_freeze
- Fault scenarios: CPF-06
- Convergence evidence: ok

## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: plan_freeze
- Fault scenarios: CPF-06
- Convergence evidence: ok
"""
        decl = parse_control_plane_declaration(text)
        self.assertEqual(decl.kind, "ambiguous")

    def test_unknown_invariant_is_invalid(self):
        text = """## Control-Plane Impact
- Invariants: UNKNOWN_INVARIANT_CODE
- Transition boundaries: plan_freeze
- Fault scenarios: CPF-01
- Convergence evidence: ok
"""
        decl = parse_control_plane_declaration(text)
        self.assertEqual(decl.kind, "invalid")
        self.assertIn("unknown invariant code", decl.reason or "")

    def test_unknown_transition_boundary_is_invalid(self):
        text = """## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: magical_teleportation
- Fault scenarios: CPF-06
- Convergence evidence: ok
"""
        decl = parse_control_plane_declaration(text)
        self.assertEqual(decl.kind, "invalid")
        self.assertIn("unknown transition boundary", decl.reason or "")

    def test_unknown_fault_scenario_is_invalid(self):
        text = """## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: plan_freeze
- Fault scenarios: CPF-9999
- Convergence evidence: ok
"""
        decl = parse_control_plane_declaration(text)
        self.assertEqual(decl.kind, "invalid")
        self.assertIn("unknown fault scenario ID", decl.reason or "")

    def test_duplicate_fields_is_invalid(self):
        text = """## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION
- Transition boundaries: plan_freeze
- Fault scenarios: CPF-01
- Convergence evidence: ok
"""
        decl = parse_control_plane_declaration(text)
        self.assertEqual(decl.kind, "invalid")
        self.assertIn("duplicate field", decl.reason or "")

    def test_allow_bare_declaration(self):
        interfaces = """Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
Transition boundaries: plan_freeze
Fault scenarios: CPF-06
Convergence evidence: Unambiguous single owner"""
        decl = parse_control_plane_declaration(interfaces, allow_bare=True)
        self.assertEqual(decl.kind, "declared")
        self.assertEqual(decl.invariants, ("SINGLE_ACTIVE_LIFECYCLE_OWNER",))

    def test_render_declaration_section(self):
        decl = ControlPlaneDeclaration(
            kind="declared",
            source_path="agent/next.md",
            revision="abc",
            content_hash="123",
            invariants=("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
            transition_boundaries=("plan_freeze",),
            fault_scenarios=("CPF-06",),
            convergence_evidence="Single owner convergence",
        )
        rendered = render_declaration_section(decl)
        self.assertIn("## Control-Plane Impact", rendered)
        self.assertIn("SINGLE_ACTIVE_LIFECYCLE_OWNER", rendered)


class ControlPlaneClassificationTests(unittest.TestCase):
    """Unit tests for task-level classification."""

    def test_explicit_declaration_is_control_plane(self):
        text = """## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: plan_freeze
- Fault scenarios: CPF-06
- Convergence evidence: ok
"""
        scope = classify_control_plane_task(task_text=text)
        self.assertEqual(scope.kind, "control_plane")
        self.assertEqual(scope.evidence_source, "explicit_declaration")

    def test_protected_runtime_surface_in_plan_is_control_plane(self):
        plan = {
            "summary": "Update lifecycle invariants evaluator",
            "implementation_steps": [
                "Modify evaluate_lifecycle_invariants in src/dev_orchestrator/core/lifecycle_authority.py",
            ],
            "interfaces": [],
            "validation": [],
        }
        scope = classify_control_plane_task(candidate_plan=plan)
        self.assertEqual(scope.kind, "control_plane")
        self.assertIn("src/dev_orchestrator/core/lifecycle_authority.py", scope.matched_surfaces)

    def test_unrelated_ui_task_is_ordinary(self):
        task_text = "# P20.1 UI Dark Mode Toggle\n\nAdd toggle switch in settings panel for dark mode."
        plan = {
            "summary": "Implement dark mode CSS and React toggle",
            "implementation_steps": ["Edit frontend/components/Toggle.tsx", "Add dark theme CSS styles"],
            "interfaces": ["ToggleProps"],
            "validation": ["Verify click switches theme class on document body"],
        }
        scope = classify_control_plane_task(task_text=task_text, candidate_plan=plan)
        self.assertEqual(scope.kind, "ordinary")
        self.assertEqual(scope.matched_surfaces, ())

    def test_unrelated_provider_task_is_ordinary(self):
        task_text = "# Provider Adapter Support\n\nAdd mock adapter for local testing."
        plan = {
            "summary": "Add mock provider",
            "implementation_steps": ["Create dev_orchestrator/agents/mock_adapter.py"],
            "interfaces": [],
            "validation": ["Unit tests in tests_py/test_mock_adapter.py"],
        }
        scope = classify_control_plane_task(task_text=task_text, candidate_plan=plan)
        self.assertEqual(scope.kind, "ordinary")

    def test_documentation_task_is_ordinary(self):
        task_text = "# Architecture Documentation\n\nUpdate docs/architecture.md diagram."
        scope = classify_control_plane_task(task_text=task_text)
        self.assertEqual(scope.kind, "ordinary")

    def test_benchmark_task_is_ordinary(self):
        task_text = "# Benchmark Suite\n\nAdd latency benchmark script in benchmarks/perf.py."
        scope = classify_control_plane_task(task_text=task_text)
        self.assertEqual(scope.kind, "ordinary")

    def test_explicit_ordinary_conflicting_with_protected_surface_is_invalid(self):
        task_text = "Classification: ordinary\nModify transition executor."
        plan = {
            "summary": "Fix _lifecycle_launch_guard in src/dev_orchestrator/core/transition_executor.py",
            "implementation_steps": ["Edit _lifecycle_launch_guard"],
        }
        scope = classify_control_plane_task(task_text=task_text, candidate_plan=plan)
        self.assertEqual(scope.kind, "invalid")
        self.assertEqual(scope.evidence_source, "conflict")

    def test_unevaluable_when_all_inputs_missing(self):
        scope = classify_control_plane_task()
        self.assertEqual(scope.kind, "unevaluable")


class ControlPlanePlanFreezeTests(unittest.TestCase):
    """Plan freeze and validation integration tests."""

    def test_ordinary_plan_render_does_not_add_declaration_section(self):
        original = "# P20.1 UI\n\nStatus: **PENDING DESIGN**\n"
        plan = {
            "summary": "Build UI component",
            "implementation_steps": ["step 1"],
            "interfaces": ["interface 1"],
            "validation": ["test 1"],
            "risks": ["risk 1"],
            "out_of_scope": ["scope 1"],
        }
        rendered = AIPlannerCoordinator._render_next(original, plan, "Approved by reviewer")
        self.assertNotIn("## Control-Plane Impact", rendered)
        self.assertIn("## Approved executable design", rendered)

    def test_control_plane_plan_render_materializes_declaration_section(self):
        original = "# P16.15 Lifecycle Update\n\nStatus: **PENDING DESIGN**\n"
        plan = {
            "summary": "Update src/dev_orchestrator/core/lifecycle_authority.py",
            "implementation_steps": ["step 1"],
            "interfaces": [
                "Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER",
                "Transition boundaries: plan_freeze",
                "Fault scenarios: CPF-06",
                "Convergence evidence: Idempotent single owner",
            ],
            "validation": ["test 1 with CPF-06"],
            "risks": ["risk 1"],
            "out_of_scope": ["scope 1"],
        }
        rendered = AIPlannerCoordinator._render_next(original, plan, "Approved by reviewer")
        self.assertIn("## Control-Plane Impact", rendered)
        self.assertIn("SINGLE_ACTIVE_LIFECYCLE_OWNER", rendered)
        self.assertIn("CPF-06", rendered)
        self.assertIn("## Approved executable design", rendered)

    def test_candidate_plan_missing_required_scenarios_fails_gate(self):
        decl = ControlPlaneDeclaration(
            kind="declared",
            source_path="agent/next.md",
            revision=None,
            content_hash="h1",
            invariants=("SINGLE_ACTIVE_LIFECYCLE_OWNER", "CURRENT_TASK_MATCHES_ACTIVE_EXECUTION"),
            transition_boundaries=("plan_freeze",),
            fault_scenarios=("CPF-01",),  # Missing CPF-06, CPF-08, CPF-09, CPF-10
            convergence_evidence="Single owner",
        )
        scope = ControlPlaneScope(
            kind="control_plane",
            matched_surfaces=("src/dev_orchestrator/core/lifecycle_authority.py",),
            evidence_source="candidate_plan",
            reason="touches protected surfaces",
        )
        gate = require_control_plane_declaration("proj1", "task1", scope, decl)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.code, "CONTROL_PLANE_DECLARATION_REQUIRED")
        self.assertIn("omits required fault scenarios", gate.reason)


class ControlPlaneGitHistoryTests(unittest.TestCase):
    """Git history proof that P16.14 declaration preceded source code changes."""

    def test_p1614_declaration_commit_preceded_source_modifications(self):
        repo_root = Path.cwd()
        # Find commit cb1648e or declaration commit in repo
        try:
            log_out = subprocess.check_output(
                ["git", "-C", str(repo_root), "log", "--oneline", "-n", "10"],
                text=True,
            )
            # Check for the declaration-only commit
            self.assertIn("declare control-plane impact", log_out.lower())

            # Verify that commit cb1648e only modified agent/ files and no src/ files
            commit_files = subprocess.check_output(
                ["git", "-C", str(repo_root), "show", "--name-only", "--format=", "cb1648e"],
                text=True,
            ).splitlines()
            commit_files = [f.strip() for f in commit_files if f.strip()]
            for f in commit_files:
                self.assertTrue(f.startswith("agent/"), f"Commit cb1648e must only modify agent/ files, found: {f}")
        except subprocess.CalledProcessError:
            self.skipTest("git log check not available in this environment")


class ControlPlaneGateLifecycleTests(unittest.TestCase):
    """Authoritative gate transition, idempotency, repair, and replay tests.

    Exercises CPF-08, CPF-09, CPF-10.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _init_repo(self.root)
        self.runtime_root = self.root / ".dev_orchestrator"
        self.runtime_root.mkdir()

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_unannounced_control_plane_launch_refuses_to_owner_gate(self):
        """CPF-08: Control-plane task missing declaration fails closed to OWNER_GATE."""
        # Commit a task that modifies protected runtime surface without declaration
        (self.repo / "agent" / "next.md").write_text(
            "# P16.99 Lifecycle Core Refactor\n\n"
            "Status: **READY_TO_RUN**\n\n"
            "## Approved executable design\n\n"
            "Modify src/dev_orchestrator/core/lifecycle_authority.py to add new hook.\n",
            encoding="utf-8",
        )
        head = _commit(self.repo, "unannounced control plane task")
        truth = read_repository_truth(self.repo)

        executor = TransitionExecutor(self.runtime_root, ai_execution_port=unittest.mock.MagicMock())
        project = {
            "project_id": "test_proj",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
        }
        policy = {"engine": "aibroker"}

        # Attempt Worker launch
        launch = executor._launch(
            project,
            source_request_id="req-cpf08-1",
            source_kind="ready",
            task_id="P16.99",
            source_task_id=None,
            branch=truth.branch,
            head=truth.head,
            worker_prompt="Implement hook",
            policy=policy,
        )
        self.assertIsNone(launch, "Worker launch must be refused")

        ledger = executor._load_ledger()
        authority = ledger["lifecycle"]["test_proj"]
        gate = authority.get("owner_gate")

        self.assertIsNotNone(gate, "owner_gate must be set")
        self.assertEqual(gate["code"], "CONTROL_PLANE_DECLARATION_REQUIRED")
        self.assertEqual(authority["lifecycle_state"], "OWNER_GATE")

        # Execution record must be blocked with gate details
        exec_row = ledger["executions"]["req-cpf08-1"]
        self.assertEqual(exec_row["state"], "blocked")
        self.assertEqual(exec_row["blocked_gate_code"], "CONTROL_PLANE_DECLARATION_REQUIRED")
        self.assertEqual(exec_row["head"], truth.head)

    def test_same_head_repeated_launch_attempts_are_byte_stable(self):
        """CPF-10: Same-HEAD retries against declaration gate are byte-stable without timestamp refresh."""
        (self.repo / "agent" / "next.md").write_text(
            "# P16.99 Lifecycle Core Refactor\n\n"
            "Status: **READY_TO_RUN**\n\n"
            "## Approved executable design\n\n"
            "Modify src/dev_orchestrator/core/lifecycle_authority.py\n",
            encoding="utf-8",
        )
        head = _commit(self.repo, "unannounced task")
        truth = read_repository_truth(self.repo)

        executor = TransitionExecutor(self.runtime_root, ai_execution_port=unittest.mock.MagicMock())
        project = {
            "project_id": "test_proj",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
        }
        policy = {"engine": "aibroker"}

        # First attempt -> Refusal
        executor._launch(
            project,
            source_request_id="req-cpf10-1",
            source_kind="ready",
            task_id="P16.99",
            source_task_id=None,
            branch=truth.branch,
            head=truth.head,
            worker_prompt="prompt",
            policy=policy,
        )
        first_ledger = copy.deepcopy(executor._load_ledger())
        first_gate_recorded_at = first_ledger["lifecycle"]["test_proj"]["owner_gate"]["recorded_at"]
        first_authority_updated_at = first_ledger["lifecycle"]["test_proj"]["updated_at"]

        # Second attempt with same source_request_id on same HEAD
        executor._launch(
            project,
            source_request_id="req-cpf10-1",
            source_kind="ready",
            task_id="P16.99",
            source_task_id=None,
            branch=truth.branch,
            head=truth.head,
            worker_prompt="prompt",
            policy=policy,
        )
        second_ledger = executor._load_ledger()
        second_gate_recorded_at = second_ledger["lifecycle"]["test_proj"]["owner_gate"]["recorded_at"]

        self.assertEqual(first_gate_recorded_at, second_gate_recorded_at)
        self.assertEqual(first_ledger["lifecycle"]["test_proj"]["owner_gate"], second_ledger["lifecycle"]["test_proj"]["owner_gate"])

    def test_repaired_head_reconciles_and_replays_blocked_request(self):
        """CPF-09: Repaired HEAD with valid declaration resolves gate and replays blocked request."""
        # 1. Start with unannounced task -> blocked
        (self.repo / "agent" / "next.md").write_text(
            "# P16.99 Lifecycle Core Refactor\n\n"
            "Status: **READY_TO_RUN**\n\n"
            "## Approved executable design\n\n"
            "Modify src/dev_orchestrator/core/lifecycle_authority.py\n",
            encoding="utf-8",
        )
        bad_head = _commit(self.repo, "unannounced task")
        truth1 = read_repository_truth(self.repo)

        executor = TransitionExecutor(self.runtime_root, ai_execution_port=unittest.mock.MagicMock())
        project = {
            "project_id": "test_proj",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
        }
        policy = {"engine": "aibroker"}

        launch1 = executor._launch(
            project,
            source_request_id="req-cpf09-1",
            source_kind="ready",
            task_id="P16.99",
            source_task_id=None,
            branch=truth1.branch,
            head=truth1.head,
            worker_prompt="prompt",
            policy=policy,
        )
        self.assertIsNone(launch1)

        # 2. Repair task by adding valid canonical declaration at new commit HEAD
        (self.repo / "agent" / "next.md").write_text(
            "# P16.99 Lifecycle Core Refactor\n\n"
            "Status: **READY_TO_RUN**\n\n"
            "## Control-Plane Impact\n"
            "- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER\n"
            "- Transition boundaries: worker_launch, authority_reconciliation\n"
            "- Fault scenarios: CPF-01, CPF-06, CPF-08, CPF-09, CPF-10\n"
            "- Convergence evidence: Unambiguous single owner convergence without continue\n\n"
            "## Approved executable design\n\n"
            "Modify src/dev_orchestrator/core/lifecycle_authority.py\n",
            encoding="utf-8",
        )
        good_head = _commit(self.repo, "repair declaration at new HEAD")
        truth2 = read_repository_truth(self.repo)

        # 3. Verify replay eligibility
        ledger = executor._load_ledger()
        blocked_rec = ledger["executions"]["req-cpf09-1"]
        authority = ledger["lifecycle"]["test_proj"]
        self.assertTrue(
            _is_declaration_gate_replayable(blocked_rec, authority, truth2.head),
            "Blocked request must be eligible for replay against new HEAD",
        )

        with unittest.mock.patch.object(executor, "_active_project", return_value=False), \
             unittest.mock.patch.object(executor, "_run_broker_worker_thread", return_value=None):
            launch2 = executor._launch(
                project,
                source_request_id="req-cpf09-1",
                source_kind="ready",
                task_id="P16.99",
                source_task_id=None,
                branch=truth2.branch,
                head=truth2.head,
                worker_prompt="prompt",
                policy=policy,
            )

        # 5. Verify gate was resolved and archived
        updated_ledger = executor._load_ledger()
        updated_auth = updated_ledger["lifecycle"]["test_proj"]
        self.assertIsNone(updated_auth.get("owner_gate"), "Declaration gate must be cleared")
        self.assertEqual(updated_auth["lifecycle_state"], "EXECUTING")
        self.assertEqual(updated_auth.get("schema_version"), 2)

        # Resolved gates history contains the archived gate
        resolved_history = updated_auth.get("resolved_owner_gates")
        self.assertIsInstance(resolved_history, list)
        self.assertEqual(len(resolved_history), 1)
        self.assertEqual(resolved_history[0]["code"], "CONTROL_PLANE_DECLARATION_REQUIRED")

        # Execution row records replayed_from_block
        exec_row = updated_ledger["executions"]["req-cpf09-1"]
        self.assertEqual(exec_row["state"], "launching")
        self.assertIn("replayed_from_block", exec_row)
        self.assertEqual(exec_row["replayed_from_block"]["head"], bad_head)

    def test_generic_owner_gate_remains_fenced_and_not_repaired(self):
        """Generic owner gate (e.g. NEXT_TASK_WITHOUT_HANDOFF) is never resolved by declaration logic."""
        (self.repo / "agent" / "next.md").write_text(
            "# P16.99 Task\n\nStatus: **READY_TO_RUN**\n\n"
            "## Control-Plane Impact\n"
            "- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER\n"
            "- Transition boundaries: worker_launch\n"
            "- Fault scenarios: CPF-06, CPF-08, CPF-09, CPF-10\n"
            "- Convergence evidence: ok\n\n"
            "## Approved executable design\nModify core\n",
            encoding="utf-8",
        )
        head = _commit(self.repo, "task with generic gate")
        truth = read_repository_truth(self.repo)

        executor = TransitionExecutor(self.runtime_root, ai_execution_port=unittest.mock.MagicMock())
        with executor._lock:
            ledger = executor._load_ledger()
            auth = new_authority("test_proj", "P16.99", "READY_TO_RUN")
            auth["owner_gate"] = {
                "code": "NEXT_TASK_WITHOUT_HANDOFF",
                "reason": "generic manual gate",
            }
            auth["lifecycle_state"] = "OWNER_GATE"
            ledger.setdefault("lifecycle", {})["test_proj"] = auth
            executor._save_ledger(ledger)

        project = {
            "project_id": "test_proj",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
        }
        policy = {"engine": "aibroker"}

        launch = executor._launch(
            project,
            source_request_id="req-generic-1",
            source_kind="ready",
            task_id="P16.99",
            source_task_id=None,
            branch=truth.branch,
            head=truth.head,
            worker_prompt="prompt",
            policy=policy,
        )
        self.assertIsNone(launch)

        ledger = executor._load_ledger()
        auth = ledger["lifecycle"]["test_proj"]
        self.assertIsNotNone(auth.get("owner_gate"))
        self.assertEqual(auth["owner_gate"]["code"], "NEXT_TASK_WITHOUT_HANDOFF")


class ControlPlanePromptAndScopeGapTests(unittest.TestCase):
    """Prompt injection and review scope-gap detection tests."""

    def test_prompt_injection_for_all_roles(self):
        decl = ControlPlaneDeclaration(
            kind="declared",
            source_path="agent/next.md",
            revision="head",
            content_hash="hash",
            invariants=("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
            transition_boundaries=("worker_launch",),
            fault_scenarios=("CPF-06",),
            convergence_evidence="Single owner convergence",
        )

        for role in ("planner", "plan_reviewer", "worker", "remediator", "technical_reviewer"):
            base_prompt = f"You are role: {role}."
            injected = inject_control_plane_contract(base_prompt, role, decl)
            self.assertIn("[CONTROL_PLANE_CONTRACT_BEGIN]", injected)
            self.assertIn("SINGLE_ACTIVE_LIFECYCLE_OWNER", injected)
            self.assertIn("CPF-06", injected)
            self.assertIn("[CONTROL_PLANE_CONTRACT_END]", injected)

            # Idempotence: injecting again does not duplicate
            reinjected = inject_control_plane_contract(injected, role, decl)
            self.assertEqual(injected, reinjected)

    def test_scope_gap_detects_undeclared_runtime_surface(self):
        changed_paths = [
            "src/dev_orchestrator/core/lifecycle_authority.py",
            "frontend/components/Button.tsx",
        ]
        # Without declaration
        gaps = declared_scope_gap(None, changed_paths)
        self.assertEqual(len(gaps), 1)
        self.assertIn("lifecycle_authority.py", gaps[0])

        # With declared declaration
        decl = ControlPlaneDeclaration(
            kind="declared",
            source_path="agent/next.md",
            revision="h",
            content_hash="h",
            invariants=("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
            transition_boundaries=("plan_freeze",),
            fault_scenarios=("CPF-06",),
            convergence_evidence="ok",
        )
        gaps2 = declared_scope_gap(decl, changed_paths)
        self.assertEqual(len(gaps2), 0)


class ControlPlaneFaultRegistryTests(unittest.TestCase):
    """Fault scenario registry validation tests."""

    def test_fault_registry_validation(self):
        repo_root = Path.cwd()
        errors = validate_fault_registry(repo_root)
        self.assertEqual(errors, [], f"Fault registry must be valid: {errors}")

    def test_all_invariants_covered_by_registry(self):
        all_covered = set()
        for scenario in fault_scenarios():
            all_covered.update(scenario.invariant_codes)
        for code in INVARIANT_CODES:
            self.assertIn(code, all_covered, f"Invariant {code} must be covered in fault registry")

    def test_required_scenarios_for_subset(self):
        scenarios = required_scenarios_for(["SINGLE_ACTIVE_LIFECYCLE_OWNER"])
        self.assertTrue(len(scenarios) >= 4)
        self.assertIn("CPF-01", scenarios)
        self.assertIn("CPF-06", scenarios)
        self.assertIn("CPF-08", scenarios)
        self.assertIn("CPF-10", scenarios)


class ControlPlaneConvergenceAndTraversalEvidenceTests(unittest.TestCase):
    """Read-only evidence projections tests."""

    def test_convergence_and_traversal_projections(self):
        executor_state = {
            "lifecycle": {
                "proj_x": {
                    "current_task_id": "P16.14",
                    "lifecycle_state": "READY_TO_RUN",
                    "generation": 3,
                    "active_owner": None,
                    "owner_gate": None,
                    "resolved_owner_gates": [{"gate_id": "g1"}],
                }
            },
            "executions": {
                "e1": {"project_id": "proj_x", "task_id": "P16.14", "state": "handoff"},
            },
            "transitions": {
                "t1": {"project_id": "proj_x", "state": "completed"},
            },
        }

        conv = convergence_evidence(executor_state, project_id="proj_x")
        self.assertEqual(conv["current_task_id"], "P16.14")
        self.assertEqual(conv["lifecycle_state"], "READY_TO_RUN")
        self.assertEqual(conv["generation"], 3)
        self.assertEqual(conv["resolved_gates_count"], 1)
        self.assertEqual(conv["handoffs_count"], 1)
        self.assertFalse(conv["manual_intervention_required"])

        trav = traversal_evidence(executor_state, project_id="proj_x")
        self.assertEqual(trav["task_id"], "P16.14")
        self.assertTrue(trav["worker_launched"])


class ControlPlaneEndToEndTraversalTests(unittest.TestCase):
    """Unconditional synthetic end-to-end traversal of a representative post-P16.13 control-plane task."""

    def test_representative_control_plane_lifecycle_traversal(self):
        # 1. Task specification with canonical declaration
        task_markdown = """# P16.15 Representative Post-P16.13 Control-Plane Task
Status: **PENDING DESIGN**

## Control-Plane Impact
- Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER, CURRENT_TASK_MATCHES_ACTIVE_EXECUTION
- Transition boundaries: plan_freeze, worker_launch, special_gate_reconciliation
- Fault scenarios: CPF-01, CPF-06, CPF-08, CPF-09, CPF-10
- Convergence evidence: Unambiguous single owner convergence without manual continue.
"""
        scope = classify_control_plane_task(task_text=task_markdown)
        self.assertEqual(scope.kind, "control_plane")

        decl = parse_control_plane_declaration(task_markdown, task_id="P16.15")
        self.assertEqual(decl.kind, "declared")

        # 2. Plan generation & validation
        candidate_plan = {
            "task_id": "P16.15",
            "summary": "Implement invariant guard update",
            "implementation_steps": ["Update src/dev_orchestrator/core/lifecycle_authority.py"],
            "interfaces": [
                "Invariants: SINGLE_ACTIVE_LIFECYCLE_OWNER, CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",
                "Transition boundaries: plan_freeze, worker_launch, special_gate_reconciliation",
                "Fault scenarios: CPF-01, CPF-06, CPF-08, CPF-09, CPF-10",
                "Convergence evidence: Unambiguous single owner convergence",
            ],
            "validation": ["Exercise fault scenarios CPF-01, CPF-06, CPF-08, CPF-09, CPF-10"],
            "risks": ["Risk of regression"],
            "out_of_scope": ["Other subsystems"],
        }
        gate = require_control_plane_declaration("proj", "P16.15", scope, decl, plan=candidate_plan)
        self.assertTrue(gate.allowed, f"Plan validation must pass: {gate.reason}")

        # 3. Plan render alongside approved executable design
        rendered_next = AIPlannerCoordinator._render_next(task_markdown, candidate_plan, "Plan approved")
        self.assertIn("## Control-Plane Impact", rendered_next)
        self.assertIn("## Approved executable design", rendered_next)

        # 4. Prompt injection for technical reviewer
        review_prompt = "Initial review prompt."
        injected_review_prompt = inject_control_plane_contract(review_prompt, "technical_reviewer", decl)
        self.assertIn("[CONTROL_PLANE_CONTRACT_BEGIN]", injected_review_prompt)

        # 5. Convergence check
        executor_state = {
            "lifecycle": {
                "proj": {
                    "current_task_id": "P16.15",
                    "lifecycle_state": "READY_TO_RUN",
                    "generation": 1,
                    "active_owner": None,
                    "owner_gate": None,
                }
            }
        }
        conv = convergence_evidence(executor_state, project_id="proj")
        self.assertEqual(conv["lifecycle_state"], "READY_TO_RUN")
        self.assertFalse(conv["manual_intervention_required"])


if __name__ == "__main__":
    unittest.main()
