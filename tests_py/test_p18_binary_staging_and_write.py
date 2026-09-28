"""Tests for P18 binary content staging, RFC 4648 Base64 validation, and CAS file writes."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.jobs.models import JobConflictError
from dev_orchestrator.transport.contracts import (
    FileWriteRequest,
    TransportRejectedError,
    WriteContentUpload,
)
from dev_orchestrator.transport.write_store import (
    WriteStagingStore,
    validate_and_decode_base64,
)


class TestP18BinaryStagingAndWrite(unittest.TestCase):
    """Verifies binary staging store, Base64 validation, and atomic CAS writes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.store = WriteStagingStore(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_binary_staging_roundtrip(self):
        """Arbitrary binary data (nulls, binary sequences) stages cleanly and roundtrips bytes."""
        payload = b"\x00\x01\x02\xff\xfe\xfd\xaa\x55hello-world\x00\x00\x00"
        b64_str = base64.b64encode(payload).decode("ascii")
        sha = "sha256:" + hashlib.sha256(payload).hexdigest()

        upload = WriteContentUpload(
            project_id="test-proj",
            host_id="local",
            content_base64=b64_str,
            decoded_size_bytes=len(payload),
            content_sha256=sha,
        )
        staged = self.store.stage_content(upload)
        self.assertEqual(staged.content_sha256, sha)
        self.assertEqual(staged.decoded_size_bytes, len(payload))
        self.assertEqual(staged.project_id, "test-proj")

        retrieved = self.store.get_staged_bytes("test-proj", sha)
        self.assertEqual(retrieved, payload)

    def test_strict_base64_validation(self):
        """Non-base64 characters, unpadded, or malformed base64 fail closed."""
        # Non-base64 chars
        with self.assertRaises(TransportRejectedError):
            validate_and_decode_base64("not!valid@base64====", 10, "sha256:123")

        # Invalid length (not multiple of 4)
        with self.assertRaises(TransportRejectedError):
            validate_and_decode_base64("abcde", 3, "sha256:123")

        # Non-string input
        with self.assertRaises(TransportRejectedError):
            validate_and_decode_base64(12345, 3, "sha256:123")  # type: ignore

    def test_staging_size_and_digest_mismatch_rejections(self):
        """Size disagreement, digest disagreement, or max_bytes breaches raise TransportRejectedError."""
        payload = b"legitimate content"
        b64_str = base64.b64encode(payload).decode("ascii")
        sha = "sha256:" + hashlib.sha256(payload).hexdigest()

        # Declared size mismatch
        with self.assertRaises(TransportRejectedError) as ctx:
            validate_and_decode_base64(b64_str, len(payload) - 5, sha)
        self.assertIn("does not match declared size", str(ctx.exception))

        # Declared digest mismatch
        with self.assertRaises(TransportRejectedError) as ctx:
            validate_and_decode_base64(b64_str, len(payload), "sha256:0000000000000000000000000000000000000000000000000000000000000000")
        self.assertIn("does not match declared digest", str(ctx.exception))

        # Exceeds max_bytes
        with self.assertRaises(TransportRejectedError) as ctx:
            validate_and_decode_base64(b64_str, len(payload), sha, max_bytes=5)
        self.assertIn("exceeds", str(ctx.exception))

    def test_cas_write_if_absent_success_and_conflict(self):
        """Writing with if_absent succeeds on absent target and fails if target already exists."""
        payload = b"initial file contents\n"
        b64_str = base64.b64encode(payload).decode("ascii")
        sha = "sha256:" + hashlib.sha256(payload).hexdigest()

        upload = WriteContentUpload(
            project_id="p1",
            host_id="local",
            content_base64=b64_str,
            decoded_size_bytes=len(payload),
            content_sha256=sha,
        )
        staged = self.store.stage_content(upload)

        target_file = self.repo_dir / "new_file.txt"
        self.assertFalse(target_file.exists())

        req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="write-key-1",
            content_ref=staged.content_ref,
            content_sha256=sha,
            decoded_size_bytes=len(payload),
            if_absent=True,
        )

        res = self.store.apply_file_write(req, [str(self.repo_dir)])
        self.assertEqual(res.status, "ok")
        self.assertTrue(target_file.is_file())
        self.assertEqual(target_file.read_bytes(), payload)

        # Trying if_absent again on now-existing file with different idempotency key fails
        req2 = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="write-key-2",
            content_ref=staged.content_ref,
            content_sha256=sha,
            decoded_size_bytes=len(payload),
            if_absent=True,
        )
        res2 = self.store.apply_file_write(req2, [str(self.repo_dir)])
        self.assertIn(res2.status, ("failed", "rejected", "conflict"))
        self.assertIn("already exists", str(res2.error))

    def test_cas_write_expected_sha256(self):
        """Writing with expected_sha256 replaces file if matching and rejects if mismatched."""
        original = b"v1 content"
        target_file = self.repo_dir / "versioned.txt"
        target_file.write_bytes(original)
        orig_sha = "sha256:" + hashlib.sha256(original).hexdigest()

        new_content = b"v2 content updated"
        b64_new = base64.b64encode(new_content).decode("ascii")
        new_sha = "sha256:" + hashlib.sha256(new_content).hexdigest()

        staged = self.store.stage_content(
            WriteContentUpload("p1", "local", b64_new, len(new_content), new_sha)
        )

        # Wrong expected SHA-256 fails closed without touching destination
        req_wrong = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="update-fail",
            content_ref=staged.content_ref,
            content_sha256=new_sha,
            decoded_size_bytes=len(new_content),
            expected_sha256="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )
        res_fail = self.store.apply_file_write(req_wrong, [str(self.repo_dir)])
        self.assertEqual(res_fail.status, "failed")
        self.assertEqual(target_file.read_bytes(), original)  # unchanged!

        # Correct expected SHA-256 succeeds
        req_ok = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="update-ok",
            content_ref=staged.content_ref,
            content_sha256=new_sha,
            decoded_size_bytes=len(new_content),
            expected_sha256=orig_sha,
        )
        res_ok = self.store.apply_file_write(req_ok, [str(self.repo_dir)])
        self.assertEqual(res_ok.status, "ok")
        self.assertEqual(target_file.read_bytes(), new_content)

    def test_cas_write_requires_exactly_one_precondition(self):
        """FileWriteRequest must specify either if_absent or expected_sha256, not both or neither."""
        req_both = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(self.repo_dir / "f.txt"),
            idempotency_key="k1",
            content_ref="ref",
            content_sha256="sha",
            decoded_size_bytes=10,
            if_absent=True,
            expected_sha256="sha",
        )
        with self.assertRaises(TransportRejectedError):
            self.store.apply_file_write(req_both, [str(self.repo_dir)])

        req_neither = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(self.repo_dir / "f.txt"),
            idempotency_key="k2",
            content_ref="ref",
            content_sha256="sha",
            decoded_size_bytes=10,
            if_absent=None,
            expected_sha256=None,
        )
        with self.assertRaises(TransportRejectedError):
            self.store.apply_file_write(req_neither, [str(self.repo_dir)])

    def test_idempotent_replay_and_conflict_detection(self):
        """Exact identical write replay returns recorded result; conflicting payload raises JobConflictError."""
        payload = b"idempotent file contents"
        b64_str = base64.b64encode(payload).decode("ascii")
        sha = "sha256:" + hashlib.sha256(payload).hexdigest()

        staged = self.store.stage_content(WriteContentUpload("p1", "local", b64_str, len(payload), sha))
        target_file = self.repo_dir / "idem.txt"

        req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="idem-key-99",
            content_ref=staged.content_ref,
            content_sha256=sha,
            decoded_size_bytes=len(payload),
            if_absent=True,
        )

        res1 = self.store.apply_file_write(req, [str(self.repo_dir)])
        self.assertEqual(res1.status, "ok")

        # Replay identical request
        res2 = self.store.apply_file_write(req, [str(self.repo_dir)])
        self.assertEqual(res2.status, "ok")
        self.assertEqual(res2.applied_at, res1.applied_at)

        # Conflicting request with same idempotency key but different target path
        req_conflict = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(self.repo_dir / "other.txt"),
            idempotency_key="idem-key-99",
            content_ref=staged.content_ref,
            content_sha256=sha,
            decoded_size_bytes=len(payload),
            if_absent=True,
        )
        with self.assertRaises(JobConflictError):
            self.store.apply_file_write(req_conflict, [str(self.repo_dir)])

    def test_write_containment_rejection(self):
        """Writing to paths outside configured file roots is rejected."""
        payload = b"escaping"
        b64_str = base64.b64encode(payload).decode("ascii")
        sha = "sha256:" + hashlib.sha256(payload).hexdigest()

        staged = self.store.stage_content(WriteContentUpload("p1", "local", b64_str, len(payload), sha))

        outside_target = self.root / "escaped.txt"
        req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(outside_target),
            idempotency_key="esc-1",
            content_ref=staged.content_ref,
            content_sha256=sha,
            decoded_size_bytes=len(payload),
            if_absent=True,
        )

        with self.assertRaises(TransportRejectedError) as ctx:
            self.store.apply_file_write(req, [str(self.repo_dir)])
        self.assertIn("escapes configured file roots", str(ctx.exception))

    def test_crash_after_replace_before_applied_record_reconciles_to_applied(self):
        """If a crash happens after atomic replace but before record is marked applied, next apply reconciles to applied."""
        initial_payload = b"before write\n"
        target_file = self.repo_dir / "crash_test.txt"
        target_file.write_bytes(initial_payload)
        pre_sha = "sha256:" + hashlib.sha256(initial_payload).hexdigest()

        new_payload = b"after write replaced\n"
        b64_str = base64.b64encode(new_payload).decode("ascii")
        post_sha = "sha256:" + hashlib.sha256(new_payload).hexdigest()

        staged = self.store.stage_content(WriteContentUpload("p1", "local", b64_str, len(new_payload), post_sha))

        idem_key = "crash-rec-1"
        from dev_orchestrator.jobs.config import canonical_path
        from dev_orchestrator.storage.json_store import write_json, read_json, utc_now_iso
        write_id = self.store.derive_write_id("p1", idem_key)
        intent_file = self.store._intent_path(write_id)
        intent_file.parent.mkdir(parents=True, exist_ok=True)
        c_target = canonical_path(target_file)
        canonical_intent = {
            "write_id": write_id,
            "project_id": "p1",
            "host_id": "local",
            "target_path": c_target,
            "idempotency_key": idem_key,
            "content_ref": staged.content_ref,
            "content_sha256": post_sha,
            "decoded_size_bytes": len(new_payload),
            "precondition": {"expected_sha256": pre_sha},
            "file_policy_digest": None,
        }
        import json
        intent_digest = "sha256:" + hashlib.sha256(
            json.dumps(canonical_intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        # Simulate state="claimed" recorded before the atomic replace
        write_json(
            intent_file,
            {
                **canonical_intent,
                "intent_digest": intent_digest,
                "state": "claimed",
                "pre_digest": pre_sha,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )
        # Target file has been replaced on disk with new_payload
        target_file.write_bytes(new_payload)

        # Call apply_file_write
        req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key=idem_key,
            content_ref=staged.content_ref,
            content_sha256=post_sha,
            decoded_size_bytes=len(new_payload),
            expected_sha256=pre_sha,
        )
        res = self.store.apply_file_write(req, [str(self.repo_dir)])
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.pre_digest, pre_sha)
        self.assertEqual(res.post_digest, post_sha)

        saved_intent = read_json(intent_file, {})
        self.assertEqual(saved_intent.get("state"), "applied")
        self.assertEqual(saved_intent.get("pre_digest"), pre_sha)
        self.assertEqual(saved_intent.get("post_digest"), post_sha)

    def test_read_digest_is_accepted_as_write_precondition(self):
        """Read digest format with sha256: prefix is accepted by CAS write without mismatch."""
        from dev_orchestrator.jobs.config import JobProjectConfig, JobsConfig
        from dev_orchestrator.transport.local import LocalMachineTransport
        from dev_orchestrator.transport.contracts import FileReadRequest

        target_file = self.repo_dir / "precond_test.txt"
        initial_data = b"version 1 data\n"
        target_file.write_bytes(initial_data)

        cfg = JobsConfig(
            runtime_root=self.root,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo_dir,
                    file_roots=[self.repo_dir],
                    commands={},
                )
            },
        )
        transport = LocalMachineTransport(jobs_config=cfg)

        read_res = transport.read_file(FileReadRequest(
            project_id="p1",
            path=str(target_file),
            host_id="local",
        ))
        self.assertEqual(read_res.status, "ok")
        self.assertTrue(read_res.content_sha256.startswith("sha256:"))

        # Stage version 2
        v2_data = b"version 2 updated data\n"
        v2_b64 = base64.b64encode(v2_data).decode("ascii")
        v2_sha = "sha256:" + hashlib.sha256(v2_data).hexdigest()
        staged = transport.stage_write_content(WriteContentUpload(
            project_id="p1",
            host_id="local",
            content_base64=v2_b64,
            decoded_size_bytes=len(v2_data),
            content_sha256=v2_sha,
        ))

        # Use read_res.content_sha256 directly as expected_sha256
        write_req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="write-cas-read-precond",
            content_ref=staged.content_ref,
            content_sha256=v2_sha,
            decoded_size_bytes=len(v2_data),
            expected_sha256=read_res.content_sha256,
        )
        write_res = transport.write_file(write_req)
        self.assertEqual(write_res.status, "ok")
        self.assertEqual(write_res.pre_digest, read_res.content_sha256)
        self.assertEqual(target_file.read_bytes(), v2_data)

    def test_concurrent_reconcile_does_not_clobber_terminal_intent(self):
        """Reconciliation reads intent under lock and does not clobber terminal applied state."""
        target_file = self.repo_dir / "term_test.txt"
        content = b"terminal state content\n"
        target_file.write_bytes(content)
        content_sha = "sha256:" + hashlib.sha256(content).hexdigest()

        staged = self.store.stage_content(WriteContentUpload("p1", "local", base64.b64encode(content).decode("ascii"), len(content), content_sha))

        req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=str(target_file),
            idempotency_key="concurrent-term-1",
            content_ref=staged.content_ref,
            content_sha256=content_sha,
            decoded_size_bytes=len(content),
            expected_sha256=content_sha,
        )

        from dev_orchestrator.storage.json_store import write_json, read_json
        write_id = self.store.derive_write_id(req.project_id, req.idempotency_key)
        intent_file = self.store._intent_path(write_id)
        intent_file.parent.mkdir(parents=True, exist_ok=True)
        orig_applied_at = "2026-09-28T10:00:00Z"
        write_json(
            intent_file,
            {
                "write_id": write_id,
                "idempotency_key": req.idempotency_key,
                "project_id": "p1",
                "host_id": "local",
                "target_path": str(target_file.resolve()),
                "content_ref": staged.content_ref,
                "content_sha256": content_sha,
                "decoded_size_bytes": len(content),
                "expected_sha256": content_sha,
                "if_absent": None,
                "state": "applied",
                "pre_digest": content_sha,
                "post_digest": content_sha,
                "applied_at": orig_applied_at,
                "created_at": orig_applied_at,
                "updated_at": orig_applied_at,
            },
        )

        rec_res = self.store.reconcile_file_write(req.project_id, req.idempotency_key)
        self.assertEqual(rec_res.status, "ok")
        self.assertEqual(rec_res.applied_at, orig_applied_at)

        # Ensure intent file was not overwritten with altered state
        saved = read_json(intent_file, {})
        self.assertEqual(saved.get("state"), "applied")
        self.assertEqual(saved.get("applied_at"), orig_applied_at)

    def test_write_rejects_mismatched_host_identity(self):
        """apply_file_write rejects request when request.host_id does not match host_identity."""
        content = b"host mismatch payload"
        content_sha = "sha256:" + hashlib.sha256(content).hexdigest()
        staged = self.store.stage_content(WriteContentUpload("p1", "remote_box", base64.b64encode(content).decode("ascii"), len(content), content_sha))
        target_file = self.root / "mismatch.txt"

        req = FileWriteRequest(
            project_id="p1",
            host_id="remote_box",
            target_path=str(target_file),
            idempotency_key="mismatch-key-1",
            content_ref=staged.content_ref,
            content_sha256=content_sha,
            decoded_size_bytes=len(content),
            if_absent=True,
        )

        with self.assertRaises(TransportRejectedError):
            self.store.apply_file_write(req, [str(self.root)], host_identity="local_box")


if __name__ == "__main__":
    unittest.main()
