"""Binary content staging and durable CAS file write intent engine.

Provides strict RFC 4648 Base64 validation, atomic staging,
compare-and-swap (CAS) durable write intents, canonical path locking,
and crash-consistent reconciliation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.jobs.config import (
    MAX_FILE_WRITE_BYTES,
    canonical_path,
    is_path_contained,
)
from dev_orchestrator.jobs.models import JobConflictError
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json
from dev_orchestrator.transport.contracts import (
    FileResult,
    FileWriteRequest,
    StagedWriteContent,
    TransportRejectedError,
    WriteContentUpload,
    canonical_sha256,
)

DEFAULT_STAGING_TTL_SECONDS = 3600.0
_STRICT_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def validate_and_decode_base64(
    content_b64: str,
    declared_size: int,
    declared_sha256: str,
    *,
    max_bytes: int = MAX_FILE_WRITE_BYTES,
) -> bytes:
    """Strictly validate and decode RFC 4648 standard Base64.
    
    Rejects malformed encoding, non-canonical padding, size disagreement,
    payloads exceeding max_bytes, and digest mismatch.
    """
    if not isinstance(content_b64, str):
        raise TransportRejectedError("content_base64 must be a string")

    if content_b64 == "":
        if declared_size != 0:
            raise TransportRejectedError(
                f"declared size {declared_size} does not match empty content (expected 0)"
            )
        empty_digest = canonical_sha256(b"")
        if canonical_sha256(declared_sha256) != empty_digest:
            raise TransportRejectedError(
                f"decoded content SHA-256 {empty_digest} does not match declared digest {declared_sha256}"
            )
        return b""

    # Strict character set and padding check
    if len(content_b64) % 4 != 0 or not _STRICT_B64_RE.match(content_b64):
        raise TransportRejectedError("content_base64 is not valid RFC 4648 standard Base64")

    # Early rejection before decoding when encoded length implies decoded size above max_bytes
    min_possible_decoded = (len(content_b64) // 4) * 3 - 2
    if min_possible_decoded > max_bytes:
        raise TransportRejectedError(
            f"encoded payload implies minimum decoded size {min_possible_decoded} exceeds maximum allowed ({max_bytes})"
        )

    derived_max_decoded = (len(content_b64) // 4) * 3
    if declared_size > derived_max_decoded or declared_size > max_bytes:
        raise TransportRejectedError(
            f"declared size {declared_size} exceeds Base64 ceiling or maximum allowed ({max_bytes})"
        )

    try:
        decoded = base64.b64decode(content_b64, validate=True)
    except Exception as exc:
        raise TransportRejectedError(f"Base64 decoding failed: {exc}") from exc

    if len(decoded) != declared_size:
        raise TransportRejectedError(
            f"decoded content size {len(decoded)} does not match declared size {declared_size}"
        )

    if len(decoded) > max_bytes:
        raise TransportRejectedError(
            f"decoded content size {len(decoded)} exceeds maximum limit {max_bytes}"
        )

    actual_digest = canonical_sha256(decoded)
    expected_digest = canonical_sha256(declared_sha256)
    if actual_digest != expected_digest:
        raise TransportRejectedError(
            f"decoded content SHA-256 {actual_digest} does not match declared digest {declared_sha256}"
        )

    return decoded


class WriteStagingStore:
    """Manages project-scoped, digest-addressed binary staging and CAS write intents."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.staging_root = self.runtime_root / "write-staging"
        self.intents_root = self.runtime_root / "write-intents"
        self.locks_root = self.runtime_root / "write-locks"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.intents_root.mkdir(parents=True, exist_ok=True)
        self.locks_root.mkdir(parents=True, exist_ok=True)

    def _project_staging_dir(self, project_id: str) -> Path:
        clean = "".join(c for c in project_id if c.isalnum() or c in ("-", "_"))
        p = self.staging_root / clean
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _blob_path(self, project_id: str, sha256_digest: str) -> Path:
        clean_digest = canonical_sha256(sha256_digest).replace("sha256:", "").replace(":", "_")
        return self._project_staging_dir(project_id) / f"{clean_digest}.blob"

    def _intent_path(self, write_id: str) -> Path:
        return self.intents_root / f"{write_id}.json"

    def _lock_for_path(self, target_path: str) -> Path:
        c_path = canonical_path(target_path)
        path_hash = hashlib.sha256(c_path.encode("utf-8")).hexdigest()[:24]
        return self.locks_root / f"{path_hash}.lock"

    @staticmethod
    def derive_write_id(project_id: str, idempotency_key: str) -> str:
        raw = f"{project_id}:{idempotency_key}".encode("utf-8")
        return "write-" + hashlib.sha256(raw).hexdigest()[:24]

    def stage_content(
        self,
        upload: WriteContentUpload,
        *,
        max_bytes: int = MAX_FILE_WRITE_BYTES,
        ttl_seconds: Optional[float] = None,
    ) -> StagedWriteContent:
        """Atomically persist decoded binary content and return an opaque reference."""
        canonical_content_sha = canonical_sha256(upload.content_sha256)
        decoded = validate_and_decode_base64(
            upload.content_base64,
            upload.decoded_size_bytes,
            canonical_content_sha,
            max_bytes=max_bytes,
        )

        blob_path = self._blob_path(upload.project_id, canonical_content_sha)
        meta_path = blob_path.with_suffix(".meta.json")
        content_ref = f"stage:{upload.project_id}:{canonical_content_sha}"

        ttl = float(ttl_seconds if ttl_seconds is not None else DEFAULT_STAGING_TTL_SECONDS)
        now_dt = datetime.now(timezone.utc)
        exp_dt = now_dt + timedelta(seconds=ttl)
        staged_at = now_dt.isoformat()
        expires_at = exp_dt.isoformat()

        if blob_path.is_file():
            # Idempotent re-upload: verify existing blob
            existing_bytes = blob_path.read_bytes()
            if len(existing_bytes) == upload.decoded_size_bytes:
                existing_digest = canonical_sha256(existing_bytes)
                if existing_digest == canonical_content_sha:
                    existing_staged_at = staged_at
                    existing_expires_at = expires_at
                    if meta_path.is_file():
                        try:
                            m_data = read_json(meta_path)
                            if isinstance(m_data, dict):
                                existing_staged_at = m_data.get("staged_at", staged_at)
                                existing_expires_at = m_data.get("expires_at", expires_at)
                        except Exception:
                            pass
                    return StagedWriteContent(
                        content_ref=content_ref,
                        content_sha256=canonical_content_sha,
                        decoded_size_bytes=upload.decoded_size_bytes,
                        project_id=upload.project_id,
                        host_id=upload.host_id,
                        staged_at=existing_staged_at,
                        expires_at=existing_expires_at,
                    )

        # Atomic write to temporary file then replace
        tmp_blob = blob_path.parent / f"{blob_path.name}.tmp.{uuid4().hex}"
        with open(tmp_blob, "wb") as f:
            f.write(decoded)
            f.flush()
            os.fsync(f.fileno())
        tmp_blob.replace(blob_path)

        meta_data = {
            "content_ref": content_ref,
            "content_sha256": canonical_content_sha,
            "decoded_size_bytes": upload.decoded_size_bytes,
            "project_id": upload.project_id,
            "host_id": upload.host_id,
            "staged_at": staged_at,
            "expires_at": expires_at,
        }
        tmp_meta = meta_path.parent / f"{meta_path.name}.tmp.{uuid4().hex}"
        write_json(tmp_meta, meta_data, indent=2)
        tmp_meta.replace(meta_path)

        return StagedWriteContent(
            content_ref=content_ref,
            content_sha256=canonical_content_sha,
            decoded_size_bytes=upload.decoded_size_bytes,
            project_id=upload.project_id,
            host_id=upload.host_id,
            staged_at=staged_at,
            expires_at=expires_at,
        )

    def collect_expired_staged_content(
        self,
        *,
        now: Optional[str] = None,
    ) -> list[str]:
        """Collect expired unreferenced staged blobs.

        Skips any blob referenced by a non-terminal write intent and deletes
        only expired unreferenced blobs.
        """
        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        # 1. Identify active (non-terminal) intent blob references
        active_refs: set[tuple[str, str]] = set()  # (project_id, clean_digest)
        terminal_states = {"applied", "failed", "ambiguous_requires_human"}
        if self.intents_root.is_dir():
            for intent_file in self.intents_root.glob("*.json"):
                try:
                    data = read_json(intent_file)
                    if isinstance(data, dict):
                        state = data.get("state")
                        if state not in terminal_states:
                            proj = data.get("project_id")
                            c_sha = data.get("content_sha256")
                            if proj and c_sha:
                                clean_sha = canonical_sha256(c_sha).replace("sha256:", "").replace(":", "_")
                                active_refs.add((proj, clean_sha))
                except Exception:
                    pass

        # 2. Iterate staging directories and delete expired unreferenced blobs
        deleted: list[str] = []
        if self.staging_root.is_dir():
            for proj_dir in self.staging_root.iterdir():
                if not proj_dir.is_dir():
                    continue
                proj_id = proj_dir.name
                for blob_file in proj_dir.glob("*.blob"):
                    clean_sha = blob_file.stem
                    if (proj_id, clean_sha) in active_refs:
                        # Retain referenced blobs regardless of expiration
                        continue

                    meta_file = blob_file.with_suffix(".meta.json")
                    is_expired = False
                    content_ref = f"stage:{proj_id}:sha256:{clean_sha}"

                    if meta_file.is_file():
                        try:
                            meta = read_json(meta_file)
                            if isinstance(meta, dict):
                                exp_str = meta.get("expires_at")
                                if exp_str:
                                    exp_dt = datetime.fromisoformat(exp_str)
                                    if exp_dt.tzinfo is None:
                                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                                    if exp_dt <= now_dt:
                                        is_expired = True
                                if meta.get("content_ref"):
                                    content_ref = meta["content_ref"]
                        except Exception:
                            pass
                    else:
                        mtime_dt = datetime.fromtimestamp(blob_file.stat().st_mtime, tz=timezone.utc)
                        if (now_dt - mtime_dt).total_seconds() > DEFAULT_STAGING_TTL_SECONDS:
                            is_expired = True

                    if is_expired:
                        try:
                            blob_file.unlink(missing_ok=True)
                            if meta_file.is_file():
                                meta_file.unlink(missing_ok=True)
                            deleted.append(content_ref)
                        except Exception:
                            pass

        return deleted

    def get_staged_bytes(self, project_id: str, sha256_digest: str) -> Optional[bytes]:
        """Read and verify staged blob bytes."""
        canonical_digest = canonical_sha256(sha256_digest)
        blob_path = self._blob_path(project_id, canonical_digest)
        if not blob_path.is_file():
            return None
        data = blob_path.read_bytes()
        actual_digest = canonical_sha256(data)
        if actual_digest != canonical_digest:
            return None
        return data

    def apply_file_write(
        self,
        request: FileWriteRequest,
        allowed_roots: list[str],
        *,
        host_identity: str = "",
    ) -> FileResult:
        """Apply a compare-and-swap file write under canonical path lock."""
        # 0. Host identity check
        import socket
        resolved_local = host_identity or socket.gethostname()
        req_host = request.host_id or "local"
        valid_hosts = {"local", resolved_local, socket.gethostname()}
        if req_host not in valid_hosts:
            raise TransportRejectedError(
                f"write request host_id {req_host!r} does not match local host identity {resolved_local!r}"
            )

        # 1. Precondition validation: exactly one precondition must be specified
        has_if_absent = request.if_absent is not None and bool(request.if_absent)
        has_expected_sha = bool(request.expected_sha256 and request.expected_sha256.strip())
        if (has_if_absent and has_expected_sha) or (not has_if_absent and not has_expected_sha):
            raise TransportRejectedError(
                "FileWriteRequest must specify exactly one precondition: 'if_absent' or 'expected_sha256'"
            )

        # 2. Path containment check
        c_target = canonical_path(request.target_path)
        is_contained = False
        for root in allowed_roots:
            if is_path_contained(root, c_target):
                is_contained = True
                break
        if not is_contained:
            raise TransportRejectedError(
                f"target path {request.target_path!r} escapes configured file roots {allowed_roots}"
            )

        target_file = Path(c_target)
        lock_file = self._lock_for_path(c_target)
        write_id = self.derive_write_id(request.project_id, request.idempotency_key)
        intent_file = self._intent_path(write_id)

        canonical_content_sha = canonical_sha256(request.content_sha256) if request.content_sha256 else ""
        canonical_expected_sha = canonical_sha256(request.expected_sha256) if has_expected_sha else None

        canonical_intent = {
            "write_id": write_id,
            "project_id": request.project_id,
            "host_id": request.host_id,
            "target_path": c_target,
            "idempotency_key": request.idempotency_key,
            "content_ref": request.content_ref,
            "content_sha256": canonical_content_sha,
            "decoded_size_bytes": request.decoded_size_bytes,
            "precondition": {"if_absent": True} if has_if_absent else {"expected_sha256": canonical_expected_sha},
            "file_policy_digest": request.expected_file_policy_digest,
        }
        intent_digest = "sha256:" + hashlib.sha256(
            json.dumps(canonical_intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        with InterProcessFileLock(lock_file):
            # Check existing intent record
            existing_intent = read_json(intent_file, None)
            recorded_pre_digest: Optional[str] = None
            if isinstance(existing_intent, dict):
                # Verify intent identity
                if existing_intent.get("intent_digest") != intent_digest:
                    raise JobConflictError(
                        f"idempotency key {request.idempotency_key!r} already used with conflicting write intent"
                    )
                # If terminal result already recorded, return it directly
                if existing_intent.get("state") == "applied":
                    return FileResult(
                        status="ok",
                        path=c_target,
                        content_sha256=existing_intent.get("post_digest"),
                        size_bytes=request.decoded_size_bytes,
                        pre_digest=existing_intent.get("pre_digest"),
                        post_digest=existing_intent.get("post_digest"),
                        applied_at=existing_intent.get("applied_at"),
                        host_identity=host_identity,
                    )
                if existing_intent.get("state") == "failed":
                    return FileResult(
                        status="failed",
                        path=c_target,
                        host_identity=host_identity,
                        error=existing_intent.get("error", "precondition_failed"),
                    )
                if existing_intent.get("state") == "ambiguous_requires_human":
                    return FileResult(
                        status="ambiguous",
                        path=c_target,
                        pre_digest=existing_intent.get("pre_digest"),
                        post_digest=existing_intent.get("post_digest"),
                        host_identity=host_identity,
                        error=existing_intent.get("error", "ambiguous_requires_human"),
                    )

                if existing_intent.get("state") == "claimed":
                    # Finding 5: Reconcile before re-evaluating
                    curr_digest: Optional[str] = None
                    if target_file.is_file():
                        curr_digest = canonical_sha256(target_file.read_bytes())
                    rec_pre = existing_intent.get("pre_digest")
                    exp_post = existing_intent.get("content_sha256")

                    if curr_digest == exp_post:
                        applied_at = utc_now_iso()
                        existing_intent["state"] = "applied"
                        existing_intent["post_digest"] = curr_digest
                        existing_intent["applied_at"] = applied_at
                        existing_intent["updated_at"] = applied_at
                        write_json(intent_file, existing_intent, indent=2)
                        return FileResult(
                            status="ok",
                            path=c_target,
                            content_sha256=curr_digest,
                            size_bytes=request.decoded_size_bytes,
                            pre_digest=rec_pre,
                            post_digest=curr_digest,
                            applied_at=applied_at,
                            host_identity=host_identity,
                        )
                    if curr_digest == rec_pre:
                        recorded_pre_digest = rec_pre
                    else:
                        existing_intent["state"] = "ambiguous_requires_human"
                        existing_intent["error"] = f"target_digest_mismatch ({curr_digest} matches neither pre nor post)"
                        existing_intent["updated_at"] = utc_now_iso()
                        write_json(intent_file, existing_intent, indent=2)
                        return FileResult(
                            status="ambiguous",
                            path=c_target,
                            pre_digest=rec_pre,
                            post_digest=curr_digest,
                            host_identity=host_identity,
                            error="ambiguous_requires_human",
                        )

            # Re-read and verify staged bytes
            staged_bytes = self.get_staged_bytes(request.project_id, canonical_content_sha)
            if staged_bytes is None or len(staged_bytes) != request.decoded_size_bytes:
                raise TransportRejectedError(
                    f"staged content {request.content_ref} missing or digest mismatch in staging"
                )

            # Evaluate precondition against current target
            if recorded_pre_digest is not None:
                pre_digest = recorded_pre_digest
            else:
                pre_digest = None
                if target_file.is_file():
                    pre_digest = canonical_sha256(target_file.read_bytes())

                if has_if_absent:
                    if target_file.exists():
                        intent_data = {
                            **canonical_intent,
                            "intent_digest": intent_digest,
                            "state": "failed",
                            "error": "precondition_failed: file already exists",
                            "pre_digest": pre_digest,
                            "updated_at": utc_now_iso(),
                        }
                        write_json(intent_file, intent_data, indent=2)
                        return FileResult(
                            status="failed",
                            path=c_target,
                            pre_digest=pre_digest,
                            host_identity=host_identity,
                            error="precondition_failed: target file already exists",
                        )
                elif has_expected_sha:
                    if not target_file.is_file() or pre_digest != canonical_expected_sha:
                        intent_data = {
                            **canonical_intent,
                            "intent_digest": intent_digest,
                            "state": "failed",
                            "error": f"precondition_failed: digest mismatch ({pre_digest} != {canonical_expected_sha})",
                            "pre_digest": pre_digest,
                            "updated_at": utc_now_iso(),
                        }
                        write_json(intent_file, intent_data, indent=2)
                        return FileResult(
                            status="failed",
                            path=c_target,
                            pre_digest=pre_digest,
                            host_identity=host_identity,
                            error=f"precondition_failed: target digest {pre_digest} != {canonical_expected_sha}",
                        )

            # Record intent as claimed before mutating
            intent_data = {
                **canonical_intent,
                "intent_digest": intent_digest,
                "state": "claimed",
                "pre_digest": pre_digest,
                "claimed_at": existing_intent.get("claimed_at", utc_now_iso()) if isinstance(existing_intent, dict) else utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            write_json(intent_file, intent_data, indent=2)

            # Atomically replace target file
            target_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target_file.parent / f"{target_file.name}.tmp.{uuid4().hex}"
            with open(tmp_target, "wb") as f:
                f.write(staged_bytes)
                f.flush()
                os.fsync(f.fileno())
            tmp_target.replace(target_file)

            # Verify post-digest of target file
            post_bytes = target_file.read_bytes()
            post_digest = canonical_sha256(post_bytes)
            if post_digest != canonical_content_sha:
                intent_data["state"] = "ambiguous_requires_human"
                intent_data["error"] = "post_digest_verification_failed"
                intent_data["updated_at"] = utc_now_iso()
                write_json(intent_file, intent_data, indent=2)
                return FileResult(
                    status="ambiguous",
                    path=c_target,
                    pre_digest=pre_digest,
                    post_digest=post_digest,
                    host_identity=host_identity,
                    error="post_digest_verification_failed",
                )

            # Mark applied
            applied_at = utc_now_iso()
            intent_data["state"] = "applied"
            intent_data["post_digest"] = post_digest
            intent_data["applied_at"] = applied_at
            intent_data["updated_at"] = applied_at
            write_json(intent_file, intent_data, indent=2)

            return FileResult(
                status="ok",
                path=c_target,
                content_sha256=post_digest,
                size_bytes=len(post_bytes),
                pre_digest=pre_digest,
                post_digest=post_digest,
                applied_at=applied_at,
                host_identity=host_identity,
            )

    def reconcile_file_write(
        self,
        project_id: str,
        idempotency_key: str,
        *,
        host_identity: str = "",
    ) -> FileResult:
        """Reconcile the durable status of an ambiguous or pending write intent."""
        write_id = self.derive_write_id(project_id, idempotency_key)
        intent_file = self._intent_path(write_id)
        if not intent_file.is_file():
            return FileResult(
                status="rejected",
                path="",
                host_identity=host_identity,
                error="write_intent_not_found",
            )

        prelim_intent = read_json(intent_file, {})
        if not isinstance(prelim_intent, dict):
            return FileResult(
                status="ambiguous",
                path="",
                host_identity=host_identity,
                error="corrupt_write_intent",
            )

        c_target = prelim_intent.get("target_path", "")
        if not c_target:
            return FileResult(
                status="ambiguous",
                path="",
                host_identity=host_identity,
                error="corrupt_write_intent: missing target_path",
            )
        lock_file = self._lock_for_path(c_target)

        with InterProcessFileLock(lock_file):
            intent = read_json(intent_file, {})
            if not isinstance(intent, dict):
                return FileResult(
                    status="ambiguous",
                    path=c_target,
                    host_identity=host_identity,
                    error="corrupt_write_intent",
                )
            if intent.get("target_path") != c_target:
                return FileResult(
                    status="ambiguous",
                    path=c_target,
                    host_identity=host_identity,
                    error="intent_target_path_mismatch",
                )

            state = intent.get("state")
            if state == "applied":
                return FileResult(
                    status="ok",
                    path=c_target,
                    content_sha256=intent.get("post_digest"),
                    size_bytes=intent.get("decoded_size_bytes"),
                    pre_digest=intent.get("pre_digest"),
                    post_digest=intent.get("post_digest"),
                    applied_at=intent.get("applied_at"),
                    host_identity=host_identity,
                )
            if state == "failed":
                return FileResult(
                    status="failed",
                    path=c_target,
                    host_identity=host_identity,
                    error=intent.get("error", "precondition_failed"),
                )

            # Reconcile against current target file
            target_file = Path(c_target)
            curr_digest: Optional[str] = None
            if target_file.is_file():
                curr_digest = canonical_sha256(target_file.read_bytes())

            expected_post = intent.get("content_sha256")
            expected_pre = intent.get("pre_digest")

            if curr_digest == expected_post:
                applied_at = utc_now_iso()
                intent["state"] = "applied"
                intent["post_digest"] = curr_digest
                intent["applied_at"] = applied_at
                intent["updated_at"] = applied_at
                write_json(intent_file, intent, indent=2)
                return FileResult(
                    status="ok",
                    path=c_target,
                    content_sha256=curr_digest,
                    size_bytes=intent.get("decoded_size_bytes"),
                    pre_digest=expected_pre,
                    post_digest=curr_digest,
                    applied_at=applied_at,
                    host_identity=host_identity,
                )

            if curr_digest == expected_pre:
                return FileResult(
                    status="failed",
                    path=c_target,
                    pre_digest=curr_digest,
                    host_identity=host_identity,
                    error="write_not_applied_safe_to_retry",
                )

            # Target matches neither pre nor post
            intent["state"] = "ambiguous_requires_human"
            intent["error"] = f"target_digest_mismatch ({curr_digest} matches neither pre nor post)"
            intent["updated_at"] = utc_now_iso()
            write_json(intent_file, intent, indent=2)
            return FileResult(
                status="ambiguous",
                path=c_target,
                pre_digest=expected_pre,
                post_digest=curr_digest,
                host_identity=host_identity,
                error="ambiguous_requires_human",
            )
