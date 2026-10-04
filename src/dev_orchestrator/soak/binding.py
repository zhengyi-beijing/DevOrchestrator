"""Evidence for an existing owned local job; this module never writes authority."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.soak.permit import load_soak_permit, validate_soak_permit
from dev_orchestrator.soak.timeline import load_soak_run
from dev_orchestrator.storage.json_store import parse_utc


def observe_owned_job(runtime_root, record, binding, *, require_live=True):
    """Require the exact job, source, permit, run and process generation."""
    permit = load_soak_permit(binding.get('permit_path') or '')
    repo = record.get('repo_path')
    if not repo:
        return None, 'execution_repository_identity_missing'
    ok, reason = validate_soak_permit(permit, repo, runtime_root)
    if not ok:
        return None, 'permit_invalid: ' + str(reason)
    if (binding.get('permit_fingerprint') != permit.fingerprint
            or binding.get('soak_run_id') != permit.run_id
            or binding.get('soak_project_id') != permit.soak_project_id):
        return None, 'binding_permit_identity_mismatch'
    try:
        service = JobService(runtime_root)
        job = service.status(binding.get('job_id') or '')
        if job is None:
            return None, 'owned_job_not_found'
        if (job.project_id != record.get('project_id') or job.task_id != 'P20.4'
                or job.source_request_id != record.get('source_request_id')
                or job.kind != 'soak' or job.command_ref != 'p20_soak_supervisor'
                or job.transport != 'local'):
            return None, 'job_execution_identity_mismatch'
        if (job.parameters_digest != permit.fingerprint
                or job.idempotency_key != f'soak:{permit.run_id}:{record.get("source_request_id")}'):
            return None, 'job_permit_identity_mismatch'
        heartbeat = service.store.get_heartbeat(job.job_id) or {}
        supervisor = job.supervisor
        token = supervisor.get('start_token')
        if not token or heartbeat.get('start_token') != token:
            return None, 'supervisor_start_token_unproved'
        if binding.get('supervisor_start_token') and binding['supervisor_start_token'] != token:
            return None, 'supervisor_start_token_mismatch'
        pid = supervisor.get('pid')
        if not pid or heartbeat.get('pid') != pid:
            return None, 'supervisor_pid_unproved'
        if binding.get('supervisor_pid') and binding['supervisor_pid'] != pid:
            return None, 'supervisor_pid_mismatch'
        run = load_soak_run(permit.disposable_runtime_root, permit.run_id)
        if (not isinstance(run, dict) or run.get('project_id') != permit.soak_project_id
                or run.get('run_id') != permit.run_id):
            return None, 'soak_run_not_started'
        if binding.get('soak_started_at') and binding['soak_started_at'] != run.get('started_at'):
            return None, 'soak_start_mismatch'
        child_pid = heartbeat.get('child_pid')
        child_created = heartbeat.get('child_create_time')
        facts = {'job_state': job.state, 'supervisor_pid': pid,
                 'supervisor_start_token': token, 'child_pid': child_pid,
                 'child_create_time': child_created, 'soak_started_at': run.get('started_at'),
                 'heartbeat_at': heartbeat.get('reported_at'), 'permit': permit}
        if not require_live and job.state in {'completed', 'failed', 'cancelled'}:
            if not binding.get('last_live_heartbeat_at'):
                return None, 'no_prior_owned_live_observation'
            return facts, None
        if job.state not in {'queued', 'running'}:
            return None, 'job_liveness_unresolved: ' + job.state
        import psutil
        sup_created = psutil.Process(pid).create_time()
        child_actual = psutil.Process(child_pid).create_time()
        if child_created is None or abs(child_actual - float(child_created)) > .1:
            return None, 'child_process_generation_mismatch'
        if binding.get('supervisor_create_time') is not None and abs(
                sup_created - float(binding['supervisor_create_time'])) > .1:
            return None, 'supervisor_process_generation_mismatch'
        if run.get('supervisor_pid') != child_pid:
            return None, 'run_supervisor_process_mismatch'
        reported = parse_utc(heartbeat.get('reported_at'))
        checkpoint = parse_utc(run.get('updated_at'))
        now = datetime.now(timezone.utc)
        if not reported or not checkpoint or any(not -2. <= (now - stamp).total_seconds() <= 60.
                                                for stamp in (reported, checkpoint)):
            return None, 'owned_job_heartbeat_or_checkpoint_stale'
        facts['supervisor_create_time'] = sup_created
        return facts, None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return None, 'owned_job_evidence_unavailable: ' + type(exc).__name__
    except Exception as exc:
        # Process exits and access denial are UNKNOWN, never permission to relaunch.
        return None, 'owned_process_evidence_unavailable: ' + type(exc).__name__
