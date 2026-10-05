"""Read-only source correlation; checkpoint labels never establish acceptance."""
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
    runtime = Path(permit.disposable_runtime_root)
    driver = read_json(runtime / 'fault-driver.json', {})
    found = {}
    for fault_id, declared in driver.get('dispositions', {}).items():
        if not isinstance(declared, dict):
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
