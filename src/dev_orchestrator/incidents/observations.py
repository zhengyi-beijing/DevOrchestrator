"""Normal post-tick source observations, independent of the optional soak harness."""
import json
import os
import hashlib
from pathlib import Path
from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.core.lifecycle_authority import evaluate_lifecycle_invariants
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

SEGMENT_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 16
MAX_TOTAL_BYTES = 128 * 1024 * 1024


class ObservationFailure(ValueError):
    def __init__(self, project_id, cause):
        super().__init__('run-scoped observation unavailable: ' + type(cause).__name__)
        self.project_ids = {project_id}


def observation_directory(runtime_root, run_id=None):
    directory = Path(runtime_root) / 'reliability'
    if run_id is None:
        return directory  # Historical, pre-remediation observations.
    from dev_orchestrator.incidents.reliability_runs import get_reliability_run
    run = get_reliability_run(runtime_root, run_id)
    if not run:
        raise ValueError('unknown observation run')
    if run.get('observation_store_version') == 2:
        if not isinstance(run_id, str) or not run_id.startswith('run-') or not run_id[4:].isalnum():
            raise ValueError('invalid observation run identity')
        return directory / 'observations' / run_id
    return directory


def _segments(directory):
    index_path = directory / 'observations-index.json'
    if not index_path.exists():
        if list(directory.glob('invariant-observations-*.jsonl')):
            raise ValueError('unindexed observation segment')
        return []
    index = json.loads(index_path.read_text(encoding='utf-8'))
    if index.get('schema_version') != 1 or not isinstance(index.get('segments'), list):
        raise ValueError('invalid observation index')
    segments = index['segments']
    if any(not isinstance(row, dict) for row in segments):
        raise ValueError('invalid observation segment')
    names = [row.get('name') for row in segments]
    expected = [f'invariant-observations-{i:06d}.jsonl' for i in range(1, len(names) + 1)]
    if names != expected or len(names) >= MAX_SEGMENTS:
        raise ValueError('invalid segment sequence')
    if set(names) != {p.name for p in directory.glob('invariant-observations-*.jsonl')}:
        raise ValueError('missing or unindexed observation segment')
    return segments


def read_observations(runtime_root, run_id=None):
    """Read every retained segment; corrupt or missing history is never zero."""
    directory = observation_directory(runtime_root, run_id)
    if not directory.is_dir():
        raise FileNotFoundError('observation store not produced')
    with InterProcessFileLock(directory / 'observations.lock'):
        segments = _segments(directory)
        if sum(path.stat().st_size for path in directory.glob('invariant-observations*.jsonl')) > MAX_TOTAL_BYTES:
            raise ValueError('observation storage exceeds bounded total')
        # Copy a bounded active segment while locked; immutable sealed files are
        # verified and decoded after releasing the append lock.
        active = directory / 'invariant-observations.jsonl'
        active_data = active.read_bytes()
    rows = []
    for segment in segments:
        data = (directory / segment['name']).read_bytes()
        if len(data) != segment['bytes'] or hashlib.sha256(data).hexdigest() != segment['sha256']:
            raise ValueError('sealed observation segment changed')
        rows.extend(json.loads(line) for line in data.decode('utf-8').splitlines() if line.strip())
    rows.extend(json.loads(line) for line in active_data.decode('utf-8').splitlines() if line.strip())
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('invalid observation row')
    return rows


