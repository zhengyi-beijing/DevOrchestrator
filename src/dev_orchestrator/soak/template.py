"""Tracked pure-software template for five actual, separately reviewed changes."""
from pathlib import Path
import json
import subprocess
import sys

from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.soak.permit import validate_soak_permit
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

SOAK_TASK_SPECS = (
    {'task_id': 'P204.1', 'kind': 'feature', 'title': 'Add label normalization',
     'description': 'Implement normalize_label(text) in soak_app.py. Require str, trim ends, collapse internal whitespace, lowercase; reject empty result with ValueError. Add tests for normal input, whitespace, empty and wrong type. Change only this feature.'},
    {'task_id': 'P204.2', 'kind': 'edge_fix', 'title': 'Fix division edge cases',
     'description': 'Fix divide(a,b) in soak_app.py so zero denominator raises ValueError with a useful reason and nonfinite inputs are rejected with ValueError. Preserve normal floating-point division. Add failing regression tests before the fix.'},
    {'task_id': 'P204.3', 'kind': 'refactor', 'title': 'Refactor summary without changing behavior',
     'description': 'Refactor summarize(values) to remove duplicated loops using a small internal helper. Preserve the exact sum, count, mean and empty-input result. Add behavioral equivalence tests including negative values. Do not change the public function signature or dictionary keys.'},
    {'task_id': 'P204.4', 'kind': 'validation', 'title': 'Validate parsed records',
     'description': 'Fix parse_record(text), which currently accepts invalid IDs and nonfinite amounts. Accept exactly ID,amount; trim fields; require a nonempty ASCII identifier containing letters, digits, hyphen or underscore, and finite numeric amount. Reject missing/extra fields and invalid values with ValueError. Add regressions proving the former invalid cases fail.'},
    {'task_id': 'P204.5', 'kind': 'api', 'title': 'Add bounded in-memory store API',
     'description': 'Add RecordStore with put(identifier, amount), get(identifier), and list_ids(limit=100). Validate IDs/finite amounts with the parser rules, replace existing IDs deterministically, raise KeyError for missing get, sort list results, and require integer limit between 0 and 1000 (bool rejected). Add tests for replacement, missing items, ordering and invalid limits. No network or filesystem persistence.'},
)

BASE_SOURCE = '''import math

def normalize_label(text):
    raise NotImplementedError("P204.1 feature")

def divide(a, b):
    return a / b

def summarize(values):
    total = 0
    for value in values:
        total += value
    count = 0
    for value in values:
        count += 1
    return {"sum": total, "count": count, "mean": total / count if count else None}

def parse_record(text):
    identifier, amount = text.split(",")
    return identifier.strip(), float(amount)
'''
BASE_TESTS = '''from soak_app import divide, summarize, parse_record

def test_divide_normal():
    assert divide(6, 2) == 3

def test_summary_contract():
    assert summarize([2, -1, 5]) == {"sum": 6, "count": 3, "mean": 2}
    assert summarize([]) == {"sum": 0, "count": 0, "mean": None}

def test_parse_normal():
    assert parse_record("item_1,2.5") == ("item_1", 2.5)
'''


def project_config(project_id, repo, *, real_ai=True):
    return {
        'project_id': project_id, 'name': 'P20 isolated software project',
        'repo_path': str(repo), 'adapter': 'agent_files', 'worker_runtime': '.devorch/worker',
        'execution': {'enabled': True, 'owner_authorized': True, 'engine': 'aibroker',
                      'allowed_next_actions': ['continue_current_stage', 'next_task'],
                      'worker_quality': 'balanced', 'worker_timeout_seconds': 14400,
                      'worker_prompt': 'Execute exactly the current bounded software task in agent/next.md. Read AGENTS.md, CURRENT.md and result.md. Write regression tests first, implement, run pytest, record actual test command and output, update agent evidence, commit locally clean. Never push, stop a daemon, edit a runtime ledger, or advance next.md yourself. Do not pre-implement future tasks.',
                      'remediation_prompt': 'Fix only the current technical review blocking findings. Run tests, update evidence, commit clean locally. Do not push, edit runtime ledgers or advance tasks.'},
        'ai_roles': {'planner': {'enabled': True, 'quality': 'high', 'review_quality': 'high',
                                'review_independence': 'provider', 'timeout_seconds': 1800,
                                'review_timeout_seconds': 900, 'max_plan_remediation_rounds': 2,
                                'max_plan_recovery_cycles': 2, 'hard_total_review_rejects': 5},
                     'reviewer': {'enabled': True, 'quality': 'high', 'independence': 'provider',
                                  'timeout_seconds': 1800, 'max_remediation_rounds': 3}},
        'watchdog': {'enabled': True, 'auto_recovery': True,
                     'no_progress_threshold_minutes': 15, 'cooldown_minutes': 30,
                     'max_attempts_per_run': 3, 'diagnostic_timeout_seconds': 120},
        'progress_channel': {'enabled': True, 'level': 'normal'},
    }


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                            text=True, encoding='utf-8', timeout=30, **hidden_subprocess_kwargs())
    return result.stdout.strip()


