"""Real isolated daemon ownership tests; no AI, faults, or production mutation."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from dev_orchestrator.config import REPO_ROOT
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.soak.cleanup import stop_owned_daemon, parent_expired
from dev_orchestrator.soak.permit import create_soak_permit, save_soak_permit
from dev_orchestrator.soak.template import generate_soak_project
from dev_orchestrator.storage.json_store import read_json, write_json


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _project(root, run, project):
    permit = create_soak_permit(run, project, root / 'repo', root / 'runtime', REPO_ROOT, REPO_ROOT / 'runtime')
    manifest = generate_soak_project(permit)
    save_soak_permit(root / 'permit.json', permit)
    # No role can execute: this fixture verifies daemon process cleanup only.
    write_json(manifest['config_path'], {'projects': []})
    return permit, manifest


def _start(permit, manifest):
    result = subprocess.run([sys.executable, '-m', 'dev_orchestrator', 'start-daemon',
        '--config', manifest['config_path'], '--runtime-root', permit.disposable_runtime_root,
        '--port', str(_port()), '--bridge-port', str(_port()), '--interval', '5'],
        capture_output=True, timeout=30, **hidden_subprocess_kwargs())
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    return read_json(Path(permit.disposable_runtime_root) / 'daemon.json', {})['pid']


def test_real_owned_daemon_shutdown_preserves_ledgers_and_is_idempotent(tmp_path):
    permit, manifest = _project(tmp_path, 'owned-cleanup', 'owned')
    runtime = Path(permit.disposable_runtime_root)
    write_json(runtime / 'evidence.json', {'original': 'retained'})
    pid = _start(permit, manifest)
    created = psutil.Process(pid).create_time()
    try:
        stop_owned_daemon(permit)
        stop_owned_daemon(permit)
        assert read_json(runtime / 'evidence.json', {}) == {'original': 'retained'}
        assert read_json(runtime / 'soak-cleanup.json', {})['create_time'] == created
        assert not psutil.pid_exists(pid)
    finally:
        stop_owned_daemon(permit)


def test_case_daemon_self_stops_after_parent_run_failure(tmp_path):
    parent, _ = _project(tmp_path / 'primary', 'parent-loss', 'primary')
    root = Path(parent.disposable_runtime_root) / 'fault-cases/F01'
    permit, manifest = _project(root, 'parent-loss-F01', 'case')
    runtime = Path(permit.disposable_runtime_root)
    pid = _start(permit, manifest)
    try:
        write_json(runtime / 'soak-parent-owner.json', {'permit_path': str(root / 'permit.json'),
            'parent_permit_path': str(tmp_path / 'primary/permit.json'), 'pid': os.getpid(),
            'create_time': psutil.Process(os.getpid()).create_time()})
        write_json(Path(parent.disposable_runtime_root) / f'run-{parent.run_id}.json', {'status': 'FAILED'})
        assert parent_expired(runtime)
        until = time.monotonic() + 15
        while psutil.pid_exists(pid) and time.monotonic() < until:
            time.sleep(.1)
        assert not psutil.pid_exists(pid)
        assert read_json(runtime / 'daemon.json', {})['reason'] == 'permitted_soak_parent_terminal_or_lost'
    finally:
        stop_owned_daemon(permit)


def test_parent_guard_refuses_escape_from_disposable_cases(tmp_path):
    parent, _ = _project(tmp_path / 'parent', 'guard', 'primary')
    permit, _ = _project(tmp_path / 'unrelated', 'guard-F01', 'case')
    runtime = Path(permit.disposable_runtime_root)
    write_json(runtime / 'soak-parent-owner.json', {'permit_path': str(tmp_path / 'unrelated/permit.json'),
        'parent_permit_path': str(tmp_path / 'parent/permit.json'), 'pid': os.getpid(), 'create_time': 1.0})
    with pytest.raises(ValueError, match='escapes'):
        parent_expired(runtime)
