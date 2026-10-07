"""Explicit local disposable-only fault boundary around the real execution port."""
from dataclasses import replace
from pathlib import Path
import threading
from dev_orchestrator.storage.json_store import read_json, write_json, utc_now_iso
from dev_orchestrator.soak.permit import load_soak_permit, validate_soak_permit
from dev_orchestrator.soak.faults import reserve_fault_injection, assert_target_disposable


def wrap_fault_port(port, runtime_root):
    runtime = Path(runtime_root).resolve()
    envelope = read_json(runtime / 'p20-local-fault.json', None)
    if envelope is None:
        return port
    permit = load_soak_permit((envelope or {}).get('permit_path') or '')
    from dev_orchestrator.config import REPO_ROOT
    parent = load_soak_permit((envelope or {}).get('parent_permit_path') or '')
    if (permit is None or parent is None or Path(permit.disposable_runtime_root).resolve() != runtime
            or envelope.get('fault_id') not in permit.allowed_fault_ids
            or envelope.get('fault_id') not in parent.allowed_fault_ids
            or Path(parent.canonical_repo_path).resolve() != REPO_ROOT.resolve()):
        raise ValueError('disposable fault permit identity unproved')
    for candidate in (permit, parent):
        ok, reason = validate_soak_permit(candidate, REPO_ROOT, parent.canonical_runtime_root)
        if not ok:
            raise ValueError('fault permit refused: ' + str(reason))
    if not runtime.is_relative_to(Path(parent.disposable_runtime_root).resolve() / 'fault-cases'):
        raise ValueError('case runtime outside parent disposable ownership')
    return FaultPort(port, permit, envelope['fault_id'])


