"""Local preparation permit and bounded foreground-to-job handoff."""
from pathlib import Path
import sys

from dev_orchestrator.soak.permit import load_soak_permit, validate_soak_permit
from dev_orchestrator.storage.json_store import read_json, write_json, utc_now_iso

BOOTSTRAP_FILE = 'p20-soak-bootstrap.json'


def prepared_bootstrap(runtime_root, repo_path, head):
    data = read_json(Path(runtime_root) / BOOTSTRAP_FILE, None)
    if not isinstance(data, dict) or data.get('prepared_head') != head:
        return None
    permit = load_soak_permit(data.get('permit_path') or '')
    ok, _ = validate_soak_permit(permit, repo_path, runtime_root)
    if not ok or data.get('permit_fingerprint') != permit.fingerprint:
        return None
    return data


def prepare_bootstrap(runtime_root, repo_path, head, permit_path):
    """Called by local CLI only after source commit; no authority/role record writes."""
    permit = load_soak_permit(permit_path)
    ok, reason = validate_soak_permit(permit, repo_path, runtime_root)
    if not ok:
        raise ValueError('invalid local permit: ' + str(reason))
    runtime = Path(runtime_root)
    existing = read_json(runtime / BOOTSTRAP_FILE, None)
    if isinstance(existing, dict) and existing.get('run_id') != permit.run_id:
        raise ValueError('another run already owns the local preparation permit')
    jobs_path = runtime / 'execution-jobs.json'
    config = read_json(jobs_path, None)
    if config is None:
        config = {'enabled': True, 'runtime_root': str(runtime.resolve()), 'projects': {}}
    if not isinstance(config, dict):
        raise ValueError('existing job configuration is invalid')
    project = config.setdefault('projects', {}).setdefault('devorchestrator', {
        'repo_path': str(Path(repo_path).resolve()), 'file_roots': [], 'commands': {},
    })
    if Path(project['repo_path']).resolve() != Path(repo_path).resolve():
        raise ValueError('job project repository identity differs')
    project.setdefault('commands', {})['p20_soak_supervisor'] = {
        'argv': [sys.executable, '-m', 'dev_orchestrator', 'soak-supervise',
                 '--permit', str(Path(permit_path).resolve())],
        'cwd': '.', 'duration_class': 'long', 'effect_class': 'idempotent',
        'max_runtime_seconds': 43200, 'heartbeat_interval_seconds': 10,
    }
    write_json(jobs_path, config)
    data = {'run_id': permit.run_id, 'permit_path': str(Path(permit_path).resolve()),
            'permit_fingerprint': permit.fingerprint, 'prepared_head': head,
            'prepared_at': utc_now_iso(), 'source': 'p20_local_cli'}
    write_json(runtime / BOOTSTRAP_FILE, data)
    return data


def bootstrap_prompt(data, source_request_id, runtime_root):
    return (
        'P20.4 IMPLEMENTATION PREPARATION IS COMPLETE AT THIS CLEAN HEAD. '
        'Your remaining bounded responsibility is to hand off to the already prepared '
        'DevO-owned durable soak job. Do not modify source, tests, agent documents, Git '
        'or the frozen plan; do not stop/restart any daemon. The current daemon already '
        'loads the binding-aware code. Use only read-only checks and the local command: '
        f'python -m dev_orchestrator soak-start --permit "{data["permit_path"]}" '
        f'--source-request-id "{source_request_id}" --runtime-root "{runtime_root}". '
        'It may already have been submitted by the local owner; the same command and '
        'job identities are idempotent. Verify its real job and binding, then return. '
        'Never claim six hours or completion; lifecycle ownership continues in the '
        'existing execution record until terminal soak evidence is independently reviewed. '
        'The full frozen direction remains the acceptance contract.'
    )


def initialize_primary(permit, manifest, *, automatic=False):
    """Initial owner-authorized startup precedes the measurement window; no later continue."""
    import subprocess
    import time
    from dev_orchestrator.platform.process import hidden_subprocess_kwargs, is_pid_alive
    runtime = Path(permit.disposable_runtime_root)
    broker = read_json(Path(permit.canonical_runtime_root) / 'aibroker-execution.json', None)
    if not isinstance(broker, dict):
        raise ValueError('real broker configuration absent')
    # Machine-local private configuration is never an evidence artifact.
    write_json(runtime / 'aibroker-execution.json', broker)
    def command(*args):
        result = subprocess.run([sys.executable, '-m', 'dev_orchestrator', *map(str, args)],
            capture_output=True, text=True, encoding='utf-8', timeout=45,
            **hidden_subprocess_kwargs())
        if result.returncode:
            raise RuntimeError('initial daemon/control startup failed; inspect isolated logs')
        return result
    daemon = read_json(runtime / 'daemon.json', {})
    if not is_pid_alive(daemon.get('pid')):
        command('start-daemon', '--config', manifest['config_path'], '--runtime-root', runtime,
                '--port', manifest['ports']['daemon_port'], '--bridge-port', manifest['ports']['bridge_port'],
                '--interval', 5)
    control_id = 'initial-' + permit.run_id
    from dev_orchestrator.control.command_store import ControlCommandStore
    store = ControlCommandStore(runtime)
    if store.get(control_id) is None:
        until = time.monotonic() + 30
        while not (runtime / 'projects' / (permit.soak_project_id + '.json')).exists():
            if time.monotonic() >= until:
                raise RuntimeError('initial primary snapshot unavailable')
            time.sleep(.5)
        if automatic:
            from dev_orchestrator.control.surface import project_control_view
            snapshot = read_json(runtime / 'projects' / (permit.soak_project_id + '.json'), None)
            store.submit({'schema_version': 1, 'command_id': control_id,
                'project_id': permit.soak_project_id, 'action': 'continue',
                'expected': project_control_view(snapshot, runtime)['control_identity'], 'target': {}},
                source='automatic_p20_initial_case_activation')
        else:
            command('project-control', permit.soak_project_id, 'continue', '--command-id', control_id,
                    '--config', manifest['config_path'], '--runtime-root', runtime)
    manifest['initial_control_id'] = control_id
    write_json(runtime / 'soak-project-manifest.json', manifest)
    return manifest
