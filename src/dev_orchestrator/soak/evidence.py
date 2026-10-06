"""Read-only source correlation; checkpoint labels never establish acceptance."""
import hashlib
from pathlib import Path
from dev_orchestrator.storage.json_store import read_json, parse_utc
from dev_orchestrator.core.repository import is_git_ancestor, read_repository_truth


def accepted_tasks(permit):
    runtime = Path(permit.disposable_runtime_root)
    plans = read_json(runtime / 'ai-planner.json', {}).get('plans', {})
    workers = read_json(runtime / 'transition-executor.json', {}).get('executions', {})
    reviews = read_json(runtime / 'ai-reviewer.json', {}).get('reviews', {})
    decisions = read_json(runtime / 'review-decisions.json', {}).get('decisions', {})
    truth = read_repository_truth(permit.disposable_repo_path)
    evidence = {}
    if not truth.valid:
        return evidence
    for number in range(1, 6):
        task = f'P204.{number}'
        for review_id, review in reviews.items():
            if (review.get('project_id') != permit.soak_project_id or review.get('task_id') != task
                    or review.get('state') != 'completed' or review.get('decision') != 'next'):
                continue
            source = review.get('source_request_id')
            worker = workers.get(source, {})
            origin = source
            seen = set()
            for _ in range(4):
                prior_worker = workers.get(origin, {})
                if prior_worker.get('source_kind') != 'remediation' or origin in seen:
                    break
                seen.add(origin)
                predecessor_review = reviews.get(prior_worker.get('review_decision_id'), {})
                if not predecessor_review or predecessor_review.get('task_id') != task:
                    origin = None
                    break
                origin = predecessor_review.get('source_request_id')
            planner = next(((key, row) for key, row in plans.items()
                if row.get('project_id') == permit.soak_project_id and row.get('task_id') == task
                and row.get('state') == 'worker_launched' and row.get('worker_source_request_id') == origin), None)
            decision = decisions.get(review_id, {})
            actuation = workers.get(review_id, {})
            normally_settled = (actuation.get('project_id') == permit.soak_project_id
                and actuation.get('task_id') == task
                and ((actuation.get('state') == 'handoff' and actuation.get('handoff_consumed') is True)
                     or (actuation.get('state') == 'settled' and actuation.get('outcome') == 'task_complete')))
            wp = (worker.get('resource_context') or {}).get('provider')
            rp = (review.get('resource_context') or {}).get('provider')
            if (not normally_settled or not planner or worker.get('state') != 'completed' or worker.get('task_id') != task
                    or worker.get('engine') != 'aibroker' or not wp or not rp or wp == rp
                    or decision.get('decision') != 'next' or decision.get('head') != review.get('head')
                    or not all(worker.get(key) for key in ('dispatch_id', 'execution_id', 'decision_id'))
                    or not all(review.get(key) for key in ('dispatch_id', 'execution_id', 'decision_id'))
                    or not is_git_ancestor(permit.disposable_repo_path, worker.get('head') or '', review.get('head') or '')
                    or not is_git_ancestor(permit.disposable_repo_path, review.get('head') or '', truth.head)):
                continue
            evidence[task] = {'planner_id': planner[0], 'worker_source_request_id': source,
                'task_id': task,
                'review_id': review_id, 'commit': review['head'], 'worker_provider': wp,
                'reviewer_provider': rp, 'accepted_at': review.get('completed_at'), 'ledger_verified': True}
        if task not in evidence:
            break  # consecutive acceptance only
    return evidence


def primary_active(permit):
    runtime = Path(permit.disposable_runtime_root)
    for name, section, states in (
        ('ai-planner.json', 'plans', {'planning', 'reviewing', 'applying', 'remediating'}),
        ('transition-executor.json', 'executions', {'launching', 'running'}),
        ('ai-reviewer.json', 'reviews', {'launching', 'running'}),
    ):
        rows = read_json(runtime / name, {}).get(section, {})
        for key, row in rows.items():
            if row.get('project_id') == permit.soak_project_id and row.get('state') in states:
                return {'role_record_id': key, 'task_id': row.get('task_id'), 'state': row.get('state')}
    return None


def _after_effect(value, effect):
    observed = parse_utc(value)
    injected = parse_utc(effect.get('injected_at'))
    return observed is not None and injected is not None and observed > injected


