"""Command Line Interface for aibench operations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .broker_client import BrokerBenchmarkClient
from .containment import audit_containment, submit_queue_request
from .contracts import (
    ContainmentConfig,
    DEFAULT_PROMOTION_THRESHOLDS,
    RunSummary,
    TrialPlan,
    TrialRecord,
    ZvecProbeResult,
    canonical_json,
    sha256_bytes,
)
from .corpus import generate_synthetic_corpus, verify_corpus_manifest
from .decision import evaluate_promotion_decision
from .prompts import get_canonical_tasks
from .report import build_run_summary, generate_report_markdown
from .runner import BenchmarkRunner, build_trial_plan
from .scheduled_worker import load_containment_config
from .staleness import run_staleness_probe
from .zvec import ZvecAdapter


def cmd_corpus_verify(args: argparse.Namespace) -> int:
    corpus_p = Path(args.corpus_path)
    if not corpus_p.exists():
        print(f"Generating synthetic corpus at {corpus_p}...")
        generate_synthetic_corpus(corpus_p)

    ok, errors = verify_corpus_manifest(corpus_p)
    if ok:
        manifest = json.loads((corpus_p / "manifest.json").read_text(encoding="utf-8"))
        print(f"Corpus verification PASSED. Manifest hash: {manifest.get('corpus_manifest_hash')}")
        return 0
    else:
        print(f"Corpus verification FAILED:\n" + "\n".join(errors), file=sys.stderr)
        return 1


def cmd_containment_audit(args: argparse.Namespace) -> int:
    cfg = load_containment_config(Path(args.config) if args.config else None)
    result = audit_containment(
        cfg,
        allow_mock_sid=args.allow_mock_sid,
        allow_unrestricted_dev_roots=args.allow_dev_roots,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.passed else 1


def cmd_zvec_probe(args: argparse.Namespace) -> int:
    adapter = ZvecAdapter(executable_path=args.zvec_path)
    res = adapter.probe(force_refresh=True)
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.supported else 1


def cmd_plan_freeze(args: argparse.Namespace) -> int:
    corpus_p = Path(args.corpus_path)
    if not corpus_p.exists():
        generate_synthetic_corpus(corpus_p)

    manifest = json.loads((corpus_p / "manifest.json").read_text(encoding="utf-8"))
    manifest_hash = manifest.get("corpus_manifest_hash", "")

    resources = [r.strip() for r in args.resources.split(",") if r.strip()]
    if len(resources) < 3:
        # Default representative set
        resources = [
            "agy/agy-1/gemini-3.8-flash-high",
            "copilot/default/claude-sonnet-4.6",
            "claude/default/opus",
        ]

    plan = build_trial_plan(
        selected_resources=resources,
        corpus_manifest_hash=manifest_hash,
        repeats=args.repeats,
        seed=args.seed,
        execution_source=getattr(args, "execution_source", "live"),
    )

    out_p = Path(args.output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
    print(f"TrialPlan frozen at {out_p}. Plan ID: {plan.plan_id}, Total Cells: {len(plan.cells)}")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    plan_p = Path(args.plan)
    if not plan_p.is_file():
        print(f"Error: plan file {plan_p} not found", file=sys.stderr)
        return 1
    plan_bytes = plan_p.read_bytes()
    plan_hash = sha256_bytes(plan_bytes)
    queue_p = Path(args.queue)
    nonce = submit_queue_request(queue_p, plan_p, plan_hash)
    print(f"Submitted trial plan {plan_p} to queue {queue_p}. Nonce: {nonce}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    plan_p = Path(args.plan)
    if not plan_p.is_file():
        print(f"Error: plan file {plan_p} not found", file=sys.stderr)
        return 1
    plan = TrialPlan.from_dict(json.loads(plan_p.read_text(encoding="utf-8")))

    corpus_p = Path(args.corpus_path)
    if not corpus_p.exists():
        generate_synthetic_corpus(corpus_p)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / f"results_{plan.plan_id}.jsonl"

    zvec = ZvecAdapter(executable_path=args.zvec_path)

    # Initialize execution port
    if args.mock_broker:
        from .broker_client import MockExecutionPort
        port = MockExecutionPort()
        b_client = BrokerBenchmarkClient(port, config_path=args.resources_config)
    else:
        from dev_orchestrator.ai.aibroker_subprocess import AIBrokerClientConfig, AIBrokerExecutionPort
        b_cfg = AIBrokerClientConfig(
            python_executable=Path(sys.executable),
            broker_repo=Path("."),
            config_path=Path(args.resources_config or r"runtime\aibroker-m1\resources-m2.yaml"),
            service_url=args.service_url or "http://127.0.0.1:8875",
        )
        port = AIBrokerExecutionPort(b_cfg)
        b_client = BrokerBenchmarkClient(port, config_path=args.resources_config, service_url=b_cfg.service_url)

    execution_source = getattr(args, "execution_source", None)
    if not execution_source:
        execution_source = "pipeline_self_test" if args.mock_broker else getattr(plan, "execution_source", "live")

    snapshot = b_client.snapshot_resources()
    scratch_root = out_dir / "scratch"
    runner = BenchmarkRunner(
        b_client,
        zvec,
        scratch_root,
        results_file,
        execution_source=execution_source,
    )

    print(f"Running benchmark plan {plan.plan_id} ({len(plan.cells)} cells)...")
    records = runner.run_plan(plan, corpus_p, snapshot)
    print(f"Benchmark completed: {len(records)} records written to {results_file}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    res_p = Path(args.results)
    if not res_p.is_file():
        print(f"Error: results file {res_p} not found", file=sys.stderr)
        return 1
    records: list[TrialRecord] = []
    with open(res_p, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(TrialRecord.from_dict(json.loads(line.strip())))

    plan_p = Path(args.plan)
    plan = TrialPlan.from_dict(json.loads(plan_p.read_text(encoding="utf-8")))

    summary = build_run_summary(records, plan)
    out_p = Path(args.output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    print(f"Summary written to {out_p}")

    if args.markdown:
        md_p = Path(args.markdown)
        md_text = generate_report_markdown(summary)
        md_p.write_text(md_text, encoding="utf-8")
        print(f"Markdown report written to {md_p}")

    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    sum_p = Path(args.summary)
    if not sum_p.is_file():
        print(f"Error: summary file {sum_p} not found", file=sys.stderr)
        return 1
    summary = RunSummary.from_dict(json.loads(sum_p.read_text(encoding="utf-8")))

    plan_p = Path(args.plan)
    plan = TrialPlan.from_dict(json.loads(plan_p.read_text(encoding="utf-8")))

    probe = ZvecAdapter(executable_path=args.zvec_path).probe()
    decision = evaluate_promotion_decision(summary, probe, plan)

    out_p = Path(args.output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(decision.to_dict(), indent=2), encoding="utf-8")
    print(f"Promotion decision written to {out_p}: {decision.decision}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="aibench", description="DevOrchestrator AI Capability Benchmark")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # corpus-verify
    p_cv = subparsers.add_parser("corpus-verify")
    p_cv.add_argument("--corpus-path", default="benchmark/corpus")

    # containment-audit
    p_ca = subparsers.add_parser("containment-audit")
    p_ca.add_argument("--config", default=None)
    p_ca.add_argument("--allow-mock-sid", action="store_true", default=False)
    p_ca.add_argument("--allow-dev-roots", action="store_true", default=False)

    # zvec-probe
    p_zp = subparsers.add_parser("zvec-probe")
    p_zp.add_argument("--zvec-path", default=None)

    # plan-freeze
    p_pf = subparsers.add_parser("plan-freeze")
    p_pf.add_argument("--output", default="benchmark/trial_plan.json")
    p_pf.add_argument("--repeats", type=int, default=1)
    p_pf.add_argument("--seed", type=int, default=42)
    p_pf.add_argument("--resources", default="agy/agy-1/gemini-3.8-flash-high,copilot/default/claude-sonnet-4.6,claude/default/opus")
    p_pf.add_argument("--corpus-path", default="benchmark/corpus")
    p_pf.add_argument("--execution-source", default="live")

    # submit
    p_sub = subparsers.add_parser("submit")
    p_sub.add_argument("--plan", required=True)
    p_sub.add_argument("--queue", default=r"C:\work\aibench_queue")

    # run
    p_run = subparsers.add_parser("run")
    p_run.add_argument("--plan", required=True)
    p_run.add_argument("--output-dir", default="benchmark/output")
    p_run.add_argument("--corpus-path", default="benchmark/corpus")
    p_run.add_argument("--zvec-path", default=None)
    p_run.add_argument("--resources-config", default=r"runtime\aibroker-m1\resources-m2.yaml")
    p_run.add_argument("--service-url", default="http://127.0.0.1:8875")
    p_run.add_argument("--mock-broker", action="store_true", default=False)
    p_run.add_argument("--execution-source", default=None)

    # report
    p_rep = subparsers.add_parser("report")
    p_rep.add_argument("--results", required=True)
    p_rep.add_argument("--plan", required=True)
    p_rep.add_argument("--output", default="benchmark/summary.json")
    p_rep.add_argument("--markdown", default="benchmark/report.md")

    # decide
    p_dec = subparsers.add_parser("decide")
    p_dec.add_argument("--summary", required=True)
    p_dec.add_argument("--plan", required=True)
    p_dec.add_argument("--zvec-path", default=None)
    p_dec.add_argument("--output", default="benchmark/promotion_decision.json")

    args = parser.parse_args()

    handlers = {
        "corpus-verify": cmd_corpus_verify,
        "containment-audit": cmd_containment_audit,
        "zvec-probe": cmd_zvec_probe,
        "plan-freeze": cmd_plan_freeze,
        "submit": cmd_submit,
        "run": cmd_run,
        "report": cmd_report,
        "decide": cmd_decide,
    }
    sys.exit(handlers[args.subcommand](args))


if __name__ == "__main__":
    main()
