import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.incidents.fingerprint import incident_fingerprint
from dev_orchestrator.incidents.capture import capture_incident
from dev_orchestrator.incidents.store import (
    IncidentStore,
    load_incident_store,
    begin_txn,
    commit_txn,
    reconcile_journal,
)


class TestIncidentStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fingerprint_allowlist_and_volatile_rejection(self):
        valid_semantic = {
            "failure_class": "WATCHDOG_EXECUTION_LOSS",
            "diagnosis_code": "worker_hung_no_lineage",
            "lifecycle_class": "EXECUTING",
            "role_state_class": "DEAD",
        }
        fp1 = incident_fingerprint(valid_semantic)
        fp2 = incident_fingerprint(valid_semantic)
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 32)

        # Volatile or unallowlisted keys must be rejected
        for bad_key in ["timestamp", "pid", "uuid", "created_at", "arbitrary_random_field"]:
            bad_semantic = dict(valid_semantic)
            bad_semantic[bad_key] = "value"
            with self.assertRaises(ValueError):
                incident_fingerprint(bad_semantic)

    def test_atomic_commit_and_index_revision(self):
        store = load_incident_store(self.runtime_root)
        self.assertEqual(store.revision, 0)
        self.assertIsNone(store.last_txn_id)

        intent = begin_txn(
            store,
            operation="test_op",
            side_files={"extra.txt": b"hello world"},
            target_index_updater=lambda idx: idx.setdefault("families", {})
        )
        self.assertTrue(intent.intent_path.exists())

        res = commit_txn(store, intent)
        self.assertTrue(res)
        self.assertEqual(store.revision, 1)
        self.assertFalse(intent.intent_path.exists())
        self.assertTrue((store.base_dir / "extra.txt").exists())

    def test_reconciliation_committed_confirmed(self):
        store = load_incident_store(self.runtime_root)
        intent = begin_txn(
            store,
            operation="crash_after_commit",
            side_files={"data.bin": b"12345"},
            target_index_updater=lambda idx: idx.update({"marked": True})
        )
        # Simulate crash after index write: index was written with new revision and last_txn_id,
        # but intent file was not yet unlinked.
        target_idx = dict(store.index)
        target_idx["marked"] = True
        target_idx["revision"] = intent.target_revision
        target_idx["last_txn_id"] = intent.txn_id
        (store.base_dir / "index.json").write_text(json.dumps(target_idx), encoding="utf-8")
        store.index = target_idx

        self.assertTrue(intent.intent_path.exists())
        results = reconcile_journal(store)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "committed_confirmed")
        self.assertFalse(intent.intent_path.exists())

    def test_reconciliation_reapplied(self):
        store = load_incident_store(self.runtime_root)
        intent = begin_txn(
            store,
            operation="crash_before_commit",
            side_files={"file_reapply.txt": b"reapply_content"},
            target_index_updater=lambda idx: idx.update({"reapplied": True})
        )
        # Simulate crash before index write: intent exists, target side files exist, but index is still at prior revision
        self.assertTrue(intent.intent_path.exists())
        results = reconcile_journal(store)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "reapplied")
        self.assertFalse(intent.intent_path.exists())
        self.assertEqual(store.revision, 1)
        self.assertTrue(store.index.get("reapplied"))
        self.assertTrue((store.base_dir / "file_reapply.txt").exists())

    def test_reconciliation_superseded_txn(self):
        store = load_incident_store(self.runtime_root)
        # Commit a legit transaction first
        intent1 = begin_txn(
            store,
            operation="legit_first",
            side_files={"f1.txt": b"one"},
            target_index_updater=lambda idx: idx.update({"step": 1})
        )
        commit_txn(store, intent1)
        self.assertEqual(store.revision, 1)

        # Now simulate an older intent from prior_revision=0 that was interrupted
        old_intent_file = store.intents_dir / "txn-old.json"
        orphan_side = store.base_dir / "orphaned_side.txt"
        orphan_side.write_bytes(b"stale_side")
        old_intent_data = {
            "schema_version": 1,
            "txn_id": "txn-old",
            "operation": "stale_op",
            "created_at": "2026-09-25T00:00:00Z",
            "prior_revision": 0,
            "target_revision": 1,
            "payload_hashes": {"orphaned_side.txt": hashlib.sha256(b"stale_side").hexdigest()},
            "side_files": ["orphaned_side.txt"],
            "target_index": {"schema_version": 1, "revision": 1, "last_txn_id": "txn-old", "step": 0},
            "intent_path": str(old_intent_file),
        }
        old_intent_file.write_text(json.dumps(old_intent_data), encoding="utf-8")

        results = reconcile_journal(store)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "superseded_txn")
        # Store index should stay at newer revision 1 with step=1
        self.assertEqual(store.revision, 1)
        self.assertEqual(store.index.get("step"), 1)
        # Side file should have been moved to orphans
        self.assertFalse(orphan_side.exists())
        self.assertTrue(any(f.name.endswith("orphaned_side.txt") for f in store.orphans_dir.rglob("*")))

    def test_reconciliation_payload_unverified(self):
        store = load_incident_store(self.runtime_root)
        intent = begin_txn(
            store,
            operation="payload_tamper",
            side_files={"tampered.txt": b"initial_content"},
            target_index_updater=lambda idx: idx.update({"tampered": True})
        )
        # Tamper the side file
        (store.base_dir / "tampered.txt").write_bytes(b"tampered_different_bytes")

        results = reconcile_journal(store)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "payload_unverified")
        # Prior revision remains 0 and index untampered
        self.assertEqual(store.revision, 0)
        self.assertNotIn("tampered", store.index)

        # Fixed point test: running it again produces fixed point
        results2 = reconcile_journal(store)
        self.assertEqual(results2, [])

    def test_index_quarantine_corrupt_json(self):
        store_dir = self.runtime_root / "incident-packets"
        store_dir.mkdir(parents=True, exist_ok=True)
        corrupt_file = store_dir / "index.json"
        corrupt_file.write_text("NOT_VALID_JSON{", encoding="utf-8")

        store = load_incident_store(self.runtime_root)
        self.assertEqual(store.revision, 0)
        # Corrupt file should have been renamed/quarantined
        quarantined = list(store_dir.glob("index.corrupt.*"))
        self.assertEqual(len(quarantined), 1)

    def test_index_quarantine_future_schema(self):
        store_dir = self.runtime_root / "incident-packets"
        store_dir.mkdir(parents=True, exist_ok=True)
        future_file = store_dir / "index.json"
        future_file.write_text(json.dumps({"schema_version": 999, "revision": 5}), encoding="utf-8")

        store = load_incident_store(self.runtime_root)
        self.assertEqual(store.revision, 0)
        quarantined = list(store_dir.glob("index.future_schema.*"))
        self.assertEqual(len(quarantined), 1)

    def test_append_only_recurrence_and_retention(self):
        semantic = {
            "failure_class": "WATCHDOG_EXECUTION_LOSS",
            "diagnosis_code": "worker_dead",
            "lifecycle_class": "EXECUTING",
            "role_state_class": "DEAD",
        }
        # First capture
        fam1 = capture_incident(
            self.runtime_root,
            project_id="proj1",
            task_id="task1",
            classification="WATCHDOG_EXECUTION_LOSS",
            semantic=semantic,
            evidence={"log": "error 1"},
            occurrence_key="occ-1",
        )
        self.assertEqual(fam1["recurrence_count"], 1)
        self.assertEqual(len(fam1["evidence_references"]), 1)
        first_packet_id = fam1["packet_id"]

        # Second capture with same semantic fingerprint and occurrence_key (idempotent no-op)
        fam2 = capture_incident(
            self.runtime_root,
            project_id="proj1",
            task_id="task1",
            classification="WATCHDOG_EXECUTION_LOSS",
            semantic=semantic,
            evidence={"log": "error 1"},
            occurrence_key="occ-1",
        )
        self.assertEqual(fam2["recurrence_count"], 1)
        self.assertEqual(fam2["packet_id"], first_packet_id)

        # Third capture with same semantic fingerprint but new occurrence_key (recurrence)
        fam3 = capture_incident(
            self.runtime_root,
            project_id="proj1",
            task_id="task1",
            classification="WATCHDOG_EXECUTION_LOSS",
            semantic=semantic,
            evidence={"log": "error 2"},
            occurrence_key="occ-2",
        )
        self.assertEqual(fam3["recurrence_count"], 2)
        self.assertEqual(len(fam3["evidence_references"]), 2)
        # Packet ID must not change (append-only recurrence to family)
        self.assertEqual(fam3["packet_id"], first_packet_id)

    def test_transient_read_error_leaves_index_intact_without_quarantine_or_unlinking(self):
        store_dir = self.runtime_root / "incident-packets"
        store_dir.mkdir(parents=True, exist_ok=True)
        index_file = store_dir / "index.json"
        original_content = json.dumps({"schema_version": 1, "revision": 10, "families": {}, "candidates": {}})
        index_file.write_text(original_content, encoding="utf-8")

        from unittest.mock import patch
        with patch.object(Path, "read_text", side_effect=OSError("transient device read error")):
            store = load_incident_store(self.runtime_root)
            self.assertTrue(store.index.get("degraded"))
            self.assertIn("unreadable index file", store.index.get("degraded_reason", ""))

        # Crucial: index_file on disk MUST NOT have been unlinked or quarantined!
        self.assertTrue(index_file.is_file())
        self.assertEqual(index_file.read_text(encoding="utf-8"), original_content)
        quarantined = list(store_dir.glob("index.corrupt.*"))
        self.assertEqual(len(quarantined), 0)

    def test_superseded_txn_preserves_live_referenced_side_files(self):
        store = load_incident_store(self.runtime_root)
        # Create an intent that references packet and candidate files
        pkt_rel = "packets/pkt-live.json"
        unref_rel = "packets/pkt-unref.json"
        intent = begin_txn(
            store,
            operation="superseded_test",
            side_files={
                pkt_rel: json.dumps({"packet_id": "pkt-live"}),
                unref_rel: json.dumps({"packet_id": "pkt-unref"}),
            },
            target_index_updater=lambda idx: idx.update({"some_key": True}),
        )

        # Commit an intervening live index with higher revision that references pkt-live but NOT pkt-unref
        live_idx = {
            "schema_version": 1,
            "revision": intent.prior_revision + 5,
            "last_txn_id": "intervening_txn",
            "families": {
                "fam1": {"family_id": "fam1", "packet_id": "pkt-live", "packet_ids": ["pkt-live"]},
            },
            "candidates": {},
        }
        (store.base_dir / "index.json").write_text(json.dumps(live_idx), encoding="utf-8")

        # Now reconcile journal: intent is superseded
        results = reconcile_journal(store)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "superseded_txn")

        # Live referenced file MUST remain in place!
        self.assertTrue((store.base_dir / pkt_rel).is_file())

        # Unreferenced file MUST be moved to orphans!
        self.assertFalse((store.base_dir / unref_rel).exists())
        orphans = list((store.orphans_dir / intent.txn_id).glob("*.json"))
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].name, "pkt-unref.json")


if __name__ == "__main__":
    unittest.main()
