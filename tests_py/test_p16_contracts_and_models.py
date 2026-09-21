import json
import sys
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.contracts import (
    DECISION_NO_PROMOTE,
    DECISION_PROMOTE,
    GATE_NAMES,
    ROLE_CLASSES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_UNAVAILABLE,
    TRACK_A,
    TRACK_B,
    BenchmarkTask,
    BrokerAttempt,
    ContainmentAuditResult,
    ContainmentConfig,
    FrozenPrompt,
    MetricCoverage,
    PromotionDecision,
    ResourceSnapshot,
    RetrievalHit,
    RunSummary,
    StalenessProbeResult,
    TrialCell,
    TrialPlan,
    TrialRecord,
    TrialScore,
    ZvecProbeResult,
    canonical_json,
    sha256_bytes,
)


class TestP16ContractsAndModels(unittest.TestCase):
    def test_role_classes_and_tracks(self):
        self.assertEqual(ROLE_CLASSES, ("planner", "reviewer", "worker", "debugger"))
        self.assertEqual(TRACK_A, "A")
        self.assertEqual(TRACK_B, "B")

    def test_benchmark_task_validation(self):
        task = BenchmarkTask(
            task_id="t1",
            role="planner",
            title="Title",
            description="Desc",
            prompt_template="Prompt",
            timeout_seconds=100.0,
            response_schema={"type": "object"},
            rubric_ref="r1",
            retrieval_sensitive=True,
            ground_truth={"key": "val"},
        )
        self.assertEqual(task.task_id, "t1")
        self.assertEqual(task.role, "planner")

        d = task.to_dict()
        task2 = BenchmarkTask.from_dict(d)
        self.assertEqual(task, task2)

        with self.assertRaises(ValueError):
            BenchmarkTask(
                task_id="t2",
                role="invalid_role",
                title="T",
                description="D",
                prompt_template="P",
                timeout_seconds=10.0,
                response_schema={},
                rubric_ref="r",
                retrieval_sensitive=False,
                ground_truth={},
            )

        with self.assertRaises(ValueError):
            BenchmarkTask(
                task_id="t3",
                role="worker",
                title="T",
                description="D",
                prompt_template="P",
                timeout_seconds=-5.0,
                response_schema={},
                rubric_ref="r",
                retrieval_sensitive=False,
                ground_truth={},
            )

    def test_frozen_prompt_serialization(self):
        text = "sample prompt content"
        pbytes = text.encode("utf-8")
        phash = sha256_bytes(pbytes)
        prompt = FrozenPrompt(task_id="t1", prompt_bytes=pbytes, prompt_hash=phash, prompt_text=text)
        d = prompt.to_dict()
        prompt2 = FrozenPrompt.from_dict(d)
        self.assertEqual(prompt.task_id, prompt2.task_id)
        self.assertEqual(prompt.prompt_hash, prompt2.prompt_hash)
        self.assertEqual(prompt.prompt_text, prompt2.prompt_text)

    def test_retrieval_hit_serialization(self):
        hit = RetrievalHit(path="src/app.py", start_line=10, end_line=20, score=0.95, mode="zvec")
        d = hit.to_dict()
        hit2 = RetrievalHit.from_dict(d)
        self.assertEqual(hit, hit2)

    def test_broker_attempt_serialization(self):
        attempt = BrokerAttempt(
            request_id="req_1",
            dispatch_id="disp_1",
            decision_id="dec_1",
            execution_id="exec_1",
            session_id="sess_1",
            resource_id="res_1",
            provider="agy",
            account="agy-1",
            model="gemini-3.8-flash-high",
            status="succeeded",
            output='{"answer": "ok"}',
            error=None,
            usage={"total_tokens": 150},
            usage_source="reported",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:05Z",
            first_output_at="2026-01-01T00:00:02Z",
            quota_observation={"bucket": "b1"},
            rate_limit_observation=None,
            failure_classification=None,
        )
        d = attempt.to_dict()
        attempt2 = BrokerAttempt.from_dict(d)
        self.assertEqual(attempt, attempt2)

    def test_trial_cell_validation_and_serialization(self):
        cell = TrialCell(
            trial_id="tr_1",
            task_id="task_1",
            role="planner",
            target_resource_id="res_1",
            track="A",
            repeat_index=0,
            pair_id="pair_1",
            pair_order=0,
            prompt_hash="abc",
            corpus_revision="rev_1",
            timeout_seconds=300.0,
        )
        d = cell.to_dict()
        cell2 = TrialCell.from_dict(d)
        self.assertEqual(cell, cell2)

        with self.assertRaises(ValueError):
            TrialCell(
                trial_id="tr_2",
                task_id="task_1",
                role="planner",
                target_resource_id="res_1",
                track="C",  # invalid track
                repeat_index=0,
                pair_id="pair_1",
                pair_order=0,
                prompt_hash="abc",
                corpus_revision="rev_1",
                timeout_seconds=300.0,
            )

    def test_trial_score_serialization(self):
        score = TrialScore(
            trial_id="tr_1",
            correctness=0.85,
            cited_spans_valid=4,
            cited_spans_invalid=1,
            required_findings_met=3,
            required_findings_total=3,
            false_findings=0,
            patch_valid=True,
            tests_passed=5,
            tests_failed=0,
            regression_rate=0.0,
            raw_score=0.85,
            details={"notes": "good"},
        )
        d = score.to_dict()
        score2 = TrialScore.from_dict(d)
        self.assertEqual(score, score2)

    def test_metric_coverage_serialization(self):
        metrics = MetricCoverage(
            observed_shell_tool_calls=2,
            zvec_calls=1,
            provider_reported_tool_calls=3,
            total_complete_tool_calls=3,
            reported_tokens=500,
            wall_time_seconds=12.5,
        )
        d = metrics.to_dict()
        metrics2 = MetricCoverage.from_dict(d)
        self.assertEqual(metrics, metrics2)

    def test_trial_record_serialization(self):
        cell = TrialCell("tr_1", "task_1", "planner", "res_1", "A", 0, "p1", 0, "hash", "rev", 100.0)
        attempt = BrokerAttempt("req_1", status="succeeded", output="{}")
        score = TrialScore("tr_1", 1.0, 2, 0, 2, 2, 0)
        metrics = MetricCoverage(1, 0, 0, None, 100, 5.0)
        record = TrialRecord("tr_1", "plan_1", cell, attempt, score, metrics, STATUS_COMPLETED, False, "2026-01-01T00:00:00Z")
        d = record.to_dict()
        record2 = TrialRecord.from_dict(d)
        self.assertEqual(record, record2)

    def test_trial_plan_serialization(self):
        cell = TrialCell("tr_1", "task_1", "planner", "res_1", "A", 0, "p1", 0, "hash", "rev", 100.0)
        plan = TrialPlan(
            plan_id="plan_1",
            created_at="2026-01-01T00:00:00Z",
            corpus_manifest_hash="man_hash",
            rubric_digest="rub_digest",
            config_hash="cfg_hash",
            registry_digest="reg_digest",
            selected_resources=("res_1", "res_2", "res_3"),
            cells=(cell,),
            repeats=1,
            seed=42,
            max_dispatches=100,
        )
        d = plan.to_dict()
        plan2 = TrialPlan.from_dict(d)
        self.assertEqual(plan, plan2)

    def test_promotion_decision_serialization(self):
        dec = PromotionDecision(
            decision=DECISION_PROMOTE,
            reasons=(),
            failed_gates=(),
            gate_results={"capability_gate": {"passed": True}},
            run_id="run_1",
            plan_id="plan_1",
            timestamp="2026-01-01T00:00:00Z",
        )
        d = dec.to_dict()
        dec2 = PromotionDecision.from_dict(d)
        self.assertEqual(dec, dec2)

        with self.assertRaises(ValueError):
            PromotionDecision("INVALID", (), (), {}, "r", "p", "t")

    def test_containment_config_and_audit_serialization(self):
        cfg = ContainmentConfig(
            scratch_root="C:\\scratch",
            queue_root="C:\\queue",
            dedicated_sid="S-1-5-21-12345",
            scheduled_task_name="WorkerTask",
            broker_config_path="C:\\broker.yaml",
            zvec_path="C:\\zvec.exe",
            firewall_rule_name="AIBenchDeny",
            protected_roots=("C:\\repo1", "C:\\repo2"),
        )
        d = cfg.to_dict()
        cfg2 = ContainmentConfig.from_dict(d)
        self.assertEqual(cfg, cfg2)

        audit = ContainmentAuditResult(
            sid_match=True,
            deny_acl_verified=True,
            scratch_verified=True,
            registry_verified=True,
            passed=True,
            details={"check": "ok"},
        )
        d_aud = audit.to_dict()
        audit2 = ContainmentAuditResult.from_dict(d_aud)
        self.assertEqual(audit, audit2)


if __name__ == "__main__":
    unittest.main()