def generate_soak_project(permit, template_root=None):
    """Validate before effect; refuse existing unrelated content; never reinitialize a run."""
    ok, reason = validate_soak_permit(permit, permit.canonical_repo_path,
                                     permit.canonical_runtime_root, require_marker=False)
    if not ok:
        raise ValueError('unsafe initialization: ' + str(reason))
    repo, runtime = Path(permit.disposable_repo_path), Path(permit.disposable_runtime_root)
    marker = repo / '.devorch-disposable-soak'
    manifest_path = runtime / 'soak-project-manifest.json'
    if manifest_path.exists():
        ok, reason = validate_soak_permit(permit, permit.canonical_repo_path, permit.canonical_runtime_root)
        manifest = read_json(manifest_path, None)
        if not ok or not isinstance(manifest, dict) or manifest.get('permit_fingerprint') != permit.fingerprint:
            raise ValueError('existing project identity is not proved')
        return manifest
    for path in (repo, runtime):
        if path.exists() and any(path.iterdir()):
            raise ValueError('refuse initialization over existing content: ' + str(path))
    repo.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(marker, {'run_id': permit.run_id, 'project_id': permit.soak_project_id})
    agent = repo / 'agent'
    (agent / 'staged').mkdir(parents=True)
    (repo / 'tests').mkdir()
    (repo / 'config').mkdir()
    (repo / '.gitignore').write_text('.devorch/\n__pycache__/\n.pytest_cache/\n*.pyc\n', encoding='utf-8')
    (repo / '.gitattributes').write_text('* text=auto\n*.md text eol=lf\n', encoding='utf-8', newline='\n')
    (repo / 'soak_app.py').write_text(BASE_SOURCE, encoding='utf-8')
    (repo / 'tests/test_baseline.py').write_text(BASE_TESTS, encoding='utf-8')
    (repo / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npythonpath = ["."]\ntestpaths = ["tests"]\n', encoding='utf-8')
    (repo / 'AGENTS.md').write_text('Pure software fixture only. Work on the current task only. Use tests and real commits; preserve past evidence. No push, hardware, remote calls, daemon shutdown or runtime ledger editing. DevOrchestrator owns task transitions. Future tasks stay pending design.\n', encoding='utf-8')
    for name in ('CURRENT.md', 'HANDOFF.md', 'result.md'):
        (agent / name).write_text('# Isolated P20 run\nNo task accepted yet.\n', encoding='utf-8', newline='\n')
    roadmap = []
    for index, task in enumerate(SOAK_TASK_SPECS):
        successor = SOAK_TASK_SPECS[index + 1]['task_id'] if index + 1 < len(SOAK_TASK_SPECS) else None
        text = (f"# {task['task_id']} — {task['title']}\n\nStatus: **PENDING DESIGN**\n\n"
                f"Predecessor: {SOAK_TASK_SPECS[index-1]['task_id'] if index else 'none'}\n"
                f"Successor: {successor or 'TBD / owner decision required'}\n\n"
                f"## Objective\n{task['description']}\n\n## Acceptance\n"
                'Real implementation, regression tests, actual successful pytest output, clean local commit, independent Technical Review ACCEPT. No other task or runtime mutation.\n')
        (agent / 'staged' / (task['task_id'] + '.md')).write_text(text, encoding='utf-8', newline='\n')
        if index == 0:
            (agent / 'next.md').write_text(text, encoding='utf-8', newline='\n')
        roadmap.append({'task_id': task['task_id'], 'successor': successor,
                        'successor_spec_path': f"agent/staged/{successor}.md" if successor else None})
    write_json(agent / 'staged/roadmap.json', {'schema_version': 1, 'tasks': roadmap})
    config_path = repo / 'config/projects.json'
    write_json(config_path, {'execution_accounting': {'enabled': True, 'failure_memory': True},
                            'projects': [project_config(permit.soak_project_id, repo)]})
    write_json(runtime / 'execution-jobs.json', {
        'enabled': True, 'runtime_root': str(runtime), 'projects': {
            permit.soak_project_id: {'repo_path': str(repo), 'file_roots': ['tests'], 'commands': {
                'pytest_all': {'argv': [sys.executable, '-m', 'pytest', '-q'], 'cwd': '.',
                               'effect_class': 'read_only', 'max_runtime_seconds': 300,
                               'heartbeat_interval_seconds': 5}}}}})
    # Deterministic CPF evidence remains explicitly separate from real AI/fault evidence.
    jobs = read_json(runtime / 'execution-jobs.json', {})
    jobs['projects'][permit.soak_project_id]['commands']['cpf_replay'] = {
        'argv': [sys.executable, '-m', 'dev_orchestrator.soak.cpf_runner',
            '--job-dir', '{job_dir}'], 'cwd': '.', 'effect_class': 'read_only',
        'max_runtime_seconds': 300, 'heartbeat_interval_seconds': 5}
    write_json(runtime / 'execution-jobs.json', jobs)
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.name', 'DevO isolated soak')
    _git(repo, 'config', 'user.email', 'soak@devorchestrator.local')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'test: initialize five-task pure-software soak fixture')
    from dev_orchestrator.core.successor_consistency import resolve_successor
    for index, task in enumerate(SOAK_TASK_SPECS):
        resolved = resolve_successor(repo, task['task_id'])
        expected = SOAK_TASK_SPECS[index + 1]['task_id'] if index + 1 < len(SOAK_TASK_SPECS) else None
        if (resolved.successor_task_id != expected
                or resolved.kind != ('successor' if expected else 'end_of_roadmap')):
            raise ValueError('generated native roadmap preflight refused: ' + resolved.reason)
    manifest = {'run_id': permit.run_id, 'project_id': permit.soak_project_id,
                'permit_fingerprint': permit.fingerprint, 'repo_path': str(repo),
                'runtime_root': str(runtime), 'config_path': str(config_path),
                'marker_path': str(marker), 'initial_commit': _git(repo, 'rev-parse', 'HEAD'),
                'task_ids': [t['task_id'] for t in SOAK_TASK_SPECS],
                'ports': {'bridge_port': 19765, 'daemon_port': 19770, 'broker_port': 19775},
                'generated_at': utc_now_iso()}
    write_json(manifest_path, manifest)
    return manifest