class FaultPort:
    def __init__(self, port, permit, fault_id):
        self.port, self.permit, self.fault_id = port, permit, fault_id
        self.lock = threading.Lock()
        self.effect_path = Path(permit.disposable_runtime_root) / 'fault-effect.json'

    def __getattr__(self, name):
        return getattr(self.port, name)

    def _reserve_effect(self, request, attempt):
        target = {'path': self.permit.disposable_runtime_root, 'request_id': request.request_id}
        assert_target_disposable(self.permit, target)
        intent = reserve_fault_injection(self.permit.disposable_runtime_root, self.permit.run_id,
            self.fault_id, 'live_injection', target,
            {'role': request.role, 'request_id': request.request_id})
        effect = {'fault_id': self.fault_id, 'attempt': attempt + 1,
            'injection_id': intent['injection_id'], 'request_id': request.request_id,
            'role': request.role, 'reserved_at': utc_now_iso(),
            'effect_mode': 'injected_execution_boundary', 'state': 'reserved'}
        write_json(self.effect_path, effect)
        return effect

    def execute(self, request):
        # Preserve real execution routing and independent-resource selection for all ordinary calls.
        fid = self.fault_id
        role = 'worker' if fid.startswith('F03') else 'reviewer' if fid.startswith(('F04', 'F14')) else 'planner'
        if request.project_id != self.permit.soak_project_id or request.role != role:
            return self.port.execute(request)
        if fid.startswith(('F04', 'F14')) and request.stage_run_id != 'review':
            return self.port.execute(request)
        if Path(request.working_directory).resolve() != Path(self.permit.disposable_repo_path).resolve():
            raise ValueError('fault request repository differs from owned case')

        maximum = 4 if fid.startswith('F15') else 1
        previous = read_json(self.effect_path, {})
        attempt = int(previous.get('attempt', 0))
        if attempt >= maximum or fid[:3] not in {'F01', 'F02', 'F03', 'F04', 'F05', 'F06', 'F07', 'F14', 'F15'}:
            return self.port.execute(request)

        # Response-boundary faults consume an attempt only after a real upstream
        # role succeeds. Resource starvation is not evidence that the requested
        # fault was injected and must not exhaust the injection budget.
        if fid.startswith(('F01', 'F04', 'F05', 'F06', 'F14', 'F15')):
            result = self.port.execute(request)
            if result.status != 'succeeded':
                return result
            with self.lock:
                previous = read_json(self.effect_path, {})
                attempt = int(previous.get('attempt', 0))
                if attempt >= maximum:
                    return result
                effect = self._reserve_effect(request, attempt)
            if fid.startswith(('F01', 'F15')):
                changed = replace(result, output='P20 isolated malformed response: no protocol envelope')
            elif fid.startswith('F04'):
                changed = replace(result, output='{"decision":"reject","reason":"P20 injected rejection: include a regression proving empty normalized labels raise ValueError in the implementation verification plan."}')
            elif fid.startswith('F05'):
                changed = replace(result, status='failed', output=None, error='P20 isolated provider quota exhausted',
                                  failure_classification='quota_exhausted')
            elif fid.startswith('F06'):
                changed = replace(result, status='failed', output=None, error='P20 isolated provider temporarily unavailable',
                                  failure_classification='provider_temporarily_unavailable')
            else:
                effect.update(state='triggered', injected_at=utc_now_iso(),
                    effect_mode='injected_transient_technical_review_failure', dispatch_id=result.dispatch_id,
                    execution_id=result.execution_id)
                write_json(self.effect_path, effect)
                from dev_orchestrator.ai.aibroker_subprocess import AIBrokerInvocationError
                raise AIBrokerInvocationError('P20 isolated transient technical review failure before duplicate retry')
            effect.update(state='injected', injected_at=utc_now_iso(), dispatch_id=result.dispatch_id,
                          execution_id=result.execution_id, effect_mode='injected_real_response_boundary')
            write_json(self.effect_path, effect)
            return changed

        with self.lock:
            previous = read_json(self.effect_path, {})
            attempt = int(previous.get('attempt', 0))
            if attempt >= maximum:
                return self.port.execute(request)
            effect = self._reserve_effect(request, attempt)
        if fid.startswith('F07'):
            # Actual call fails at the local transport boundary; the production Broker stays untouched.
            effect.update(state='injected', injected_at=utc_now_iso(), effect_mode='injected_transport_boundary')
            write_json(self.effect_path, effect)
            from dev_orchestrator.ai.aibroker_subprocess import AIBrokerInvocationError
            raise AIBrokerInvocationError('P20 isolated injected AIBroker transport unavailable')
        if fid.startswith('F03'):
            dispatch_done = threading.Event()
            def interrupt_owned():
                while not dispatch_done.wait(.5):
                    try:
                        fact = self.port.status(request.request_id)
                    except Exception:
                        continue
                    if dispatch_done.is_set():
                        return
                    if isinstance(fact, dict) and fact.get('request_id') == request.request_id and fact.get('status') == 'running':
                        try:
                            result = self.port.interrupt(
                                request.request_id, 'P20 isolated owned Worker killed once')
                        except Exception:
                            continue
                        if (not isinstance(result, dict)
                                or result.get('request_id') != request.request_id
                                or result.get('status') not in {'failed', 'cancelled'}
                                or result.get('interrupt_supported') is not True):
                            continue
                        # Broker's normal managed process-tree interruption API owns target verification.
                        effect.update(state='injected', injected_at=utc_now_iso(),
                            effect_mode='owned_broker_worker_interrupt',
                            interrupt_status=result.get('status'),
                            interrupt_supported=result.get('interrupt_supported'),
                            interrupt_dispatch_id=result.get('dispatch_id'),
                            interrupt_execution_id=result.get('execution_id'))
                        write_json(self.effect_path, effect)
                        return
            poller = threading.Thread(target=interrupt_owned, daemon=True)
            poller.start()
            try:
                result = self.port.execute(request)
            finally:
                dispatch_done.set()
                poller.join(timeout=5)
                current = read_json(self.effect_path, {})
                if (current.get('injection_id') == effect.get('injection_id')
                        and current.get('state') == 'reserved'):
                    current.update(state='not_injected', attempt=0,
                        reason='owned Worker completed before a terminal interrupt was confirmed')
                    write_json(self.effect_path, current)
            return result
        if fid.startswith('F02'):
            # Real provider dispatch with an explicitly injected short timeout, never a mock clock.
            result = self.port.execute(replace(request, timeout_seconds=1.))
            effect.update(state='injected', injected_at=utc_now_iso(), effect_mode='real_dispatch_timeout',
                dispatch_id=result.dispatch_id, execution_id=result.execution_id)
            write_json(self.effect_path, effect)
            return result
        return self.port.execute(request)