def worker_interrupt_recovered(permit, effect):
    """Prove a killed Worker was replaced by an independently reviewed retry.

    Recovery Workers can be launched by a durable control command, so they do
    not necessarily retain the original Planner's ``worker_source_request_id``.
    The proof therefore follows the completed Worker into its own Technical
    Review and terminal lifecycle actuation instead of requiring false Planner
    ancestry.
    """
    if (effect.get('fault_id') != 'F03_WORKER_KILLED'
            or effect.get('state') != 'injected'
            or effect.get('effect_mode') != 'owned_broker_worker_interrupt'):
        return None
    runtime = Path(permit.disposable_runtime_root)
    truth = read_repository_truth(permit.disposable_repo_path)
    if not truth.valid:
        return None
    workers = read_json(runtime / 'transition-executor.json', {}).get('executions', {})
    reviews = read_json(runtime / 'ai-reviewer.json', {}).get('reviews', {})
    decisions = read_json(runtime / 'review-decisions.json', {}).get('decisions', {})
    interrupted = next(((key, row) for key, row in workers.items()
        if row.get('project_id') == permit.soak_project_id
        and row.get('broker_request_id') == effect.get('request_id')
        and row.get('state') in {'failed', 'cancelled'}
        and _after_effect(row.get('completed_at'), effect)), None)
    if interrupted is None:
        return None
    for review_id, review in reviews.items():
        source = review.get('source_request_id')
        worker = workers.get(source, {})
        actuation = workers.get(review_id, {})
        decision = decisions.get(review_id, {})
        wp = (worker.get('resource_context') or {}).get('provider')
        rp = (review.get('resource_context') or {}).get('provider')
        settled = ((actuation.get('state') == 'handoff'
                    and actuation.get('handoff_consumed') is True)
                   or (actuation.get('state') == 'settled'
                       and actuation.get('outcome') == 'task_complete'))
        if (review.get('project_id') != permit.soak_project_id
                or worker.get('project_id') != permit.soak_project_id
                or source == interrupted[0]
                or worker.get('task_id') != review.get('task_id')
                or worker.get('state') != 'completed'
                or not _after_effect(worker.get('completed_at'), effect)
                or worker.get('engine') != 'aibroker'
                or not all(worker.get(key) for key in ('dispatch_id', 'execution_id', 'decision_id'))
                or review.get('state') != 'completed' or review.get('decision') != 'next'
                or not _after_effect(review.get('completed_at'), effect)
                or not all(review.get(key) for key in ('dispatch_id', 'execution_id', 'decision_id'))
                or decision.get('decision') != 'next'
                or decision.get('head') != review.get('head')
                or not is_git_ancestor(permit.disposable_repo_path,
                                       worker.get('head') or '', review.get('head') or '')
                or not is_git_ancestor(permit.disposable_repo_path,
                                       review.get('head') or '', truth.head)
                or not settled or not wp or not rp or wp == rp):
            continue
        return {'worker_source_request_id': source, 'task_id': review.get('task_id'),
                'review_id': review_id, 'commit': review.get('head'),
                'worker_provider': wp, 'reviewer_provider': rp,
                'accepted_at': review.get('completed_at'), 'ledger_verified': True,
                'recovery_lineage': 'durable_control_retry'}
    return None


def broker_retry_recovered(permit, effect):
    """Prove the real Broker boundary succeeded on a later Planner attempt."""
    if (effect.get('fault_id') != 'F07_BROKER_UNAVAILABLE'
            or effect.get('state') != 'injected'
            or effect.get('effect_mode') != 'injected_transport_boundary'):
        return None
    runtime = Path(permit.disposable_runtime_root)
    plans = read_json(runtime / 'ai-planner.json', {}).get('plans', {})
    workers = read_json(runtime / 'transition-executor.json', {}).get('executions', {})
    for plan_id, plan in plans.items():
        if (plan.get('project_id') != permit.soak_project_id
                or plan.get('state') != 'worker_launched'):
            continue
        source = plan.get('worker_source_request_id')
        worker = workers.get(source, {})
        attempts = plan.get('planner_attempts') or ()
        injected_attempt = next((attempt for attempt in attempts
            if attempt.get('request_id') == effect.get('request_id')
            and _after_effect(attempt.get('completed_at'), effect)), None)
        if (worker.get('project_id') != permit.soak_project_id
                or worker.get('task_id') != plan.get('task_id')
                or injected_attempt is None):
            continue
        for number, attempt in enumerate(attempts, 1):
            resource = attempt.get('resource') or {}
            if (attempt.get('status') == 'succeeded'
                    and _after_effect(attempt.get('completed_at'), effect)
                    and attempt.get('dispatch_id') and attempt.get('execution_id')
                    and resource.get('provider')):
                return {'planner_id': plan_id, 'task_id': plan.get('task_id'),
                        'worker_source_request_id': source,
                        'recovered_attempt': number,
                        'planner_provider': resource['provider'],
                        'recovered_at': attempt.get('completed_at'),
                        'ledger_verified': True}
    return None