def _append_rows(directory, rows):
    payload = ''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
                      for row in rows).encode('utf-8')
    if len(payload) > SEGMENT_BYTES:
        raise ValueError('observation batch exceeds bounded segment')
    with InterProcessFileLock(directory / 'observations.lock'):
        segments = _segments(directory)
        if sum(path.stat().st_size for path in directory.glob('invariant-observations*.jsonl')) + len(payload) > MAX_TOTAL_BYTES:
            raise ValueError('bounded observation total capacity exhausted')
        target = directory / 'invariant-observations.jsonl'
        if target.exists() and target.stat().st_size + len(payload) > SEGMENT_BYTES:
            if len(segments) + 1 >= MAX_SEGMENTS:
                raise ValueError('bounded observation capacity exhausted; coverage unresolved')
            data = target.read_bytes()
            name = f'invariant-observations-{len(segments) + 1:06d}.jsonl'
            os.replace(target, directory / name)
            segments.append({'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            write_json(directory / 'observations-index.json', {'schema_version': 1, 'segments': segments})
        with target.open('ab') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


def record_post_tick_observations(runtime_root, summary, executor):
    runtime = Path(runtime_root)
    # Collection is an explicit measurement, never an unbounded production log.
    # Hold registry lock through append so closed runs cannot receive new rows.
    directory = runtime / 'reliability'
    registry = directory / 'runs.jsonl'
    if not registry.exists():
        return
    with InterProcessFileLock(directory / 'runs.lock'):
        runs = [json.loads(line) for line in registry.read_text(encoding='utf-8').splitlines() if line.strip()]
        runs = [run for run in runs if run.get('closed_at') is None
                and run.get('observation_store_version') == 2]
        if not runs:
            return
        rows = _observation_rows(runtime, summary, executor)
        for run in runs:
            project = run['project_id']
            selected = [row for row in rows if row['project_id'] == project]
            if not selected:
                continue
            try:
                target = observation_directory(runtime, run['run_id'])
                target.mkdir(parents=True, exist_ok=True)
                _append_rows(target, selected)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ObservationFailure(project, exc) from exc


def _observation_rows(runtime, summary, executor):
    ledger = executor.state()
    planner = read_json(runtime / 'ai-planner.json', {})
    reviewer = read_json(runtime / 'ai-reviewer.json', {})
    decisions = read_json(runtime / 'review-decisions.json', {})
    watchdog = read_json(runtime / 'watchdog.json', {})
    rows = []
    for snapshot in (summary or {}).get('projects', []):
        project = snapshot.get('project_id')
        authority = ledger.get('lifecycle', {}).get(project)
        if not isinstance(authority, dict):
            continue
        findings = evaluate_lifecycle_invariants(snapshot=snapshot, executor_state=ledger,
            planner_state=planner, reviewer_state=reviewer, decisions_state=decisions)
        for finding in findings:
            ev = finding.evidence or {}
            if finding.code in {'CURRENT_TASK_MATCHES_ACTIVE_EXECUTION', 'TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION', 'PENDING_DESIGN_NOT_EXECUTING'}:
                affected = bool(ev.get('active_workers'))
            elif finding.code == 'SINGLE_ACTIVE_LIFECYCLE_OWNER':
                affected = len([owner for owner in ev.get('owners', []) if owner.get('role') != 'review_obligation']) > 1
            elif finding.code == 'SUCCESSOR_HANDOFF_LINEAGE_VALID':
                affected = any(row.get('handoff_consumed') for row in ev.get('invalid_handoffs', []))
            else:
                # Missing handoff is an obligation failure; pending/gated work has not actuated.
                affected = False
            rows.append({'project_id': project, 'recorded_at': utc_now_iso(), 'source': 'normal_post_tick',
                'generation': authority.get('generation'), 'task_id': authority.get('current_task_id'),
                'transition_id': authority.get('active_transition_id'), 'invariant_code': finding.code,
                'holds': finding.holds, 'actuation_observed': affected,
                'escaped_to_authority': bool(not finding.holds and affected),
                # Full evidence already lives in its source ledgers. Keep only
                # bounded correlation, never repeat growing histories each tick.
                'evidence': {'source': 'transition-executor.json'}})
        rows.append({'project_id': project, 'recorded_at': utc_now_iso(), 'source': 'normal_post_tick',
            'observation_kind': 'authority_snapshot', 'task_id': authority.get('current_task_id'),
            'generation': authority.get('generation'), 'lifecycle_state': authority.get('lifecycle_state'),
            'watchdog': _compact_watchdog(watchdog.get('projects', {}).get(project, {})),
            'owner_gate': _compact_gate(authority.get('owner_gate')),
            'active_owner': bool(authority.get('active_owner'))})
    return rows


def _compact_gate(gate):
    if not isinstance(gate, dict):
        return None
    compact = {key: gate.get(key) for key in ('code', 'gate_id', 'task_id')}
    reason = str(gate.get('reason') or '')
    compact.update(reason=reason[:256], reason_sha256=hashlib.sha256(reason.encode('utf-8')).hexdigest())
    return compact


def _compact_watchdog(row):
    counts = row.get('attempt_counts') or {}
    return {'last_checked_at': row.get('last_checked_at'),
            'stall': {'detected_at': (row.get('stall') or {}).get('detected_at')},
            'attempt_counts': {'max': max(counts.values(), default=0)},
            'owner_gate': _compact_gate(row.get('owner_gate'))}
