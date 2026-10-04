"""An unresolved blocker mentioned in a plan is not evidence of its resolution."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator


def test_unresolved_blocker_is_not_resolved_by_textual_presence_in_plan(tmp_path):
    coordinator = AIPlannerCoordinator(tmp_path, None)
    blocker = 'durability is missing'
    plan = {'task_id': 'P14', 'summary': 'Existing candidate plan',
            'risks': [blocker], 'implementation_steps': ['Investigate the unresolved durability risk']}
    adjudication = coordinator.adjudicate_plan_delta(
        'p1', 'P14', accepted_contract=plan,
        unresolved_delta={'blocking_finding': blocker, 'rejection_reason': blocker})
    assert adjudication['disposition'] == 'owner_gate'
