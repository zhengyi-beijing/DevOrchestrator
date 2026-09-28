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


if __name__ == "__main__":
    unittest.main()
