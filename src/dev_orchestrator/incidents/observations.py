"""Normal post-tick source observations, independent of the optional soak harness."""
import json
import os
from pathlib import Path
from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.core.lifecycle_authority import evaluate_lifecycle_invariants
from dev_orchestrator.incidents.sanitize import sanitize_incident_payload
from dev_orchestrator.storage.json_store import read_json, utc_now_iso


def record_post_tick_observations(runtime_root, summary, executor):
    runtime = Path(runtime_root)
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
                'evidence': sanitize_incident_payload(ev)})
        rows.append({'project_id': project, 'recorded_at': utc_now_iso(), 'source': 'normal_post_tick',
            'observation_kind': 'authority_snapshot', 'task_id': authority.get('current_task_id'),
            'generation': authority.get('generation'), 'lifecycle_state': authority.get('lifecycle_state'),
            'watchdog': sanitize_incident_payload(watchdog.get('projects', {}).get(project, {})),
            'owner_gate': sanitize_incident_payload(authority.get('owner_gate')),
            'active_owner': authority.get('active_owner')})
    directory = runtime / 'reliability'
    directory.mkdir(parents=True, exist_ok=True)
    with InterProcessFileLock(directory / 'observations.lock'):
        target = directory / 'invariant-observations.jsonl'
        if target.exists() and target.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('bounded observation capacity exhausted; coverage is unresolved')
        with (directory / 'invariant-observations.jsonl').open('a', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
