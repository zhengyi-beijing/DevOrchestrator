from dataclasses import replace
from datetime import datetime, timezone, timedelta

from dev_orchestrator.incidents.recovery_policy import RecoveryInputs, decide_recovery
from dev_orchestrator.incidents.recovery_ledger import reserve_recovery_attempt, reconcile_recovery_attempts, load_recovery_attempts


def healthy_inputs():
    return RecoveryInputs(
        authority={'project_id': 'p1', 'current_task_id': 'T1', 'lifecycle_generation': 1,
                   'git': {'head': 'abc', 'dirty': False},
                   'repository_projection': {'matches_authority': True}},
        owner_gate=None, incident={'incident_id': 'i1', 'failure_family': 'f1',
                                  'failure_class': 'planner_timeout', 'failure_signature': 's1'},
        family=None, previous_attempts=(), liveness=None,
        resource_availability={}, existing_effects=None, budgets={})


def test_disabled_recovery_does_not_authorize_effect():
    decision = decide_recovery(replace(healthy_inputs(), budgets={'recovery_enabled': False}))
    assert decision.action in {'OWNER_GATE', 'NO_ACTION_ALREADY_RECOVERED'}
    assert not decision.authorized_effect


def test_quarantined_family_cannot_launch_recovery():
    decision = decide_recovery(replace(healthy_inputs(), family={'state': 'quarantined'}))
    assert decision.action == 'NO_ACTION_ALREADY_RECOVERED'
    assert not decision.authorized_effect


def test_fresh_reserved_attempt_is_not_abandoned_by_tick(tmp_path):
    decision = decide_recovery(healthy_inputs())
    reserved = reserve_recovery_attempt(tmp_path, 'p1', decision)
    now = datetime.fromisoformat(reserved['reserved_at'].replace('Z', '+00:00'))
    reconcile_recovery_attempts(tmp_path, 'p1', now=now)
    assert load_recovery_attempts(tmp_path, 'p1')[0]['status'] == 'reserved'


def test_configured_reservation_lease_survives_reload_and_expires(tmp_path):
    decision = decide_recovery(healthy_inputs())
    reserved = reserve_recovery_attempt(tmp_path, 'p1', decision,
        config={'incidents': {'recovery_attempt_reservation_ttl_seconds': 17}})
    start = datetime.fromisoformat(reserved['reserved_at'].replace('Z', '+00:00'))
    assert load_recovery_attempts(tmp_path, 'p1')[0]['reservation_ttl_seconds'] == 17
    reconcile_recovery_attempts(tmp_path, 'p1', now=start + timedelta(seconds=16))
    assert load_recovery_attempts(tmp_path, 'p1')[0]['status'] == 'reserved'
    reconcile_recovery_attempts(tmp_path, 'p1', now=start + timedelta(seconds=17))
    assert load_recovery_attempts(tmp_path, 'p1')[0]['status'] == 'abandoned'