def daemon_restart_active_recovered(permit, effect):
    """Prove a daemon PID changed while active authority stayed coherent."""
    if (effect.get('fault_id') != 'F08_DAEMON_RESTART_ACTIVE'
            or effect.get('state') != 'injected'
            or effect.get('effect_mode') != 'isolated_native_boundary'):
        return None
    before = effect.get('daemon_before') or {}
    after = effect.get('daemon_after') or {}
    pre_active = effect.get('pre_restart_active') or {}
    pre_authority = effect.get('pre_restart_authority') or {}
    if (not pre_active.get('role_record_id') or not pre_active.get('task_id')
            or pre_active.get('state') not in {'planning', 'reviewing', 'applying',
                                               'remediating', 'launching', 'running'}
            or not before.get('pid') or not after.get('pid')
            or before.get('create_time') is None or after.get('create_time') is None
            or before.get('state') not in {'running', 'degraded'}
            or after.get('state') not in {'running', 'degraded'}
            or (before.get('pid'), before.get('create_time'))
               == (after.get('pid'), after.get('create_time'))):
        return None
    runtime = Path(permit.disposable_runtime_root)
    daemon = read_json(runtime / 'daemon.json', {})
    daemon_create_time = daemon.get('create_time')
    if daemon.get('pid') and daemon_create_time is None:
        try:
            daemon_create_time = __import__('psutil').Process(int(daemon['pid'])).create_time()
        except (__import__('psutil').Error, OSError, ValueError, TypeError):
            pass
    authority = read_json(runtime / 'transition-executor.json', {}).get(
        'lifecycle', {}).get(permit.soak_project_id, {})
    invariants = authority.get('invariants')
    owner = authority.get('active_owner')
    task = pre_active['task_id']
    if (daemon.get('state') != 'running'
            or (daemon.get('pid'), daemon_create_time)
               != (after.get('pid'), after.get('create_time'))
            or authority.get('current_task_id') != task
            or authority.get('generation') != pre_authority.get('generation')
            or not isinstance(invariants, list) or not invariants
            or any(row.get('holds') is not True for row in invariants)
            or (owner is not None and owner.get('task_id') != task)):
        return None
    return {'role_record_id': pre_active['role_record_id'], 'task_id': task,
            'old_pid': before['pid'], 'new_pid': after['pid'],
            'current_task_id': task, 'generation': authority.get('generation'),
            'single_owner_converged': True, 'clock_reset': False}


def duplicate_retry_idempotent(permit, effect):
    """Prove that the injected retry replay resolved to one durable command."""
    if (effect.get('effect_mode') != 'real_idempotent_duplicate_retry_request'
            or effect.get('duplicate_same_record') is not True
            or not effect.get('retry_command_id')):
        return None
    from dev_orchestrator.control.command_store import ControlCommandStore
    row = ControlCommandStore(permit.disposable_runtime_root).get(effect['retry_command_id'])
    if (not isinstance(row, dict) or row.get('project_id') != permit.soak_project_id
            or row.get('action') != 'retry' or not row.get('request_hash')):
        return None
    return {'retry_command_id': effect['retry_command_id'],
            'request_hash': row['request_hash'], 'durable_record_count': 1}


