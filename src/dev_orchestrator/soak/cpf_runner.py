"""Fixed-purpose CPF job: corpus reads use the canonical test working directory."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys

from dev_orchestrator.config import REPO_ROOT
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.platform.process import hidden_subprocess_kwargs

CPF_TEST_PATHS = ('tests_py/test_p17_cpf_scenarios_part1.py',
                  'tests_py/test_p17_cpf_scenarios_part2.py')


def run_cpf_job(job_dir):
    directory = Path(job_dir).resolve()
    if directory.parent.name != 'jobs':
        raise ValueError('CPF output must be in an owned job directory')
    store = ExecutionJobStore(directory.parent.parent)
    job = store.get(directory.name)
    payload = store.get_input_artifact(directory.name)
    if (job is None or job.command_ref != 'cpf_replay' or job.stage_run_id != 'CPF-verification'
            or not isinstance(payload, dict) or not payload.get('source_revision')
            or set(payload.get('file_hashes', {})) != set(CPF_TEST_PATHS)):
        raise ValueError('CPF job identity or input missing')
    for path in CPF_TEST_PATHS:
        if hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() != payload['file_hashes'][path]:
            raise ValueError('CPF test source changed: ' + path)
    return subprocess.run([sys.executable, '-m', 'pytest', '-q',
        *CPF_TEST_PATHS, '--junitxml=' + str(directory / 'cpf.xml')],
        cwd=REPO_ROOT, **hidden_subprocess_kwargs()).returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', required=True)
    args = parser.parse_args()
    raise SystemExit(run_cpf_job(args.job_dir))
