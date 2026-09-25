import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.incidents.candidate import generate_candidate, materialize_candidate
from dev_orchestrator.incidents.evaluation import evaluate_promotion, compute_pre_review_digest
from dev_orchestrator.incidents.review import record_candidate_review, resolve_candidate_review
from dev_orchestrator.incidents.promotion import resolve_promotion_state, promote_candidate


class TestCandidatePromotion(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

        # Set up a mock regression owner git repository
        self.owner_repo = Path(self.temp_dir) / "owner_repo"
        self.owner_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(self.owner_repo)], check=True, capture_output=True)
        (self.owner_repo / "pyproject.toml").write_text('[project]\nname = "dev_orchestrator"\n', encoding="utf-8")
        src_pkg = self.owner_repo / "src" / "dev_orchestrator"
        src_pkg.mkdir(parents=True, exist_ok=True)
        (src_pkg / "__init__.py").write_text('"""DevOrchestrator package."""\n', encoding="utf-8")
        (self.owner_repo / "tests_py").mkdir(parents=True, exist_ok=True)
        (self.owner_repo / "tests_py" / "__init__.py").write_text("", encoding="utf-8")

        subprocess.run(["git", "-C", str(self.owner_repo), "config", "user.name", "Tester"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.owner_repo), "config", "user.email", "tester@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.owner_repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.owner_repo), "commit", "-m", "initial commit"], check=True, capture_output=True)
        proc = subprocess.run(["git", "-C", str(self.owner_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        self.owner_head = proc.stdout.strip()
        proc_br = subprocess.run(["git", "-C", str(self.owner_repo), "rev-parse", "--abbrev-ref", "HEAD"], check=True, capture_output=True, text=True)
        self.owner_branch = proc_br.stdout.strip()

        # Config pointing to owner repo
        self.config_path = self.runtime_root / "projects.json"
        self.config_data = {
            "projects": [
                {
                    "project_id": "dev_orchestrator",
                    "repo_path": str(self.owner_repo),
                    "regression_owner": True,
                }
            ]
        }
        self.config_path.write_text(json.dumps(self.config_data), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_generate_candidate_runtime_only(self):
        cand = generate_candidate(
            self.runtime_root,
            candidate_id="cand-123",
            invariant="progress_must_advance",
            invalid_outcome="WATCHDOG_EXECUTION_LOSS",
            generated_by="harvest_agent",
        )
        self.assertEqual(cand["candidate_id"], "cand-123")
        self.assertEqual(cand["generated_by"], "harvest_agent")
        self.assertTrue(cand["candidate_content_sha256"])

        # Must be in runtime store, NOT in owner repo
        cand_dir = self.runtime_root / "incident-packets" / "candidates" / "cand-123"
        self.assertTrue(cand_dir.is_dir())
        self.assertTrue((cand_dir / "manifest.json").exists())
        self.assertTrue((cand_dir / "test_candidate_cand-123.py").exists())
        self.assertFalse((self.owner_repo / "tests_candidate").exists())

    def test_evaluate_promotion_and_digest_stability(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-eval",
            generated_by="auto_agent",
        )

        res1 = evaluate_promotion(self.runtime_root, "cand-eval")
        digest1 = res1["pre_review_digest"]
        self.assertTrue(digest1)
        self.assertFalse(res1["promotable"])
        self.assertFalse(res1["review_gate"]["accepted"])

        # Deterministic rerun must produce identical digest
        res2 = evaluate_promotion(self.runtime_root, "cand-eval")
        digest2 = res2["pre_review_digest"]
        self.assertEqual(digest1, digest2)

    def test_pre_review_digest_changes_on_content_or_gate_failure(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-diff",
            generated_by="auto_agent",
        )
        res1 = evaluate_promotion(self.runtime_root, "cand-diff")
        orig_digest = res1["pre_review_digest"]

        # 1. Modify test content
        test_file = self.runtime_root / "incident-packets" / "candidates" / "cand-diff" / "test_candidate_cand-diff.py"
        test_file.write_text("# modified content\n", encoding="utf-8")
        res_mod = evaluate_promotion(self.runtime_root, "cand-diff")
        self.assertNotEqual(res_mod["pre_review_digest"], orig_digest)

        # 2. Gate failure
        res_fail = evaluate_promotion(
            self.runtime_root,
            "cand-diff",
            gate_overrides={"stability": {"verdict": False, "evidence_hash": "fail"}},
        )
        self.assertNotEqual(res_fail["pre_review_digest"], res_mod["pre_review_digest"])

    def test_record_review_rejects_self_review(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-self",
            generated_by="creator_alice",
        )
        evaluate_promotion(self.runtime_root, "cand-self")

        with self.assertRaises(ValueError) as ctx:
            record_candidate_review(
                self.runtime_root,
                candidate_id="cand-self",
                reviewer_id="creator_alice",  # same as generated_by!
                verdict="ACCEPTED",
            )
        self.assertIn("matches generated_by", str(ctx.exception))

    def test_independent_review_and_six_gate_resolution(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-indep",
            generated_by="creator_alice",
        )
        eval_res = evaluate_promotion(self.runtime_root, "cand-indep")
        orig_digest = eval_res["pre_review_digest"]

        # Independent review by Bob
        rev = record_candidate_review(
            self.runtime_root,
            candidate_id="cand-indep",
            reviewer_id="reviewer_bob",
            verdict="ACCEPTED",
            notes="LGTM verified invariant",
        )
        self.assertTrue(rev.get("review_id"))

        # Resolve promotion state: 6 gates passed
        promo_state = resolve_promotion_state(self.runtime_root, "cand-indep")
        self.assertTrue(promo_state["promotable"])
        self.assertTrue(promo_state["review_gate"]["accepted"])
        # pre_review_digest MUST NOT have changed
        self.assertEqual(promo_state["pre_review_digest"], orig_digest)
        self.assertTrue(promo_state["promotion_decision_digest"])

    def test_review_stale_when_candidate_modified(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-stale",
            generated_by="alice",
        )
        evaluate_promotion(self.runtime_root, "cand-stale")
        record_candidate_review(
            self.runtime_root,
            candidate_id="cand-stale",
            reviewer_id="bob",
            verdict="ACCEPTED",
        )

        # Modify content
        test_file = self.runtime_root / "incident-packets" / "candidates" / "cand-stale" / "test_candidate_cand-stale.py"
        test_file.write_text("# mutated\n", encoding="utf-8")

        rev_res = resolve_candidate_review(self.runtime_root, "cand-stale")
        self.assertTrue(rev_res["stale"])
        self.assertFalse(rev_res["accepted"])

        promo_state = resolve_promotion_state(self.runtime_root, "cand-stale")
        self.assertFalse(promo_state["promotable"])

    def test_materialize_candidate_staging_and_git_exclude(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-mat",
            generated_by="alice",
        )
        res = materialize_candidate(self.runtime_root, "cand-mat", config_path=self.config_path)
        self.assertTrue(res["materialized"])

        # Staging directory exists
        staging_dir = self.owner_repo / "tests_candidate"
        self.assertTrue(staging_dir.is_dir())
        self.assertTrue((staging_dir / "conftest.py").exists())
        self.assertTrue((staging_dir / "test_candidate_cand-mat.py").exists())

        # .git/info/exclude must contain tests_candidate/
        exclude_file = self.owner_repo / ".git" / "info" / "exclude"
        self.assertTrue(exclude_file.exists())
        exclude_content = exclude_file.read_text(encoding="utf-8")
        self.assertIn("tests_candidate/", exclude_content)

    def test_promote_candidate_destination_free(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-promo",
            generated_by="alice",
        )
        evaluate_promotion(self.runtime_root, "cand-promo")
        record_candidate_review(
            self.runtime_root,
            candidate_id="cand-promo",
            reviewer_id="bob",
            verdict="ACCEPTED",
        )

        # Operator promotion
        promo = promote_candidate(
            self.runtime_root,
            candidate_id="cand-promo",
            config_path=self.config_path,
            expected_head=self.owner_head,
            expected_branch=self.owner_branch,
        )
        self.assertTrue(promo["promoted"])
        promoted_file = Path(promo["promoted_file"])
        self.assertTrue(promoted_file.is_file())
        self.assertEqual(promoted_file.parent.name, "tests_py")

        # Verify git status: uncommitted file created, not staged or committed
        status_proc = subprocess.run(["git", "-C", str(self.owner_repo), "status", "--porcelain"], check=True, capture_output=True, text=True)
        self.assertIn(f"?? tests_py/{promoted_file.name}", status_proc.stdout)

    def test_promote_refuses_when_dirty_or_head_mismatch(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-dirty",
            generated_by="alice",
        )
        evaluate_promotion(self.runtime_root, "cand-dirty")
        record_candidate_review(
            self.runtime_root,
            candidate_id="cand-dirty",
            reviewer_id="bob",
            verdict="ACCEPTED",
        )

        # 1. Wrong expected head
        res_head = promote_candidate(
            self.runtime_root,
            candidate_id="cand-dirty",
            config_path=self.config_path,
            expected_head="0000000000000000000000000000000000000000",
        )
        self.assertFalse(res_head["promoted"])
        self.assertEqual(res_head["code"], "anchor_mismatch")

        # 2. Dirty worktree
        (self.owner_repo / "dirty.txt").write_text("dirty content", encoding="utf-8")
        res_dirty = promote_candidate(
            self.runtime_root,
            candidate_id="cand-dirty",
            config_path=self.config_path,
            expected_head=self.owner_head,
        )
        self.assertFalse(res_dirty["promoted"])
        self.assertEqual(res_dirty["code"], "repository_dirty")

    def test_vacuous_test_fails_reproduction_gate(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-vacuous",
            generated_by="alice",
        )
        # Overwrite with vacuous test
        cand_test = self.runtime_root / "incident-packets" / "candidates" / "cand-vacuous" / "test_candidate_cand-vacuous.py"
        cand_test.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_pass(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        eval_res = evaluate_promotion(self.runtime_root, "cand-vacuous")
        gates = eval_res["executable_gates"]
        self.assertFalse(gates["reproduction"]["verdict"])
        self.assertFalse(eval_res["promotable"])

    def test_promote_rollback_failed_dirty_and_blocks_subsequent(self):
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-fail-dirty",
            generated_by="alice",
        )
        evaluate_promotion(self.runtime_root, "cand-fail-dirty")
        record_candidate_review(
            self.runtime_root,
            candidate_id="cand-fail-dirty",
            reviewer_id="bob",
            verdict="ACCEPTED",
        )

        def dirty_hook(target_file):
            # Create extra dirty residue that won't be unlinked by target_path.unlink()
            dirty_residue = target_file.parent / "dirty_residue.tmp"
            dirty_residue.write_text("residue", encoding="utf-8")
            raise IOError("injected write crash after dirtying repo")

        res = promote_candidate(
            self.runtime_root,
            candidate_id="cand-fail-dirty",
            config_path=self.config_path,
            expected_head=self.owner_head,
            expected_branch=self.owner_branch,
            _post_write_hook=dirty_hook,
        )
        self.assertFalse(res["promoted"])
        self.assertEqual(res["code"], "failed_dirty")
        self.assertIn("owner_gate", res)
        self.assertTrue(res["owner_gate"]["gate_id"].startswith("owner_gate:promotion_failed_dirty:"))

        # Subsequent promotion on a clean candidate MUST be blocked!
        generate_candidate(
            self.runtime_root,
            candidate_id="cand-next-blocked",
            generated_by="charlie",
        )
        evaluate_promotion(self.runtime_root, "cand-next-blocked")
        record_candidate_review(
            self.runtime_root,
            candidate_id="cand-next-blocked",
            reviewer_id="bob",
            verdict="ACCEPTED",
        )
        res_blocked = promote_candidate(
            self.runtime_root,
            candidate_id="cand-next-blocked",
            config_path=self.config_path,
        )
        self.assertFalse(res_blocked["promoted"])
        self.assertEqual(res_blocked["code"], "promotion_blocked")

    def test_isolation_gate_forbidden_modules_and_calls(self):
        from dev_orchestrator.incidents.evaluation import _check_isolation_ast
        # Forbidden modules
        ok, reason = _check_isolation_ast("import subprocess\nsubprocess.run(['ls'])")
        self.assertFalse(ok)
        self.assertIn("forbidden_import: subprocess", reason)

        ok, reason = _check_isolation_ast("from urllib import request")
        self.assertFalse(ok)
        self.assertIn("forbidden_import_from: urllib", reason)

        ok, reason = _check_isolation_ast("import serial")
        self.assertFalse(ok)
        self.assertIn("forbidden_import: serial", reason)

        ok, reason = _check_isolation_ast("import http.client")
        self.assertFalse(ok)
        self.assertIn("forbidden_import: http.client", reason)

        # Forbidden calls
        ok, reason = _check_isolation_ast("import os\nos.system('echo hi')")
        self.assertFalse(ok)
        self.assertIn("forbidden_call: os.system", reason)

        ok, reason = _check_isolation_ast("eval('1 + 1')")
        self.assertFalse(ok)
        self.assertIn("forbidden_call: eval", reason)

        ok, reason = _check_isolation_ast("from os import system\nsystem('echo 1')")
        self.assertFalse(ok)
        self.assertIn("forbidden_import_from: os.system", reason)

    def test_auto_synthesized_candidate_passes_executable_gates(self):
        from dev_orchestrator.incidents.capture import capture_incident
        from dev_orchestrator.incidents.store import load_incident_store
        semantic = {
            "failure_class": "watchdog_stall",
            "diagnosis_code": "agent_stalled",
            "lifecycle_class": "executing",
            "contract_class": "progress",
            "invariant_identifier": "worker_must_not_stall",
        }
        family = capture_incident(
            self.runtime_root,
            project_id="test-proj",
            task_id="P16.10",
            classification="ORCHESTRATOR_ALIVE_TASK_STALLED",
            semantic=semantic,
            evidence={"log": "stall"},
            occurrence_key="test-occ-1",
            config={"incidents": {"enabled": True, "auto_synthesis": True}},
        )
        cand_id = family.get("candidate_id")
        self.assertIsNotNone(cand_id)

        # Verify candidate manifest and sha in store
        store = load_incident_store(self.runtime_root)
        cdata = store.index["candidates"][cand_id]
        cand_source_file = self.runtime_root / "incident-packets" / "candidates" / cand_id / f"test_candidate_{cand_id}.py"
        self.assertTrue(cand_source_file.is_file())
        real_sha = hashlib.sha256(cand_source_file.read_bytes()).hexdigest()
        self.assertEqual(cdata["candidate_content_sha256"], real_sha)

        # Evaluate candidate: all five executable gates must pass!
        eval_res = evaluate_promotion(self.runtime_root, cand_id)
        gates = eval_res["executable_gates"]
        self.assertTrue(gates["reproduction"]["verdict"], "Reproduction gate must pass on auto-synthesized candidate")
        self.assertTrue(gates["discrimination"]["verdict"], "Discrimination gate must pass on auto-synthesized candidate")
        self.assertTrue(gates["stability"]["verdict"], "Stability gate must pass on auto-synthesized candidate")
        self.assertTrue(gates["isolation"]["verdict"], "Isolation gate must pass on auto-synthesized candidate")
        self.assertTrue(gates["deduplication"]["verdict"], "Deduplication gate must pass on auto-synthesized candidate")
        self.assertTrue(all(g["verdict"] for g in gates.values()))
        store.reload()
        self.assertTrue(store.index["candidates"][cand_id]["executable_gates_passed"])


if __name__ == "__main__":
    unittest.main()
