"""Sequential disposable cases: real effects, normal recovery, observed dispositions."""
from pathlib import Path
import os
import subprocess
import sys
import threading
import time
from dev_orchestrator.storage.json_store import read_json, write_json, utc_now_iso, parse_utc
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.soak.permit import create_soak_permit, save_soak_permit
from dev_orchestrator.soak.template import generate_soak_project, _git
from dev_orchestrator.soak.bootstrap import initialize_primary
from dev_orchestrator.soak.faults import SOAK_FAULTS, reserve_fault_injection, assert_target_disposable
from dev_orchestrator.soak.evidence import accepted_tasks, primary_active
from dev_orchestrator.soak.cleanup import stop_owned_daemon

FAILURE_CLASSES = ('malformed_ai_output', 'planner_timeout', 'worker_process_lost',
    'reviewer_rejected', 'provider_quota_exhausted', 'provider_unavailable', 'aibroker_unavailable',
    'daemon_restart_during_execution', 'daemon_restart_during_handoff', 'stale_head',
    'unexpected_dirty_worktree', 'missing_execution_record', 'lost_handoff', 'duplicate_request',
    'retry_without_progress')


class LiveFaultDriver:
    def __init__(self, permit, permit_path):
        self.permit = permit
        self.permit_path = str(Path(permit_path).resolve())
        self.path = Path(permit.disposable_runtime_root) / 'fault-driver.json'
        self.state = {'run_id': permit.run_id, 'dispositions': {}, 'pending': list(permit.allowed_fault_ids)}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._drive, daemon=True, name='p20-disposable-fault-cases')
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=45)
        if self.thread.is_alive():
            raise RuntimeError('fault driver shutdown deadline exceeded')

    def advance(self):
        with self.lock:
            return dict(self.state['dispositions']), list(self.state['pending'])

    def _save(self):
        with self.lock:
            write_json(self.path, self.state)

    def _command(self, runtime, *args):
        result = subprocess.run([sys.executable, '-m', 'dev_orchestrator', *map(str, args),
            '--runtime-root', str(runtime)], capture_output=True, encoding='utf-8', text=True,
            timeout=45, **hidden_subprocess_kwargs())
        if result.returncode:
            raise RuntimeError('isolated lifecycle command failed; inspect case logs')

    def _restart(self, permit, manifest):
        runtime = Path(permit.disposable_runtime_root)
        # Normal daemon API owns PID/process termination. Target is this exact permitted case.
        assert_target_disposable(permit, {'path': str(runtime)})
        self._command(runtime, 'stop-daemon')
        self._command(runtime, 'start-daemon', '--config', manifest['config_path'],
            '--port', manifest['ports']['daemon_port'], '--bridge-port', manifest['ports']['bridge_port'], '--interval', 5)

    def _drive(self):
        for index, fault in enumerate(SOAK_FAULTS):
            if self.stop_event.is_set():
                break
            if fault.fault_id not in self.permit.allowed_fault_ids:
                continue
            try:
                proof = self._case(index, fault)
                if proof is not None:
                    with self.lock:
                        self.state['dispositions'][fault.fault_id] = proof
                        self.state['pending'].remove(fault.fault_id)
                self._save()
            except Exception as exc:
                # A harness error is pending/failed evidence, never an expected owner gate.
                with self.lock:
                    self.state.setdefault('errors', {})[fault.fault_id] = type(exc).__name__
                self._save()

    def _case(self, index, fault):
        root = Path(self.permit.disposable_runtime_root) / 'fault-cases' / fault.fault_id
        permit = create_soak_permit(self.permit.run_id + '-' + fault.fault_id[:3],
            self.permit.soak_project_id + '-' + fault.fault_id[:3], root / 'repo', root / 'runtime',
            self.permit.canonical_repo_path, self.permit.canonical_runtime_root, [fault.fault_id])
        manifest = generate_soak_project(permit)
        try:
            return self._run_case(index, fault, permit, manifest, root)
        finally:
            try:
                stop_owned_daemon(permit)
            finally:
                from dev_orchestrator.incidents.reliability_runs import close_reliability_run
                run_id = read_json(Path(permit.disposable_runtime_root) / 'soak-project-manifest.json', {}).get('reliability_run_id')
                if run_id:
                    close_reliability_run(permit.disposable_runtime_root, run_id, closer_source='p20_case_finally')

    def _run_case(self, index, fault, permit, manifest, root):
        # Cases test one bounded task except handoff faults, which require a real successor.
        if fault.fault_id[:3] not in {'F09', 'F13'}:
            agent = Path(permit.disposable_repo_path) / 'agent'
            write_json(agent / 'staged/roadmap.json', {'schema_version': 1, 'tasks': [
                {'task_id': 'P204.1', 'successor': None, 'successor_spec_path': None}]})
            for path in (agent / 'next.md', agent / 'staged/P204.1.md'):
                content = path.read_text(encoding='utf-8').replace('Successor: P204.2', 'Successor: TBD / owner decision required')
                path.write_text(content, encoding='utf-8', newline='\n')
            _git(permit.disposable_repo_path, 'add', 'agent')
            _git(permit.disposable_repo_path, 'commit', '-m', 'test: bound isolated fault case to one task')
        runtime = Path(permit.disposable_runtime_root)
        permit_path = root / 'permit.json'
        save_soak_permit(permit_path, permit)
        write_json(runtime / 'soak-parent-owner.json', {'permit_path': str(permit_path),
            'parent_permit_path': self.permit_path, 'pid': os.getpid(),
            'create_time': __import__('psutil').Process(os.getpid()).create_time()})
        write_json(runtime / 'p20-local-fault.json', {'permit_path': str(permit_path),
            'parent_permit_path': self.permit_path,
            'fault_id': fault.fault_id})
        manifest['ports'] = {'daemon_port': 19800 + index * 2, 'bridge_port': 19801 + index * 2}
        from dev_orchestrator.incidents.reliability_runs import open_reliability_run
        manifest['reliability_run_id'] = open_reliability_run(runtime, permit.soak_project_id,
            opener_source='p20_live_fault_case')['run_id']
        write_json(runtime / 'soak-project-manifest.json', manifest)
        initialize_primary(permit, manifest, automatic=True)
        until = time.monotonic() + 1200.
        effect = None
        stable_gate, stable_since = None, None
        gate_restarted = False
        while not self.stop_event.is_set() and time.monotonic() < until:
            ledger = read_json(runtime / 'transition-executor.json', {})
            authority = ledger.get('lifecycle', {}).get(permit.soak_project_id, {})
            effect = read_json(runtime / 'fault-effect.json', effect)
            if (effect is None or (fault.fault_id.startswith('F14') and effect.get('state') == 'triggered')) and index >= 7:
                effect = self._structural_effect(permit, manifest, fault, ledger, authority)
            if effect and effect.get('state') == 'injected':
                incident = self._capture_effect(permit, fault, effect, index)
                proof = accepted_tasks(permit)
                recovered = next((item for item in proof.values()
                    if parse_utc(item['accepted_at']) > parse_utc(effect['injected_at'])), None)
                if recovered is not None:
                    return self._finish(permit, fault, effect, incident, 'automatic_recovery',
                        {'accepted_task': recovered}, manifest)
                gate = authority.get('owner_gate')
                if isinstance(gate, dict) and gate.get('reason') and not authority.get('active_owner') and not primary_active(permit):
                    identity = (gate.get('gate_id') or gate.get('code'), authority.get('current_task_id'), authority.get('generation'))
                    if identity != stable_gate:
                        stable_gate, stable_since = identity, time.monotonic()
                    elif time.monotonic() - stable_since >= 15.:
                        if not gate_restarted:
                            self._restart(permit, manifest)
                            gate_restarted = True
                            stable_since = time.monotonic()
                        else:
                            return self._finish(permit, fault, effect, incident, 'owner_gate',
                                {'owner_gate': gate, 'stable_after_restart': True,
                                 'generation': authority.get('generation'), 'task_id': authority.get('current_task_id')}, manifest)
                else:
                    stable_gate, stable_since = None, None
            self.stop_event.wait(5.)
        return None

    def _structural_effect(self, permit, manifest, fault, ledger, authority):
        fid, runtime, repo = fault.fault_id[:3], Path(permit.disposable_runtime_root), Path(permit.disposable_repo_path)
        active = primary_active(permit)
        handoffs = [row for row in ledger.get('executions', {}).values()
                    if row.get('project_id') == permit.soak_project_id and row.get('state') == 'handoff']
        if fid in {'F09', 'F13'} and not handoffs:
            return None
        if fid not in {'F09', 'F13', 'F14'} and not active:
            return None
        if fid == 'F12' and not (repo / '.devorch/status.json').exists():
            return None
        if fid == 'F14':
            # This must be an actual retry command, never a relabeled initial continue.
            from dev_orchestrator.control.command_store import ControlCommandStore
            retry = next((row for row in ControlCommandStore(runtime).recent(100)
                          if row.get('project_id') == permit.soak_project_id and row.get('action') == 'retry'), None)
            if retry is None:
                reviews = read_json(runtime / 'ai-reviewer.json', {})
                failed = next((row for row in reversed(list(reviews.get('reviews', {}).values()))
                    if row.get('project_id') == permit.soak_project_id and row.get('state') == 'failed'), None)
                if failed is None:
                    return None
                from dev_orchestrator.core.project_status import project_runtime_status
                from dev_orchestrator.core.lifecycle_projection import overlay_orchestration_lifecycle
                from dev_orchestrator.control.surface import project_control_view
                snapshot = read_json(runtime / 'projects' / (permit.soak_project_id + '.json'), {})
                snapshot = overlay_orchestration_lifecycle({'projects': [project_runtime_status(snapshot, runtime)]},
                    reviewer_state=reviews)['projects'][0]
                retry = ControlCommandStore(runtime).submit({'schema_version': 1,
                    'command_id': 'fault-retry-' + permit.run_id, 'project_id': permit.soak_project_id,
                    'action': 'retry', 'expected': project_control_view(snapshot, runtime)['control_identity'],
                    'target': {'target_id': failed['review_id']}}, source='automatic_p20_fault_duplicate_retry')
        intent = reserve_fault_injection(runtime, permit.run_id, fault.fault_id, 'live_injection',
            {'path': str(runtime), 'repo_path': str(repo)}, {'active': active, 'task_id': authority.get('current_task_id')})
        effect = {'fault_id': fault.fault_id, 'injection_id': intent['injection_id'],
                  'state': 'reserved', 'reserved_at': utc_now_iso(), 'effect_mode': 'isolated_native_boundary'}
        write_json(runtime / 'fault-effect.json', effect)
        if fid in {'F08', 'F09'}:
            self._restart(permit, manifest)
        elif fid == 'F10':
            _git(repo, 'commit', '--allow-empty', '-m', 'test: inject stale plan HEAD boundary')
        elif fid == 'F11':
            (repo / 'injected-dirty-worktree.txt').write_text('P20 explicitly permitted unsafe dirt fault\n', encoding='utf-8')
        elif fid == 'F12':
            from dev_orchestrator.core.project_status import STATUS_RELATIVE_PATH
            target = repo / STATUS_RELATIVE_PATH
            if not target.exists():
                return None
            assert_target_disposable(permit, {'path': str(target)})
            os.replace(target, runtime / 'fault-missing-projection-backup.json')
        elif fid == 'F13':
            # Actual loss of this disposable case's durable handoff ledger, preserved as evidence.
            target = runtime / 'transition-executor.json'
            assert_target_disposable(permit, {'path': str(target)})
            self._command(runtime, 'stop-daemon')
            os.replace(target, runtime / 'fault-lost-handoff-ledger-backup.json')
            self._command(runtime, 'start-daemon', '--config', manifest['config_path'],
                '--port', manifest['ports']['daemon_port'], '--bridge-port', manifest['ports']['bridge_port'], '--interval', 5)
        elif fid == 'F14':
            from dev_orchestrator.control.command_store import ControlCommandStore
            store = ControlCommandStore(runtime)
            prior = retry
            if not prior:
                return None
            request = {key: prior[key] for key in ('schema_version', 'command_id', 'project_id', 'action', 'expected', 'target')}
            duplicate = store.submit(request, source=prior['source'])
            effect['duplicate_same_record'] = duplicate.get('request_hash') == prior.get('request_hash')
            effect['effect_mode'] = 'real_idempotent_duplicate_retry_request'
            effect['retry_command_id'] = prior['command_id']
        effect.update(state='injected', injected_at=utc_now_iso())
        write_json(runtime / 'fault-effect.json', effect)
        return effect

    def _capture_effect(self, permit, fault, effect, index):
        from dev_orchestrator.incidents.capture import capture_incident
        return capture_incident(permit.disposable_runtime_root, permit.soak_project_id, 'P204.1',
            'P20_LIVE_INJECTED_FAILURE', {'failure_class': FAILURE_CLASSES[index],
                'fault_kind': fault.kind, 'contract_class': 'p20_isolated_fault_boundary'},
            effect, effect['injection_id'])

    def _finish(self, permit, fault, effect, incident, disposition, evidence, manifest):
        proof = {'fault_id': fault.fault_id, 'injection_id': effect['injection_id'],
            'case_permit_path': str(Path(permit.disposable_runtime_root).parent / 'permit.json'),
            'case_runtime_root': permit.disposable_runtime_root, 'mode': 'live_injection',
            'effect_mode': effect['effect_mode'], 'disposition': disposition,
            'incident_id': incident.get('packet_id'), 'failure_family': incident.get('family_id'),
            'observed_at': utc_now_iso(), 'evidence': evidence, 'evidence_verified': True}
        write_json(Path(permit.disposable_runtime_root) / 'fault-disposition.json', proof)
        # End the dedicated case normally; no manual primary continuation or lifecycle repair.
        return proof