def lost_handoff_recovered(permit):
    """Prove that the deliberately removed handoff converged from its backup."""
    runtime = Path(permit.disposable_runtime_root)
    backup = read_json(runtime / 'fault-lost-handoff-ledger-backup.json', {})
    current = read_json(runtime / 'transition-executor.json', {})
    old_handoffs = [row for row in backup.get('transitions', {}).values()
                    if row.get('project_id') == permit.soak_project_id
                    and row.get('source_task_id') and row.get('target_task_id')]
    authority = current.get('lifecycle', {}).get(permit.soak_project_id, {})
    if len(old_handoffs) != 1 or not isinstance(authority, dict):
        return None
    old = old_handoffs[0]
    rebuilt = next((row for row in current.get('transitions', {}).values()
                    if row.get('project_id') == permit.soak_project_id
                    and row.get('source_task_id') == old.get('source_task_id')
                    and row.get('target_task_id') == old.get('target_task_id')
                    and row.get('state') in {'ready', 'published', 'completed'}), None)
    handoff = next((row for row in current.get('executions', {}).values()
                    if row.get('project_id') == permit.soak_project_id
                    and row.get('source_task_id') == old.get('source_task_id')
                    and row.get('target_task_id') == old.get('target_task_id')
                    and row.get('state') == 'handoff'
                    and row.get('handoff_consumed') is True), None)
    invariants = authority.get('invariants')
    owner = authority.get('active_owner')
    current_task = authority.get('current_task_id')
    if (rebuilt is None or rebuilt.get('transition_id') != old.get('transition_id')
            or handoff is None or current_task != old.get('target_task_id')
            or not isinstance(invariants, list) or not invariants
            or any(row.get('holds') is not True for row in invariants)
            or (owner is not None and owner.get('task_id') != current_task)):
        return None
    return {'source_task_id': old['source_task_id'], 'target_task_id': old['target_task_id'],
            'transition_id': rebuilt.get('transition_id'), 'handoff_consumed': True,
            'current_task_id': current_task, 'generation': authority.get('generation'),
            'single_owner_converged': True}


def unexpected_dirt_fail_closed(permit):
    """Prove that unexpected dirt remains untouched with no active lifecycle owner."""
    repo = Path(permit.disposable_repo_path)
    marker = repo / 'injected-dirty-worktree.txt'
    truth = read_repository_truth(repo)
    runtime = Path(permit.disposable_runtime_root)
    authority = read_json(runtime / 'transition-executor.json', {}).get(
        'lifecycle', {}).get(permit.soak_project_id, {})
    if (not marker.exists() or marker.read_text(encoding='utf-8') != 'P20 explicitly permitted unsafe dirt fault\n'
            or not truth.valid or not truth.dirty or authority.get('active_owner') is not None
            or primary_active(permit) is not None
            or authority.get('lifecycle_state') not in {'PLAN_FAILED', 'OWNER_GATE', 'PENDING_DESIGN'}):
        return None
    return {'dirty_path': marker.name, 'head': truth.head,
            'lifecycle_state': authority.get('lifecycle_state'), 'active_owner': None,
            'wait_without_mutation': True}


def primary_terminal_blocker(permit):
    """Return a durable primary-pipeline blocker that cannot make soak progress.

    This is diagnostic only.  It never consumes a gate, changes a review
    decision, or manufactures final-task acceptance.
    """
    runtime = Path(permit.disposable_runtime_root)
    if primary_active(permit) is not None:
        return None
    manifest = read_json(runtime / 'soak-project-manifest.json', {})
    task_ids = manifest.get('task_ids') if isinstance(manifest, dict) else None
    final_task = task_ids[-1] if isinstance(task_ids, list) and task_ids else None
    authority = read_json(runtime / 'transition-executor.json', {}).get(
        'lifecycle', {}).get(permit.soak_project_id, {})
    current_task = authority.get('current_task_id')
    reviews = read_json(runtime / 'ai-reviewer.json', {}).get('reviews', {})
    completed_reviews = [
        (review_id, review) for review_id, review in reviews.items()
        if isinstance(review, dict)
        and review.get('project_id') == permit.soak_project_id
        and review.get('task_id') == current_task
        and review.get('state') == 'completed'
    ]
    latest = max(completed_reviews,
                 key=lambda item: (str(item[1].get('completed_at') or ''), item[0]),
                 default=None)
    if latest and (latest[1].get('decision') == 'owner_gate'
                   or latest[1].get('next_action') == 'stop'):
        review_id, review = latest
        reason = str(review.get('reason') or 'completed review requires owner decision')
        return {
            'code': ('PRIMARY_FINAL_REVIEW_OWNER_GATE'
                     if current_task and current_task == final_task
                     else 'PRIMARY_REVIEW_OWNER_GATE'),
            'task_id': current_task,
            'review_id': review_id,
            'decision': review.get('decision'),
            'next_action': review.get('next_action'),
            'completed_at': review.get('completed_at'),
            'reason_sha256': hashlib.sha256(reason.encode('utf-8')).hexdigest(),
            'reason': reason[:256],
        }
    gate = authority.get('owner_gate')
    if isinstance(gate, dict) and gate.get('reason') and not authority.get('active_owner'):
        reason = str(gate['reason'])
        return {
            'code': 'PRIMARY_AUTHORITY_OWNER_GATE',
            'task_id': current_task,
            'gate_id': gate.get('gate_id'),
            'gate_code': gate.get('code'),
            'reason_sha256': hashlib.sha256(reason.encode('utf-8')).hexdigest(),
            'reason': reason[:256],
        }
    return None


