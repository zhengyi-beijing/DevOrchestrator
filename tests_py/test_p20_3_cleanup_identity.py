"""Safety probes mock every process operation; no real process is terminated."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dev_orchestrator.core.ai_planner import cleanup_stale_role_tool_children


def setup_session(tmp_path):
    (tmp_path / 'harness-sessions.json').write_text(json.dumps({'sessions': {'s1': {
        'session_id': 's1', 'role': 'reviewer', 'request_id': 'request-1',
        'resource_id': 'resource-1', 'started_at': '2026-10-04T00:00:00+00:00',
        'child_pids': [424242], 'child_create_times': {'424242': 1791060000.0},
        'harness_pid': 313131, 'harness_create_time': 1791060000.0,
        'terminal': True, 'read_only': True,
    }}}), encoding='utf-8')
    process = MagicMock()
    process.cmdline.return_value = ['python', '-m', 'graphify', 'query', 'question']
    process.create_time.return_value = 1791080000.0  # PID reused after the recorded child
    process.ppid.return_value = 989898  # unrelated parent
    fake_psutil = MagicMock()
    fake_psutil.Process.return_value = process
    return fake_psutil, process


def test_cleanup_without_exact_request_and_resource_identity_does_not_signal(tmp_path):
    fake_psutil, process = setup_session(tmp_path)
    with patch.dict(sys.modules, {'psutil': fake_psutil}):
        cleanup_stale_role_tool_children(tmp_path, max_age_seconds=600,
                                        now=datetime(2026, 10, 4, 3, 40, tzinfo=timezone.utc))
    process.terminate.assert_not_called()


@pytest.mark.parametrize('argv', [[], ['python', '-m', 'dev_orchestrator.daemon'],
    ['node', 'request-1', 'resource-1'], ['graphify', 'update', 'request-1 resource-1'],
    ['graphify', 'update', 'query', 'request-1 resource-1']])
def test_cleanup_never_signals_generic_or_mutating_process(tmp_path, argv):
    fake_psutil, process = setup_session(tmp_path)
    process.create_time.return_value = 1791060000.0
    process.cmdline.return_value = argv
    with patch.dict(sys.modules, {'psutil': fake_psutil}):
        assert cleanup_stale_role_tool_children(tmp_path, request_id='request-1', resource_id='resource-1',
            now=datetime(2026, 10, 4, 3, 40, tzinfo=timezone.utc)) == 0
    process.terminate.assert_not_called()


def test_job_heartbeat_registers_spawn_identity_without_reopening_pid(tmp_path):
    from dev_orchestrator.review.harness import DefaultReviewerHarness
    from dev_orchestrator.core.ai_planner import persist_harness_session
    persist_harness_session(tmp_path, 'review-1', request_id='request-1', resource_id='resource-1')
    session = SimpleNamespace(state='running', result=None, job_id='job-1', timestamps={}, session_id='review-1')
    jobs = MagicMock()
    jobs.status.return_value = SimpleNamespace(state='running', heartbeat={
        'pid': 11, 'start_token': 'owned-start', 'child_pid': 22, 'child_create_time': 123.0},
        supervisor={'pid': 11, 'start_token': 'owned-start'})
    harness = DefaultReviewerHarness(tmp_path, job_service=jobs)
    harness.session_store = MagicMock()
    harness.session_store.get_session.return_value = session
    harness.status('review-1')
    row = json.loads((tmp_path / 'harness-sessions.json').read_text())['sessions']['review-1']
    assert row['child_pids'] == [22]
    assert row['child_create_times'] == {'22': 123.0}
    assert row['resource_id'] == 'resource-1'


def test_normal_harness_status_binds_actual_reviewer_dispatch_and_timeout_cleanup(tmp_path):
    from dev_orchestrator.review.harness import DefaultReviewerHarness
    session = SimpleNamespace(state='running', result=None, job_id='job-1', session_id='review-1',
        timestamps={'started_at': '2026-10-04T00:00:00+00:00'})
    jobs = MagicMock()
    jobs.status.return_value = SimpleNamespace(state='running', heartbeat={
        'pid': 11, 'start_token': 'owned-start', 'child_pid': 22, 'child_create_time': 123.0},
        supervisor={'pid': 11, 'start_token': 'owned-start'})
    jobs.get_artifact.return_value = {'content': {'session_id': 'review-1', 'role': 'reviewer', 'request_id': 'packet-request-1'}}
    port = MagicMock()
    port.status.return_value = {'request_id': 'packet-request-1', 'resource_id': 'actual-reviewer-resource', 'status': 'running'}
    harness = DefaultReviewerHarness(tmp_path, job_service=jobs, execution_port=port)
    harness.session_store = MagicMock()
    harness.session_store.get_session.return_value = session
    harness.status('review-1')
    rows = json.loads((tmp_path/'harness-sessions.json').read_text())['sessions']
    assert rows['packet-request-1']['resource_id'] == 'actual-reviewer-resource'
    assert rows['packet-request-1']['request_id'] == 'packet-request-1'
    assert rows['packet-request-1']['child_create_times'] == {'22': 123.0}
    with patch('dev_orchestrator.core.ai_planner.cleanup_stale_role_tool_children', return_value=0) as cleanup:
        harness.cleanup_timed_out('review-1', max_age_seconds=600)
    cleanup.assert_called_once_with(harness.runtime_root, role='reviewer', max_age_seconds=600,
        request_id='packet-request-1', resource_id='actual-reviewer-resource')


def test_production_coordinator_timeout_uses_harness_correlation_and_gates(tmp_path):
    from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
    from dev_orchestrator.control.owner_store import OwnerControlStore
    coordinator = AIReviewerCoordinator(tmp_path, None)
    (tmp_path/'ai-reviewer.json').write_text(json.dumps({'reviews': {'review-1': {
        'state': 'launching', 'project_id': 'p1', 'task_id': 'T1', 'source_request_id': 'worker-1'}}}))
    harness = MagicMock()
    session = SimpleNamespace(session_id='review-1', job_id='job-1', state='running')
    harness.submit.return_value = session
    harness.status.return_value = session
    truth = SimpleNamespace(branch='main', head='abc', status_hash=None)
    with patch.object(coordinator, '_get_harness', return_value=harness):
        coordinator._run_harness_review('review-1', 'worker-1', 'p1', 'T1', str(tmp_path), truth,
            {'timeout_seconds': .01, 'poll_interval_seconds': .001},
            {'resource_context': {'resource_id': 'worker-resource', 'provider': 'worker', 'account': 'a', 'model': 'm'}},
            None, {'timeout_seconds': .01, 'independence': 'provider', 'quality': 'high'})
    harness.cleanup_timed_out.assert_called_once_with('review-1', max_age_seconds=.01)
    assert OwnerControlStore(tmp_path).is_paused('p1')


def test_cleanup_does_not_signal_reused_pid_even_when_record_has_matching_request(tmp_path):
    fake_psutil, process = setup_session(tmp_path)
    with patch.dict(sys.modules, {'psutil': fake_psutil}):
        cleanup_stale_role_tool_children(tmp_path, max_age_seconds=600,
                                        request_id='request-1', resource_id='resource-1',
                                        now=datetime(2026, 10, 4, 3, 40, tzinfo=timezone.utc))
    process.terminate.assert_not_called()
