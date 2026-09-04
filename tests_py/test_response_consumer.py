import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.decision import DecisionDisposition
from dev_orchestrator.core.dispatcher import dispatch_worker_done_events
from dev_orchestrator.core.response_consumer import consume_websol_responses
from dev_orchestrator.core.websol import NextAction
from tests_py.test_worker_done_dispatcher import git, make_repo, snap


def response_text(claim, *, decision="next", next_action="next_task", changes=None, extra_text=""):
    payload = {
        "project_id": claim.project_id,
        "request_id": claim.request_id,
        "task_id": claim.task_id,
        "stage_id": claim.stage_id,
        "branch": claim.branch,
        "head": claim.head,
        "role": claim.role,
        "event": claim.event,
        "nonce": claim.nonce,
        "decision": decision,
        "next_action": next_action,
    }
    payload.update(changes or {})
    marker = "[DEVORCH_WEB_SOL_RESPONSE {0}]".format(claim.request_id)
    return marker + "\n" + json.dumps(payload, separators=(",", ":")) + extra_text


def setup_case(base: Path, project_id="alpha", binding_id="conv-A", run_id="run-1"):
    repo = base / project_id
    make_repo(repo)
    runtime = base / "runtime"
    store = BrowserBridgeStore(runtime / "bridge")
    summary = {"projects": [snap(project_id, repo, binding_id, run_id)]}
    dispatched = dispatch_worker_done_events(summary, store, runtime)
    assert len(dispatched) == 1
    claim = store.claim("chatgpt_web", binding_id)
    assert claim is not None
    return repo, runtime, store, summary, claim


def respond(store, claim, text):
    return store.respond(
        claim.adapter, claim.binding_id, claim.request_id,
        claim.nonce, claim.claim_token, text,
    )


class ResponseConsumerTests(unittest.TestCase):
    def test_valid_response_is_applied_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            respond(store, claim, response_text(claim))

            first = consume_websol_responses(summary, store, runtime)
            second = consume_websol_responses(summary, store, runtime)

            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].project_id, "alpha")
            self.assertEqual(first[0].request_id, claim.request_id)
            self.assertEqual(first[0].disposition, DecisionDisposition.APPLY)
            self.assertEqual(first[0].next_action, NextAction.NEXT_TASK)
            self.assertEqual(second, [])

            decisions = json.loads((runtime / "websol-decisions.json").read_text(encoding="utf-8"))
            record = decisions["decisions"][claim.request_id]
            self.assertEqual(record["disposition"], "apply")
            self.assertEqual(record["next_action"], "next_task")
            self.assertNotIn("worker_pid", record)
            self.assertNotIn("executed", record)

    def test_duplicate_response_replay_produces_only_one_disposition(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            raw = response_text(claim)
            respond(store, claim, raw)
            respond(store, claim, raw)

            first = consume_websol_responses(summary, store, runtime)
            second = consume_websol_responses(
                summary, BrowserBridgeStore(runtime / "bridge"), runtime
            )

            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].request_id, claim.request_id)
            self.assertEqual(second, [])
            decisions = json.loads(
                (runtime / "websol-decisions.json").read_text(encoding="utf-8")
            )
            self.assertEqual(list(decisions["decisions"]), [claim.request_id])

    def test_malformed_or_multiple_json_objects_stop_fail_closed(self):
        cases = ["not-json", "{}\n{}"]
        for index, suffix in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as td:
                repo, runtime, store, summary, claim = setup_case(Path(td), run_id="run-{0}".format(index))
                raw = "[DEVORCH_WEB_SOL_RESPONSE {0}]\n{1}".format(claim.request_id, suffix)
                respond(store, claim, raw)
                results = consume_websol_responses(summary, store, runtime)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].disposition, DecisionDisposition.STOP)
                self.assertIsNone(results[0].next_action)

    def test_identity_mismatch_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            respond(store, claim, response_text(claim, changes={"project_id": "other"}))
            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(result[0].disposition, DecisionDisposition.IGNORE)
            self.assertIsNone(result[0].next_action)

    def test_fresh_head_change_makes_response_stale(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            respond(store, claim, response_text(claim))
            (repo / "seed.txt").write_text("changed", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "new head")
            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(result[0].disposition, DecisionDisposition.STALE)

    def test_dirty_workspace_requires_review(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            respond(store, claim, response_text(claim))
            (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(result[0].disposition, DecisionDisposition.REVIEW_REQUIRED)
            self.assertIsNone(result[0].next_action)

    def test_owner_gate_is_preserved_without_execution(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            raw = response_text(claim, decision="owner_gate", next_action="stop")
            respond(store, claim, raw)
            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(result[0].disposition, DecisionDisposition.OWNER_GATE)
            self.assertEqual(result[0].next_action, NextAction.STOP)

    def test_invalid_decision_action_or_extra_field_stops(self):
        cases = [
            response_text,
        ]
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td))
            raw = response_text(claim, decision="remediate", next_action="next_stage")
            respond(store, claim, raw)
            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(result[0].disposition, DecisionDisposition.STOP)

        with tempfile.TemporaryDirectory() as td:
            repo, runtime, store, summary, claim = setup_case(Path(td), run_id="run-extra")
            raw = response_text(claim, changes={"unexpected": "field"})
            respond(store, claim, raw)
            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(result[0].disposition, DecisionDisposition.STOP)

    def test_two_projects_consume_only_the_response_that_exists(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); runtime = base / "runtime"
            repo_a, repo_b = base / "a", base / "b"
            make_repo(repo_a); make_repo(repo_b)
            store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [
                snap("alpha", repo_a, "conv-A", "run-a"),
                snap("beta", repo_b, "conv-B", "run-b"),
            ]}
            self.assertEqual(len(dispatch_worker_done_events(summary, store, runtime)), 2)
            claim_a = store.claim("chatgpt_web", "conv-A")
            respond(store, claim_a, response_text(claim_a))

            result = consume_websol_responses(summary, store, runtime)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].project_id, "alpha")
            self.assertIsNotNone(store.claim("chatgpt_web", "conv-B"))


if __name__ == "__main__":
    unittest.main()
