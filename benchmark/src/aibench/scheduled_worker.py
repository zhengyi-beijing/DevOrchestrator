"""Scheduled Task Worker for contained aibench execution."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .broker_client import BrokerBenchmarkClient
from .containment import audit_containment, get_current_user_sid
from .contracts import (
    ContainmentConfig,
    TrialPlan,
    sha256_bytes,
    utc_now_iso,
)
from .runner import BenchmarkRunner
from .zvec import ZvecAdapter


def load_containment_config(config_path: Path | None = None) -> ContainmentConfig:
    default_path = Path("runtime/benchmark.json")
    p = config_path if config_path else default_path
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        return ContainmentConfig.from_dict(data)

    # Built-in defaults outside repositories
    return ContainmentConfig(
        scratch_root=r"C:\work\aibench_scratch",
        queue_root=r"C:\work\aibench_queue",
        # Current-user live benchmark is the default. A dedicated SID /
        # scheduled-task identity is an optional hardening mode, not an
        # acceptance requirement.
        dedicated_sid="",
        scheduled_task_name="",
        broker_config_path=r"runtime\aibroker-m1\resources-m2.yaml",
        zvec_path="",
        firewall_rule_name="DevOrchestrator-AIBench-OutboundDeny",
        protected_roots=(
            r"C:\work\github\DevOrchestrator-dev",
            r"C:\work\github\AIResourceBroker",
        ),
    )


def run_worker_loop(
    queue_dir: Path,
    config: ContainmentConfig,
    run_once: bool = True,
    allow_dev_roots: bool = True,
) -> int:
    """Execute queue requests under containment boundaries."""
    username, sid = get_current_user_sid()

    # Preflight containment audit
    audit = audit_containment(config, allow_mock_sid=True, allow_unrestricted_dev_roots=allow_dev_roots)
    if not audit.passed:
        sys.stderr.write(f"Containment audit failed: {audit.details}\n")
        return 2

    queue_dir.mkdir(parents=True, exist_ok=True)
    start_time = time.monotonic()

    while True:
        request_files = list(queue_dir.glob("request-*.json"))
        if not request_files:
            if run_once:
                break
            time.sleep(1.0)
            if time.monotonic() - start_time > 60.0:
                break
            continue

        for req_file in request_files:
            nonce = req_file.name.replace("request-", "").replace(".json", "")
            try:
                req_data = json.loads(req_file.read_text(encoding="utf-8"))
                plan_path = Path(req_data["plan_path"])
                expected_hash = req_data["plan_hash"]

                plan_bytes = plan_path.read_bytes()
                actual_hash = sha256_bytes(plan_bytes)
                if actual_hash != expected_hash:
                    raise ValueError(f"plan hash mismatch: expected {expected_hash} got {actual_hash}")

                plan = TrialPlan.from_dict(json.loads(plan_bytes.decode("utf-8")))

                # Initialize components
                zvec = ZvecAdapter(config.zvec_path, config.firewall_rule_name)
                # For broker client, import AIBrokerExecutionPort
                from dev_orchestrator.ai.aibroker_subprocess import AIBrokerClientConfig, AIBrokerExecutionPort
                b_cfg = AIBrokerClientConfig(
                    python_executable=Path(sys.executable),
                    broker_repo=Path(config.protected_roots[1]) if len(config.protected_roots) > 1 else Path("."),
                    config_path=Path(config.broker_config_path),
                )
                port = AIBrokerExecutionPort(b_cfg)
                b_client = BrokerBenchmarkClient(port, config.broker_config_path)
                snapshot = b_client.snapshot_resources()

                results_file = Path(config.scratch_root) / f"results_{plan.plan_id}.jsonl"
                runner = BenchmarkRunner(b_client, zvec, Path(config.scratch_root), results_file)

                # Execute plan
                records = runner.run_plan(plan, Path("benchmark/corpus"), snapshot)

                # Write result file
                result_data = {
                    "nonce": nonce,
                    "worker_user": username,
                    "worker_sid": sid,
                    "plan_id": plan.plan_id,
                    "records_count": len(records),
                    "exit_classification": "completed",
                    "finished_at": utc_now_iso(),
                }
                result_file = queue_dir / f"result-{nonce}.json"
                result_file.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
                req_file.unlink(missing_ok=True)
            except Exception as exc:
                result_data = {
                    "nonce": nonce,
                    "worker_user": username,
                    "worker_sid": sid,
                    "exit_classification": "failed",
                    "error": str(exc),
                    "finished_at": utc_now_iso(),
                }
                result_file = queue_dir / f"result-{nonce}.json"
                result_file.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
                req_file.unlink(missing_ok=True)

        if run_once:
            break

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AIBench Scheduled Task Worker")
    parser.add_argument("--queue-dir", default=r"C:\work\aibench_queue")
    parser.add_argument("--config", default=None)
    parser.add_argument("--once", action="store_true", default=True)
    args = parser.parse_args()

    cfg = load_containment_config(Path(args.config) if args.config else None)
    exit_code = run_worker_loop(Path(args.queue_dir), cfg, run_once=args.once)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