def measured_metrics(permit, run_state):
    from dev_orchestrator.incidents.reliability_telemetry import compute_reliability_telemetry
    mapping = {'manual_continue': 'manual_continue_count', 'duplicate_worker': 'duplicate_execution_count',
        'duplicate_job': 'duplicate_job_count', 'duplicate_handoff': 'duplicate_handoff_count',
        'duplicate_successor': 'duplicate_successor_activation_count',
        'lifecycle_invariant_escape': 'lifecycle_invariant_escape_count'}
    try:
        telemetry = compute_reliability_telemetry(permit.disposable_runtime_root,
            run_state['reliability_run_id']).to_dict()
    except (KeyError, ValueError, OSError, TypeError):
        return {}, {}, None
    values, coverage = {}, {}
    for key, metric in mapping.items():
        row = telemetry['metrics'].get(metric, {})
        values[key] = row.get('value') if row.get('coverage') == 'full' else None
        coverage[key] = {'coverage': row.get('coverage', 'unknown'), 'source': row.get('source'),
                        'reason': row.get('coverage_reason')}
    values_extra, coverage_extra = observed_hard_zeros(permit, run_state)
    values.update(values_extra)
    coverage.update(coverage_extra)
    return values, coverage, telemetry


def observed_hard_zeros(permit, run_state):
    import json
    runtime = Path(permit.disposable_runtime_root)
    start, end = parse_utc(run_state.get('started_at')), parse_utc(run_state.get('updated_at'))
    from dev_orchestrator.incidents.observations import observation_directory
    source = runtime / 'reliability'
    keys = ('lifecycle_repair', 'wrong_task_execution', 'fabricated_successor', 'silent_stall',
            'infinite_retry', 'restart_regression', 'lost_lifecycle_authority')
    unknown = ({key: None for key in keys}, {key: {'coverage': 'unknown', 'source': str(source)} for key in keys})
    try:
        source = observation_directory(runtime, run_state.get('reliability_run_id'))
        if not start or not end:
            return unknown
        from dev_orchestrator.incidents.observations import read_observations
        rows = read_observations(runtime, run_state.get('reliability_run_id'))
        rows = [row for row in rows if row.get('project_id') == permit.soak_project_id
                and start <= parse_utc(row['recorded_at']) <= end]
        snapshots = [row for row in rows if row.get('observation_kind') == 'authority_snapshot'
                     and row.get('source') == 'normal_post_tick']
        times = [start] + [parse_utc(row['recorded_at']) for row in snapshots] + [end]
        if not snapshots or any((right - left).total_seconds() > 30. or right < left
                                 for left, right in zip(times, times[1:])):
            return unknown
        values = {key: 0 for key in keys}
        prior_generation, prior_task = -1, -1
        for row in snapshots:
            task = row.get('task_id')
            if task not in {f'P204.{i}' for i in range(1, 6)}:
                values['fabricated_successor'] += 1
            current_task = int(task[-1]) if isinstance(task, str) and task.startswith('P204.') and task[-1].isdigit() else -1
            generation = row.get('generation')
            if not isinstance(generation, int) or current_task < 0:
                values['lost_lifecycle_authority'] += 1
            elif generation < prior_generation or current_task < prior_task:
                values['restart_regression'] += 1
            prior_generation, prior_task = generation or 0, current_task
            watchdog = row.get('watchdog')
            if not isinstance(watchdog, dict) or not watchdog.get('last_checked_at'):
                return unknown
            stall = watchdog.get('stall') or {}
            detected = parse_utc(stall.get('detected_at'))
            if detected and (parse_utc(row['recorded_at']) - detected).total_seconds() > 300. and not row.get('owner_gate') and not watchdog.get('owner_gate'):
                values['silent_stall'] += 1
            if any(count > 3 for count in watchdog.get('attempt_counts', {}).values()):
                values['infinite_retry'] += 1
        values['wrong_task_execution'] = sum(1 for row in rows if row.get('invariant_code') == 'CURRENT_TASK_MATCHES_ACTIVE_EXECUTION'
            and row.get('holds') is False and row.get('actuation_observed'))
        audit = [json.loads(line) for line in (runtime / 'control/audit.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        values['lifecycle_repair'] = len({row.get('command_id') for row in audit
            if row.get('project_id') == permit.soak_project_id and row.get('action') in {'reconcile', 'approve_owner_gate'}
            and start <= parse_utc(row.get('timestamp') or row.get('recorded_at')) <= end})
        coverage = {key: {'coverage': 'full', 'source': str(source),
            'reason': 'continuous normal post-tick observations and run-scoped control audit'} for key in keys}
        return values, coverage
    except (OSError, ValueError, TypeError, KeyError):
        return unknown


