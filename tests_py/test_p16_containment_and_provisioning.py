import json
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.containment import (
    audit_containment,
    get_current_user_sid,
    poll_queue_result,
    submit_queue_request,
)
from aibench.contracts import ContainmentConfig
from aibench.provisioning import ContainmentProvisioner


class TestP16ContainmentAndProvisioning(unittest.TestCase):
    def test_current_user_sid_detection(self):
        user, sid = get_current_user_sid()
        self.assertTrue(bool(user))
        self.assertTrue(bool(sid))

    def test_containment_audit_disjoint_and_canary(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            scratch = base / "scratch"
            queue = base / "queue"
            prot1 = base / "protected_repo"
            prot1.mkdir()
            reg = base / "resources.yaml"
            reg.write_text("resources: []", encoding="utf-8")

            cfg = ContainmentConfig(
                scratch_root=str(scratch),
                queue_root=str(queue),
                dedicated_sid="",
                scheduled_task_name="WorkerTask",
                broker_config_path=str(reg),
                zvec_path="",
                firewall_rule_name="TestRule",
                protected_roots=(str(prot1),),
            )

            res = audit_containment(cfg, allow_mock_sid=True, allow_unrestricted_dev_roots=True)
            self.assertTrue(res.passed)
            self.assertTrue(res.scratch_verified)
            self.assertTrue(res.registry_verified)

    def test_containment_audit_rejects_nested_scratch(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            prot = base / "protected_repo"
            prot.mkdir()
            # Scratch placed INSIDE protected repo
            scratch = prot / "scratch_inside_protected"
            queue = base / "queue"
            reg = base / "resources.yaml"
            reg.write_text("resources: []", encoding="utf-8")

            cfg = ContainmentConfig(
                scratch_root=str(scratch),
                queue_root=str(queue),
                dedicated_sid="",
                scheduled_task_name="WorkerTask",
                broker_config_path=str(reg),
                zvec_path="",
                firewall_rule_name="TestRule",
                protected_roots=(str(prot),),
            )

            res = audit_containment(cfg, allow_mock_sid=True, allow_unrestricted_dev_roots=True)
            self.assertFalse(res.passed)
            self.assertIn("is inside protected root", str(res.details.get("isolation_errors", [])))

    def test_queue_request_and_poll_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            queue_dir = Path(td) / "queue"
            plan_file = Path(td) / "plan.json"
            plan_file.write_text('{"plan_id": "p1"}', encoding="utf-8")

            nonce = submit_queue_request(queue_dir, plan_file, "fake_hash")
            req_file = queue_dir / f"request-{nonce}.json"
            self.assertTrue(req_file.is_file())

            # Simulate worker response
            res_file = queue_dir / f"result-{nonce}.json"
            res_file.write_text(json.dumps({"nonce": nonce, "exit_classification": "completed"}), encoding="utf-8")

            polled = poll_queue_result(queue_dir, nonce, timeout_seconds=5.0)
            self.assertEqual(polled["nonce"], nonce)
            self.assertEqual(polled["exit_classification"], "completed")

    def test_provisioner_journal_and_uninstall_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "containment_state"
            provisioner = ContainmentProvisioner(state_dir=state_dir)

            cfg = ContainmentConfig(
                scratch_root=str(Path(td) / "scratch"),
                queue_root=str(Path(td) / "queue"),
                dedicated_sid="S-1-5-21-9999",
                scheduled_task_name="WorkerTask",
                broker_config_path="",
                zvec_path="",
                firewall_rule_name="TestRule",
                protected_roots=(str(Path(td)),),
            )

            # Dry run install writes journal and state file
            state = provisioner.install(cfg, dry_run=True)
            self.assertTrue("state_digest" in state)

            # Manually write tampered state file
            tampered_state = dict(state)
            tampered_state["state_digest"] = "tampered_digest"
            (state_dir / "installed_state.json").write_text(json.dumps(tampered_state), encoding="utf-8")

            # Uninstall must reject tampered state
            with self.assertRaises(RuntimeError) as ctx:
                provisioner.uninstall(dry_run=True)
            self.assertIn("digest mismatch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
