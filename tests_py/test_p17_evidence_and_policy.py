"""P17 Evidence and Policy model tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.evidence import (
    ConflictClaim,
    EvidenceItem,
    EvidenceSnapshot,
    SharedCredentialLease,
    get_required_source,
    has_unresolved_ambiguity,
    snapshot_digest,
)
from dev_orchestrator.convergence.policy import ProblemBudget, build_policy


class TestP17EvidenceAndPolicy(unittest.TestCase):
    def test_evidence_snapshot_and_precedence(self) -> None:
        item_ok = EvidenceItem(
            source="lifecycle_authority",
            source_id="item-1",
            timestamp="2026-09-27T00:00:00Z",
            anchor="head-1",
            data={"state": "READY_TO_RUN"},
            digest="sha256:d1",
            read_status="OK",
        )
        item_stale = EvidenceItem(
            source="lifecycle_authority",
            source_id="item-2",
            timestamp="2026-09-26T00:00:00Z",
            anchor="head-0",
            data={"state": "OLD"},
            digest="sha256:d2",
            read_status="AMBIGUOUS",
        )
        snap = EvidenceSnapshot(items=(item_ok, item_stale))
        best = get_required_source(snap, "lifecycle_authority")
        self.assertIsNotNone(best)
        self.assertEqual(best.source_id, "item-1")
        self.assertEqual(best.read_status, "OK")

    def test_unresolved_ambiguity_detection(self) -> None:
        conflict = ConflictClaim(
            source_a="repo_a",
            source_b="repo_b",
            conflict_type="split_brain",
            details="two different branches claim main",
            resolved=False,
        )
        snap_conflicted = EvidenceSnapshot(conflicts=(conflict,))
        self.assertTrue(has_unresolved_ambiguity(snap_conflicted))

        snap_clean = EvidenceSnapshot()
        self.assertFalse(has_unresolved_ambiguity(snap_clean))

    def test_shared_credential_lease(self) -> None:
        lease = SharedCredentialLease(
            session_id="sess-1",
            credential_id="cred-chatgpt",
            owner_role="worker",
            lease_until="2026-09-27T01:00:00Z",
            acquired_at="2026-09-27T00:00:00Z",
        )
        snap = EvidenceSnapshot(shared_leases=(lease,))
        self.assertEqual(len(snap.shared_leases), 1)
        self.assertEqual(snap.shared_leases[0].credential_id, "cred-chatgpt")

    def test_policy_digest_and_immutability(self) -> None:
        policy1 = build_policy(
            default_budget=ProblemBudget(max_total_attempts=7),
            max_output_invalid_repairs=3,
        )
        policy2 = build_policy(
            default_budget=ProblemBudget(max_total_attempts=7),
            max_output_invalid_repairs=3,
        )
        self.assertTrue(bool(policy1.policy_digest))
        self.assertEqual(policy1.policy_digest, policy2.policy_digest)

        # Different policy has different digest
        policy3 = build_policy(
            default_budget=ProblemBudget(max_total_attempts=10),
            max_output_invalid_repairs=3,
        )
        self.assertNotEqual(policy1.policy_digest, policy3.policy_digest)