def observed_faults(permit, run_state):
    from dev_orchestrator.soak.permit import load_soak_permit, validate_soak_permit
    from dev_orchestrator.soak.faults import disposition_allowed
    runtime = Path(permit.disposable_runtime_root)
    driver = read_json(runtime / 'fault-driver.json', {})
    found = {}
    for fault_id, declared in driver.get('dispositions', {}).items():
        if not isinstance(declared, dict):
            continue
        if not disposition_allowed(fault_id, declared.get('disposition')):
            continue
        case = Path(declared.get('case_runtime_root') or '').resolve()
        if not case.is_relative_to(runtime.resolve() / 'fault-cases'):
            continue
        case_permit = load_soak_permit(declared.get('case_permit_path') or '')
        ok, _ = validate_soak_permit(case_permit, permit.canonical_repo_path, permit.canonical_runtime_root)
        if (not ok or Path(case_permit.disposable_runtime_root).resolve() != case
                or case_permit.allowed_fault_ids != [fault_id]):
            continue
        effect = read_json(case / 'fault-effect.json', {})
        injection = read_json(case / 'soak-faults' / (str(effect.get('injection_id')) + '.json'), {})
        injected = parse_utc(effect.get('injected_at'))
        if (effect.get('state') != 'injected' or effect.get('fault_id') != fault_id
                or not injected or injected < parse_utc(run_state['started_at'])
                or injected > parse_utc(run_state['updated_at']) or injection.get('mode') != 'live_injection'
                or injection.get('fault_id') != fault_id or injection.get('run_id') != case_permit.run_id):
            continue
        index = read_json(case / 'incident-packets/index.json', {})
        family = next((row for row in index.get('families', {}).values()
                       if row.get('family_id') == declared.get('failure_family')), None)
        if (not family or family.get('project_id') != case_permit.soak_project_id
                or declared.get('incident_id') not in (family.get('packet_ids') or [family.get('packet_id')])
                or not any((row.get('occurrence_key') if isinstance(row, dict) else row) == effect['injection_id']
                           for row in family.get('occurrences', []))):
            continue
        if declared.get('disposition') == 'automatic_recovery':
            if fault_id.startswith('F13'):
                if lost_handoff_recovered(case_permit) is None:
                    continue
            elif fault_id.startswith('F14'):
                if duplicate_retry_idempotent(case_permit, effect) is None:
                    continue
            else:
                accepted = accepted_tasks(case_permit).get(declared.get('evidence', {}).get('accepted_task', {}).get('task_id'))
                if not accepted or parse_utc(accepted['accepted_at']) < injected:
                    continue
        elif declared.get('disposition') == 'owner_gate':
            authority = read_json(case / 'transition-executor.json', {}).get('lifecycle', {}).get(case_permit.soak_project_id, {})
            gate = authority.get('owner_gate')
            recorded_gate = declared.get('evidence', {}).get('owner_gate') or {}
            identity_fields = ('gate_id', 'code', 'task_id', 'reason')
            if (not gate or not gate.get('reason') or authority.get('active_owner') or primary_active(case_permit)
                    or not declared.get('evidence', {}).get('stable_after_restart')
                    or any(gate.get(key) != recorded_gate.get(key) for key in identity_fields)):
                continue
            from dev_orchestrator.incidents.reliability_runs import get_daemon_boots
            boots = get_daemon_boots(case)
            if not boots or parse_utc(boots[-1]['started_at']) <= injected:
                continue
            import json
            from dev_orchestrator.incidents.observations import read_observations, _compact_gate
            case_run_id = read_json(case / 'soak-project-manifest.json', {}).get('reliability_run_id')
            observations = read_observations(case, case_run_id)
            compact_identity = _compact_gate(gate)
            after_restart = [row for row in observations if row.get('observation_kind') == 'authority_snapshot'
                and parse_utc(row['recorded_at']) >= parse_utc(boots[-1]['started_at'])
                and not row.get('active_owner') and isinstance(row.get('owner_gate'), dict)
                and (all(row['owner_gate'].get(key) == compact_identity.get(key)
                         for key in ('gate_id', 'code', 'task_id', 'reason_sha256'))
                     if row['owner_gate'].get('reason_sha256') else
                     all(row['owner_gate'].get(key) == gate.get(key) for key in identity_fields))]
            if len(after_restart) < 3:
                continue
        elif declared.get('disposition') == 'fail_closed':
            if (not fault_id.startswith('F11')
                    or not declared.get('evidence', {}).get('stable_after_restart')
                    or unexpected_dirt_fail_closed(case_permit) is None):
                continue
            from dev_orchestrator.incidents.reliability_runs import get_daemon_boots
            boots = get_daemon_boots(case)
            if not boots or parse_utc(boots[-1]['started_at']) <= injected:
                continue
        else:
            continue
        if fault_id.startswith('F14') and (effect.get('effect_mode') != 'real_idempotent_duplicate_retry_request'
                                          or not effect.get('duplicate_same_record')):
            continue
        if fault_id.startswith('F15') and family.get('recurrence_count', 0) < 2:
            continue
        found[fault_id] = declared
    return found


