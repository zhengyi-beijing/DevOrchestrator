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
    ['node', 'request-1', 'resource-1'], ['graphify', 'update', 'request-1 resource-1']])
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


def test_cleanup_does_not_signal_reused_pid_even_when_record_has_matching_request(tmp_path):
    fake_psutil, process = setup_session(tmp_path)
    with patch.dict(sys.modules, {'psutil': fake_psutil}):
        cleanup_stale_role_tool_children(tmp_path, max_age_seconds=600,
                                        request_id='request-1', resource_id='resource-1',
                                        now=datetime(2026, 10, 4, 3, 40, tzinfo=timezone.utc))
    process.terminate.assert_not_called()
