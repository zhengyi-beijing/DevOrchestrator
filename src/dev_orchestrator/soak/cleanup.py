"""Bounded cleanup of exact locally permitted software daemon ownership."""
from pathlib import Path
import os
import subprocess
import sys
import psutil

from dev_orchestrator.soak.permit import validate_soak_permit, load_soak_permit
from dev_orchestrator.storage.json_store import read_json, write_json, utc_now_iso
from dev_orchestrator.platform.process import hidden_subprocess_kwargs


def stop_owned_daemon(permit):
    ok, reason = validate_soak_permit(permit, permit.canonical_repo_path, permit.canonical_runtime_root)
    if not ok:
        raise ValueError('cleanup permit refused: ' + str(reason))
    runtime = Path(permit.disposable_runtime_root).resolve()
    row = read_json(runtime / 'daemon.json', {})
    pid = row.get('pid')
    if not pid or row.get('state') == 'stopped':
        return
    try:
        process = psutil.Process(int(pid))
        # The exact CLI argument authenticates historical daemons that predate
        # creation-time ownership artifacts. Never match path substrings.
        argv = process.cmdline()
        position = argv.index('--runtime-root')
        if Path(argv[position + 1]).resolve() != runtime or 'daemon' not in argv:
            raise ValueError('cleanup process ownership mismatch')
        created = process.create_time()
    except psutil.NoSuchProcess:
        return
    except (ValueError, IndexError, psutil.AccessDenied) as exc:
        raise ValueError('cleanup process identity unproved') from exc
    record_path = runtime / 'soak-daemon-owner.json'
    owner = read_json(record_path, None)
    if owner is not None and (owner.get('pid') != pid or owner.get('create_time') != created
                              or owner.get('permit_fingerprint') != permit.fingerprint):
        raise ValueError('cleanup recorded process generation mismatch')
    write_json(record_path, {'pid': pid, 'create_time': created, 'permit_fingerprint': permit.fingerprint})
    result = subprocess.run([sys.executable, '-m', 'dev_orchestrator', 'stop-daemon',
        '--runtime-root', str(runtime), '--expected-pid', str(pid), '--expected-create-time', str(created)],
        capture_output=True, timeout=30, **hidden_subprocess_kwargs())
    if result.returncode:
        raise RuntimeError('owned daemon cleanup failed')
    try:
        if psutil.Process(pid).create_time() == created:
            raise RuntimeError('owned daemon remains alive after shutdown')
    except psutil.NoSuchProcess:
        pass
    write_json(runtime / 'soak-cleanup.json', {'state': 'stopped', 'pid': pid,
        'create_time': created, 'at': utc_now_iso(), 'source': 'normal_owned_cleanup'})


def parent_expired(runtime):
    """Self-shutdown guard for a permitted case after supervisor loss/hard exit."""
    runtime = Path(runtime).resolve()
    owner = read_json(runtime / 'soak-parent-owner.json', None)
    if owner is None:
        return False
    permit = load_soak_permit(owner.get('permit_path') or '')
    parent = load_soak_permit(owner.get('parent_permit_path') or '')
    if permit is None or parent is None or Path(permit.disposable_runtime_root).resolve() != runtime:
        raise ValueError('parent ownership permit missing')
    from dev_orchestrator.config import REPO_ROOT
    for candidate in (permit, parent):
        ok, reason = validate_soak_permit(candidate, REPO_ROOT, parent.canonical_runtime_root)
        if not ok:
            raise ValueError('parent guard permit refused: ' + str(reason))
    if not runtime.is_relative_to(Path(parent.disposable_runtime_root).resolve() / 'fault-cases'):
        raise ValueError('parent guard escapes disposable cases')
    run = read_json(Path(parent.disposable_runtime_root) / f'run-{parent.run_id}.json', {})
    if run.get('status') in {'FAILED', 'QUALIFIED'}:
        return True
    try:
        return psutil.Process(int(owner['pid'])).create_time() != owner['create_time']
    except psutil.NoSuchProcess:
        return True
