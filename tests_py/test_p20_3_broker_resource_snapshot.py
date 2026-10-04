import json
import sys
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.ai.aibroker_subprocess import AIBrokerClientConfig, AIBrokerExecutionPort
from dev_orchestrator.incidents.recovery_policy import build_resource_availability


def make_port(tmp_path):
    config = tmp_path / 'broker.json'
    config.write_text(json.dumps({'resources': [{
        'resource_id': 'claude/a/opus', 'provider': 'claude', 'account': 'a', 'model': 'opus',
        'metadata': {'dispatch_roles': ['planner', 'reviewer'], 'dispatch_qualities': ['high']}}]}), encoding='utf-8')
    return AIBrokerExecutionPort(AIBrokerClientConfig(Path(sys.executable), tmp_path, config, service_url='http://127.0.0.1:18875'))


def test_snapshot_preserves_real_role_quality_and_health(tmp_path):
    port = make_port(tmp_path)
    with patch.object(port, '_service_call', return_value={'resources': [{
        'resource_id': 'claude/a/opus', 'enabled': True,
        'state': {'available': True, 'last_probe_at': '2026-10-04T04:00:00+00:00'}}]}) as call:
        snapshot = port.resource_snapshot()
    call.assert_called_once_with('/api/resources', None, timeout_seconds=5.0)
    candidates = build_resource_availability(snapshot, snapshot['routing'], role='planner', previous_provider='google', previous_model='gemini')
    assert candidates['eligible_candidates'][0]['resource_id'] == 'claude/a/opus'
    assert candidates['heterogeneous_candidates']


def test_disabled_resource_is_not_eligible_even_if_old_health_is_available(tmp_path):
    port = make_port(tmp_path)
    with patch.object(port, '_service_call', return_value={'resources': [{
        'resource_id': 'claude/a/opus', 'enabled': False, 'override_enabled': False,
        'state': {'available': True}}]}):
        snapshot = port.resource_snapshot()
    candidates = build_resource_availability(snapshot, snapshot['routing'], role='planner')
    assert not candidates['eligible_candidates']
    assert not candidates['heterogeneous_candidates']
