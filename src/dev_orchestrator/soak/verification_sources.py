"""Read-only live verification, including durable CPF retry lineage.

Imported by qualification after the minimum duration. An existing supervisor
keeps its original process, clock, and cached observer code throughout the run.
"""
import hashlib
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

from dev_orchestrator.config import REPO_ROOT
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.core.lifecycle_authority import INVARIANT_CODES
from dev_orchestrator.soak.cpf_runner import CPF_TEST_PATHS
from dev_orchestrator.storage.json_store import parse_utc


def cpf_retry_proofs(permit, run_state):
    store = ExecutionJobStore(permit.disposable_runtime_root, read_only=True)
    if not run_state.get('cpf_job_id'):
        return {}
    original = store.get(run_state['cpf_job_id'])
    if (original is None or original.parameters_digest != run_state.get('source_revision')
            or original.idempotency_key != 'cpf:' + permit.run_id):
        return {}
    seen, lineage, job = set(), [], original
    for _ in range(3):  # at most two remediation retries
        if (job.job_id in seen or job.project_id != permit.soak_project_id
                or job.command_ref != 'cpf_replay' or job.source_request_id != permit.run_id
                or job.stage_run_id != 'CPF-verification' or job.transport != 'local'
                or job.input_digest != original.input_digest):
            return {}
        seen.add(job.job_id)
        lineage.append(job.job_id)
        payload = store.get_input_artifact(job.job_id)
        if (not isinstance(payload, dict) or payload.get('source_revision') != original.parameters_digest
                or set(payload.get('file_hashes', {})) != set(CPF_TEST_PATHS)):
            return {}
        if any(hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() != payload['file_hashes'][path]
               for path in CPF_TEST_PATHS):
            return {}
        successor = job.retry.get('successor_job_id')
        if not successor:
            break
        previous, job = job, store.get(successor)
        if (previous.state != 'failed' or job is None or job.retry.get('retry_of') != previous.job_id
                or job.retry.get('attempt') != previous.retry.get('attempt', 1) + 1):
            return {}
    else:
        return {}
    start, end = parse_utc(run_state.get('started_at')), parse_utc(run_state.get('updated_at'))
    finished = parse_utc(job.timestamps.get('finished_at'))
    if (job.state != 'completed' or job.exit_code != 0 or not start or not end or not finished
            or not start <= finished <= end):
        return {}
    argv = job.resolved_argv
    if (len(argv) != 5 or Path(argv[0]).resolve() != Path(sys.executable).resolve()
            or argv[1:4] != ['-m', 'dev_orchestrator.soak.cpf_runner', '--job-dir']
            or Path(argv[4]).resolve() != store._job_dir(job.job_id).resolve()):
        return {}
    try:
        root = ET.parse(store._job_dir(job.job_id) / 'cpf.xml').getroot()
    except (OSError, ET.ParseError):
        return {}
    results = {}
    for item in root.iter('testcase'):
        name = item.get('name', '')
        if any(item.find(kind) is not None for kind in ('failure', 'error', 'skipped')):
            continue
        for number in range(1, 11):
            if name.startswith(f'test_cpf_{number:02d}_'):
                key = f'CPF-{number:02d}'
                if key in results:
                    return {}  # ambiguous duplicated proof
                results[key] = {'mode': 'deterministic_test', 'outcome': 'PASS',
                    'job_id': job.job_id, 'job_lineage': lineage, 'test_name': name,
                    'source_revision': original.parameters_digest}
    return results


def continuous_escape_metric(permit, run_state):
    source = Path(permit.disposable_runtime_root) / 'reliability/invariant-observations.jsonl'
    unknown = (None, {'coverage': 'unknown', 'source': str(source)})
    try:
        start, end = parse_utc(run_state.get('started_at')), parse_utc(run_state.get('updated_at'))
        if not start or not end or end < start:
            return unknown
        rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            return unknown
        rows = [row for row in rows if row.get('project_id') == permit.soak_project_id
                and start <= parse_utc(row['recorded_at']) <= end and row.get('source') == 'normal_post_tick']
        invariants = [row for row in rows if row.get('invariant_code')]
        required = set(INVARIANT_CODES)
        # Each invariant needs complete observation coverage, not just a single
        # row or an authority snapshot standing in for the other invariants.
        escapes = set()
        for code in required:
            selected = [row for row in invariants if row['invariant_code'] == code]
            times = [start] + [parse_utc(row['recorded_at']) for row in selected] + [end]
            if not selected or any(not 0 <= (b - a).total_seconds() <= 30.
                                   for a, b in zip(times, times[1:])):
                return unknown
            for row in selected:
                if (not isinstance(row.get('holds'), bool)
                        or not isinstance(row.get('escaped_to_authority'), bool)):
                    return unknown
                if row['holds'] is False and row['escaped_to_authority']:
                    escapes.add((code, row.get('transition_id'), row.get('generation'), row.get('task_id')))
        return len(escapes), {'coverage': 'full', 'source': str(source),
            'reason': 'all six invariants observed by normal post-tick hooks with gaps <=30 seconds'}
    except (OSError, ValueError, TypeError, KeyError):
        return unknown