def start_cpf_verification(permit, source_revision):
    import hashlib
    from dev_orchestrator.jobs.models import JobSpec
    from dev_orchestrator.jobs.service import JobService
    from dev_orchestrator.config import REPO_ROOT
    paths = ('tests_py/test_p17_cpf_scenarios_part1.py', 'tests_py/test_p17_cpf_scenarios_part2.py')
    job = JobService(permit.disposable_runtime_root).submit(JobSpec(
        project_id=permit.soak_project_id, command_ref='cpf_replay',
        idempotency_key='cpf:' + permit.run_id, source_request_id=permit.run_id,
        stage_run_id='CPF-verification', parameters_digest=source_revision),
        input_payload={'source_revision': source_revision,
                       'file_hashes': {path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() for path in paths}})
    return job.job_id


def cpf_proofs(permit, run_state):
    import hashlib
    import xml.etree.ElementTree as ET
    from dev_orchestrator.jobs.service import JobService
    from dev_orchestrator.config import REPO_ROOT
    service = JobService(permit.disposable_runtime_root)
    job = service.status(run_state.get('cpf_job_id') or '')
    if (job is None or job.state != 'completed' or job.project_id != permit.soak_project_id
            or job.command_ref != 'cpf_replay' or job.source_request_id != permit.run_id
            or job.parameters_digest != run_state.get('source_revision')):
        return {}
    payload = service.store.get_input_artifact(job.job_id)
    if not isinstance(payload, dict) or payload.get('source_revision') != job.parameters_digest:
        return {}
    if any(hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() != digest
           for path, digest in payload.get('file_hashes', {}).items()):
        return {}
    root = ET.parse(service.store._job_dir(job.job_id) / 'cpf.xml').getroot()
    results = {}
    for item in root.iter('testcase'):
        name = item.get('name', '')
        for number in range(1, 11):
            if name.startswith(f'test_cpf_{number:02d}_') and not any(item.find(kind) is not None for kind in ('failure', 'error', 'skipped')):
                results[f'CPF-{number:02d}'] = {'mode': 'deterministic_test', 'outcome': 'PASS',
                    'job_id': job.job_id, 'test_name': name, 'source_revision': job.parameters_digest}
    return results
