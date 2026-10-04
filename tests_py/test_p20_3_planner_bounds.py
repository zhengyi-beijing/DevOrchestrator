"""Production coordinator probes, deterministic fixtures; not live qualification."""
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests_py'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_ai_planner import AlwaysRejectReviewerPort, ReviewerResourceFailoverPort, make_repo, wait_terminal
from dev_orchestrator.ai.contracts import ResourceContext
from dev_orchestrator.ai.contracts import AIRoleResult
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
import dev_orchestrator.core.ai_planner as planner_module
assert Path(planner_module.__file__).resolve() == (Path(__file__).resolve().parents[1] / 'src/dev_orchestrator/core/ai_planner.py').resolve()


def exercise(port, base, budget=2, reviewer_failovers=2):
    repo = base / 'repo'
    make_repo(repo)
    coordinator = AIPlannerCoordinator(base / 'runtime', port)
    project = {'project_id': 'p1', 'repo_path': str(repo), 'execution': {'engine': 'aibroker'},
               'ai_roles': {'planner': {'enabled': True, 'review_independence': 'provider',
                                       'max_plan_remediation_rounds': budget, 'max_plan_recovery_cycles': 2,
                                       'max_reviewer_resource_failovers': reviewer_failovers,
                                       'hard_total_review_rejects': 5}}}
    snapshot = {'project_id': 'p1', 'state': 'IDLE', 'next_status': '**PENDING DESIGN**',
                'telemetry': {'task_id': 'P14'}}
    plan_id, reason = coordinator.start(project, snapshot, 'bounded-probe')
    assert plan_id, reason
    return wait_terminal(coordinator, plan_id)


@pytest.mark.parametrize('budget', [1, 2, 3])
def test_semantic_budget_stops_full_planning_after_configured_remediations(budget):
    with tempfile.TemporaryDirectory(prefix='p203-bounded-') as td:
        port = AlwaysRejectReviewerPort()
        exercise(port, Path(td), budget=budget)
        assert len([r for r in port.requests if r.role == 'planner']) == 1 + budget


def test_semantic_budget_reaches_delta_adjudication_before_hard_reject_limit():
    with tempfile.TemporaryDirectory(prefix='p203-adjudication-') as td:
        port = AlwaysRejectReviewerPort()
        with patch.object(AIPlannerCoordinator, 'adjudicate_plan_delta', return_value={
            'disposition': 'owner_gate', 'reason': 'remaining concrete blocker'}) as adjudicate:
            exercise(port, Path(td))
        assert adjudicate.call_count == 1


def test_same_provider_model_account_rotation_does_not_consume_three_blind_attempts():
    class AccountRotation(ReviewerResourceFailoverPort):
        def execute(self, request):
            result = super().execute(request)
            if request.role == 'reviewer':
                return replace(result, resource_context=ResourceContext(
                    f'agy/agy-{self.review_calls}/claude-opus-4-6-thinking', 'agy',
                    f'agy-{self.review_calls}', 'claude-opus-4-6-thinking'))
            return result

    with tempfile.TemporaryDirectory(prefix='p203-account-') as td:
        port = AccountRotation(failures=2)
        exercise(port, Path(td))
        assert port.review_calls <= 2


def test_restart_and_resource_recovery_preserve_consumed_semantic_budget():
    class RejectThenQuota(AlwaysRejectReviewerPort):
        def __init__(self):
            super().__init__()
            self.review_calls = 0
        def execute(self, request):
            result = super().execute(request)
            if request.role == 'reviewer':
                self.review_calls += 1
            if request.role == 'reviewer' and self.review_calls == 2:
                return replace(result, status='failed', error='quota exhausted',
                               failure_classification='quota_exhausted', output=None)
            return result

    with tempfile.TemporaryDirectory(prefix='p203-carry-') as td:
        base = Path(td)
        port = RejectThenQuota()
        first = exercise(port, base, budget=1, reviewer_failovers=0)
        assert first['state'] == 'failed'
        before = len([r for r in port.requests if r.role == 'planner'])
        restarted = AIPlannerCoordinator(base / 'runtime', port)
        project = {'project_id': 'p1', 'repo_path': str(base / 'repo'), 'execution': {'engine': 'aibroker'},
                   'ai_roles': {'planner': {'enabled': True, 'review_independence': 'provider',
                                           'max_plan_remediation_rounds': 1, 'max_plan_recovery_cycles': 2,
                                           'hard_total_review_rejects': 5}}}
        snapshot = {'project_id': 'p1', 'state': 'IDLE', 'next_status': '**PENDING DESIGN**', 'telemetry': {'task_id': 'P14'}}
        handled, plan_id, reason = restarted.continue_failed_plan_review(project, snapshot, 'recovered-budget')
        assert handled
        assert plan_id, reason
        recovered = wait_terminal(restarted, plan_id)
        assert len([r for r in port.requests if r.role == 'planner']) == before
        assert recovered['state'] == 'owner_gate'
        assert len(recovered['rejection_chain']) == 2


def test_lowered_budget_cannot_leave_active_planning_record(tmp_path):
    import json
    from dev_orchestrator.control.owner_store import OwnerControlStore
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    row = {'plan_id': 'ai_plan:lowered', 'project_id': 'p1', 'task_id': 'P14',
           'command_id': 'lowered', 'state': 'planning', 'cumulative_remediation_rounds': 3}
    port = AlwaysRejectReviewerPort()
    coordinator = AIPlannerCoordinator(runtime, port)
    (runtime/'ai-planner.json').write_text(json.dumps({'plans': {'ai_plan:lowered': row}}), encoding='utf-8')
    coordinator._run_cycle('ai_plan:lowered', {'project_id': 'p1'}, {
        'max_plan_remediation_rounds': 1, 'max_plan_recovery_cycles': 2, 'hard_total_review_rejects': 5})
    assert coordinator.state()['plans']['ai_plan:lowered']['state'] == 'owner_gate'
    assert OwnerControlStore(runtime).is_paused('p1')
    assert not port.requests
