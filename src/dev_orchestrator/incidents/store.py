"""Durable journaled incident packet and candidate store beneath runtime/incident-packets/."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

STORE_SCHEMA_VERSION = 1
INCIDENTS_SUBDIR = "incident-packets"


def _store_dir(runtime_root: Path | str) -> Path:
    return Path(runtime_root) / INCIDENTS_SUBDIR


def _lock_path(runtime_root: Path | str) -> Path:
    return _store_dir(runtime_root) / "incidents.lock"


def _index_path(runtime_root: Path | str) -> Path:
    return _store_dir(runtime_root) / "index.json"


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return _sha256_of_bytes(path.read_bytes())
    except (OSError, IOError):
        return None


def _quarantine_index(index_file: Path, raw_text: str, reason: str) -> None:
    stamp = utc_now_iso().replace(":", "-").replace(".", "-")
    content_hash = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()[:12]
    if "future schema" in reason.lower():
        q_name = f"index.future_schema.{stamp}-{content_hash}"
    else:
        q_name = f"index.corrupt.{stamp}-{content_hash}"
    q_path = index_file.parent / q_name
    try:
        q_path.write_text(raw_text, encoding="utf-8")
        index_file.unlink(missing_ok=True)
    except (OSError, IOError):
        pass


def _empty_index(degraded: bool = False, degraded_reason: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "revision": 0,
        "last_txn_id": None,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "families": {},
        "candidates": {},
    }


def index_revision(runtime_root: Path | str) -> int:
    """Return the current authoritative store revision, or 0 if uninitialized/degraded."""
    store = load_incident_store(runtime_root)
    return store.revision


class IncidentStore:
    """Thread- and process-safe transactional store for incident families and candidates."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.base_dir = _store_dir(self.runtime_root)
        self.lock_path = _lock_path(self.runtime_root)
        self.index_path = _index_path(self.runtime_root)
        self.packets_dir = self.base_dir / "packets"
        self.candidates_dir = self.base_dir / "candidates"
        self.journal_dir = self.base_dir / "journal"
        self.intents_dir = self.journal_dir
        self.orphans_dir = self.base_dir / "orphans"
        self._ensure_dirs()
        self.index: dict[str, Any] = self._load_index_failclosed()

    @property
    def lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(self.lock_path)

    @property
    def revision(self) -> int:
        return int(self.index.get("revision", 0))

    @property
    def last_txn_id(self) -> str | None:
        return self.index.get("last_txn_id")

    def _ensure_dirs(self) -> None:
        for directory in (self.base_dir, self.packets_dir, self.candidates_dir, self.journal_dir, self.orphans_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _load_index_failclosed(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return _empty_index()

        try:
            raw_text = self.index_path.read_text(encoding="utf-8-sig")
        except (OSError, IOError) as exc:
            # Transient I/O error reading index: fail closed into degraded mode without deleting the file
            return _empty_index(degraded=True, degraded_reason=f"unreadable index file: {exc}")

        try:
            data = json.loads(raw_text)
        except Exception as exc:
            _quarantine_index(self.index_path, raw_text, f"unparseable json: {exc}")
            return _empty_index(degraded=True, degraded_reason=f"corrupt index: {exc}")

        if not isinstance(data, dict):
            _quarantine_index(self.index_path, raw_text, "index root is not a dict")
            return _empty_index(degraded=True, degraded_reason="index root is not a dict")

        schema_version = data.get("schema_version")
        if isinstance(schema_version, int) and schema_version > STORE_SCHEMA_VERSION:
            _quarantine_index(self.index_path, raw_text, f"future schema_version {schema_version}")
            return _empty_index(degraded=True, degraded_reason=f"future schema_version {schema_version}")

        # Ensure required keys exist
        if not isinstance(data.get("families"), dict):
            data["families"] = {}
        if not isinstance(data.get("candidates"), dict):
            data["candidates"] = {}
        if "revision" not in data or not isinstance(data["revision"], int):
            data["revision"] = 0
        if "schema_version" not in data:
            data["schema_version"] = STORE_SCHEMA_VERSION
        return data

    def reload(self) -> dict[str, Any]:
        """Reload index under lock or fail closed."""
        self.index = self._load_index_failclosed()
        return self.index

    def execute_txn(
        self,
        operation: str,
        mutator: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, str]]],
    ) -> tuple[dict[str, Any], str]:
        """Execute a journaled atomic mutation.

        mutator takes current index copy and returns (updated_index, side_files_dict).
        side_files_dict maps relative paths beneath base_dir to file contents (str or bytes).
        Returns (updated_index, txn_id).
        """
        with InterProcessFileLock(self.lock_path):
            self.reconcile_journal()
            current_index = self._load_index_failclosed()
            prior_rev = int(current_index.get("revision", 0))
            target_rev = prior_rev + 1
            txn_id = f"txn-{uuid4().hex[:12]}"

            next_index, side_files = mutator(copy.deepcopy(current_index))
            next_index["schema_version"] = STORE_SCHEMA_VERSION
            next_index["revision"] = target_rev
            next_index["last_txn_id"] = txn_id

            # Calculate payload hashes
            payload_hashes: dict[str, str] = {}
            for rel_str, content in side_files.items():
                content_bytes = content.encode("utf-8") if isinstance(content, str) else content
                payload_hashes[rel_str] = _sha256_of_bytes(content_bytes)

            intent_data = {
                "txn_id": txn_id,
                "operation": operation,
                "prior_revision": prior_rev,
                "target_revision": target_rev,
                "payload_hashes": payload_hashes,
                "side_files": list(side_files.keys()),
                "target_index": next_index,
                "created_at": utc_now_iso(),
            }
            intent_path = self.journal_dir / f"{txn_id}.json"
            write_json(intent_path, intent_data, indent=2)

            # Write immutable side files
            for rel_str, content in side_files.items():
                target_file = self.base_dir / rel_str
                target_file.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, str):
                    target_file.write_text(content, encoding="utf-8")
                else:
                    target_file.write_bytes(content)

            # Atomic commit point: write index.json
            tmp_index = self.base_dir / f"index.json.tmp-{txn_id}"
            write_json(tmp_index, next_index, indent=2)
            os.replace(tmp_index, self.index_path)

            # Remove journal intent
            intent_path.unlink(missing_ok=True)
            self.index = next_index
            return next_index, txn_id

    def reconcile_journal(self) -> list[dict[str, Any]]:
        """Reconcile unfinished intents.

        Returns list of reconciliation result rows with status in:
        {'committed_confirmed', 'reapplied', 'superseded_txn', 'payload_unverified'}.
        Reaches a fixed point.
        """
        results: list[dict[str, Any]] = []
        if not self.journal_dir.is_dir():
            return results

        intent_files = sorted(self.journal_dir.glob("*.json"), key=lambda p: p.name)
        if not intent_files:
            return results

        live_index = self._load_index_failclosed()
        live_rev = int(live_index.get("revision", 0))
        live_last_txn = live_index.get("last_txn_id")

        for intent_file in intent_files:
            intent = read_json(intent_file, None)
            if not isinstance(intent, dict):
                # Corrupt intent file: move to orphans and continue
                orphan_dest = self.orphans_dir / intent_file.name
                shutil.move(str(intent_file), str(orphan_dest))
                results.append({"status": "payload_unverified", "reason": "unreadable intent file", "txn_id": intent_file.stem})
                continue

            txn_id = str(intent.get("txn_id") or intent_file.stem)
            prior_rev = int(intent.get("prior_revision", -1))
            target_rev = int(intent.get("target_revision", -1))
            payload_hashes = intent.get("payload_hashes") or {}
            target_index = intent.get("target_index")

            # 1. Matching last_txn_id or target_revision already committed
            if live_last_txn == txn_id or (live_rev >= target_rev and live_rev > prior_rev and live_last_txn is not None and target_rev > 0 and live_last_txn == txn_id):
                intent_file.unlink(missing_ok=True)
                results.append({"status": "committed_confirmed", "txn_id": txn_id, "revision": live_rev})
                continue

            # 2. Live revision equals prior_revision -> check payload hashes
            if live_rev == prior_rev:
                verified = True
                if isinstance(payload_hashes, dict):
                    for rel_str, exp_hash in payload_hashes.items():
                        side_p = self.base_dir / rel_str
                        act_hash = _sha256_of_file(side_p)
                        if act_hash != exp_hash:
                            verified = False
                            break

                if verified and isinstance(target_index, dict):
                    # Reapply transaction commit
                    tmp_index = self.base_dir / f"index.json.tmp-{txn_id}"
                    write_json(tmp_index, target_index, indent=2)
                    os.replace(tmp_index, self.index_path)
                    intent_file.unlink(missing_ok=True)
                    live_index = target_index
                    live_rev = int(live_index.get("revision", target_rev))
                    live_last_txn = txn_id
                    results.append({"status": "reapplied", "txn_id": txn_id, "revision": target_rev})
                    continue
                else:
                    # Payload unverified: orphan files and remove intent
                    orphan_txn_dir = self.orphans_dir / txn_id
                    orphan_txn_dir.mkdir(parents=True, exist_ok=True)
                    if isinstance(payload_hashes, dict):
                        for rel_str in payload_hashes:
                            side_p = self.base_dir / rel_str
                            if side_p.is_file():
                                shutil.move(str(side_p), str(orphan_txn_dir / Path(rel_str).name))
                    intent_file.unlink(missing_ok=True)
                    results.append({"status": "payload_unverified", "txn_id": txn_id, "reason": "payload hash mismatch or missing target index"})
                    continue

            # 3. Live revision != prior_revision (superseded by intervening commit)
            orphan_txn_dir = self.orphans_dir / txn_id
            orphan_txn_dir.mkdir(parents=True, exist_ok=True)
            orphaned_any = False
            if isinstance(payload_hashes, dict):
                for rel_str in payload_hashes:
                    side_p = self.base_dir / rel_str
                    if side_p.is_file():
                        # Only orphan if not referenced by the current live index
                        if not self._is_referenced_by_index(live_index, rel_str):
                            shutil.move(str(side_p), str(orphan_txn_dir / Path(rel_str).name))
                            orphaned_any = True
            if not orphaned_any:
                try:
                    orphan_txn_dir.rmdir()
                except OSError:
                    pass
            intent_file.unlink(missing_ok=True)
            results.append({"status": "superseded_txn", "txn_id": txn_id, "prior_revision": prior_rev, "live_revision": live_rev})

        self.index = self._load_index_failclosed()
        return results

    def _is_referenced_by_index(self, live_index: dict[str, Any], rel_str: str) -> bool:
        """Check whether a relative path under base_dir is referenced by live_index."""
        if not isinstance(live_index, dict):
            return False
        norm = rel_str.replace("\\", "/").strip("/")
        parts = norm.split("/")
        if not parts:
            return False

        if parts[0] == "packets":
            pkt_id = Path(parts[-1]).stem
            for fam in live_index.get("families", {}).values():
                if isinstance(fam, dict):
                    if fam.get("packet_id") == pkt_id:
                        return True
                    if pkt_id in (fam.get("packet_ids") or []):
                        return True
            for cand in live_index.get("candidates", {}).values():
                if isinstance(cand, dict) and pkt_id in (cand.get("source_incidents") or []):
                    return True
            return False

        if parts[0] == "candidates" and len(parts) >= 2:
            cand_id = parts[1]
            if cand_id in live_index.get("candidates", {}):
                return True
            for fam in live_index.get("families", {}).values():
                if isinstance(fam, dict) and fam.get("candidate_id") == cand_id:
                    return True
            return False

        def _search(obj: Any) -> bool:
            if isinstance(obj, str):
                return norm in obj.replace("\\", "/")
            if isinstance(obj, dict):
                return any(_search(k) or _search(v) for k, v in obj.items())
            if isinstance(obj, (list, tuple, set)):
                return any(_search(item) for item in obj)
            return False

        return _search(live_index)


def load_incident_store(runtime_root: Path | str) -> IncidentStore:
    """Return an IncidentStore instance for the given runtime root."""
    return IncidentStore(runtime_root)


@dataclass
class TransactionIntent:
    txn_id: str
    operation: str
    prior_revision: int
    target_revision: int
    payload_hashes: dict[str, str]
    side_files: dict[str, Any]
    target_index: dict[str, Any]
    intent_path: Path


def begin_txn(
    store_or_runtime: IncidentStore | Path | str,
    operation: str,
    side_files: Optional[dict[str, Any]] = None,
    target_index_updater: Optional[Callable[[dict[str, Any]], None]] = None,
    mutator: Optional[Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]] = None,
) -> TransactionIntent:
    """Prepare a transaction intent and write side files without committing index.json."""
    store = store_or_runtime if isinstance(store_or_runtime, IncidentStore) else load_incident_store(store_or_runtime)
    prior_rev = int(store.index.get("revision", 0))
    target_rev = prior_rev + 1
    txn_id = f"txn-{uuid4().hex[:12]}"

    if mutator is not None:
        target_index, side_files_dict = mutator(copy.deepcopy(store.index))
    elif target_index_updater is not None:
        target_index = copy.deepcopy(store.index)
        target_index_updater(target_index)
        side_files_dict = dict(side_files or {})
    else:
        target_index = copy.deepcopy(store.index)
        side_files_dict = dict(side_files or {})

    target_index["schema_version"] = STORE_SCHEMA_VERSION
    target_index["revision"] = target_rev
    target_index["last_txn_id"] = txn_id

    payload_hashes: dict[str, str] = {}
    for rel_str, content in side_files_dict.items():
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        payload_hashes[rel_str] = _sha256_of_bytes(content_bytes)

    intent_path = store.journal_dir / f"{txn_id}.json"
    intent_data = {
        "schema_version": STORE_SCHEMA_VERSION,
        "txn_id": txn_id,
        "operation": operation,
        "prior_revision": prior_rev,
        "target_revision": target_rev,
        "payload_hashes": payload_hashes,
        "side_files": list(side_files_dict.keys()),
        "target_index": target_index,
        "intent_path": str(intent_path),
        "created_at": utc_now_iso(),
    }
    write_json(intent_path, intent_data, indent=2)

    for rel_str, content in side_files_dict.items():
        target_file = store.base_dir / rel_str
        target_file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target_file.write_text(content, encoding="utf-8")
        else:
            target_file.write_bytes(content)

    return TransactionIntent(
        txn_id=txn_id,
        operation=operation,
        prior_revision=prior_rev,
        target_revision=target_rev,
        payload_hashes=payload_hashes,
        side_files=side_files_dict,
        target_index=target_index,
        intent_path=intent_path,
    )


def commit_txn(
    store_or_runtime: IncidentStore | Path | str,
    intent: TransactionIntent,
) -> bool:
    """Atomically commit index.json and remove the intent."""
    store = store_or_runtime if isinstance(store_or_runtime, IncidentStore) else load_incident_store(store_or_runtime)
    tmp_index = store.base_dir / f"index.json.tmp-{intent.txn_id}"
    write_json(tmp_index, intent.target_index, indent=2)
    os.replace(tmp_index, store.index_path)
    intent.intent_path.unlink(missing_ok=True)
    store.index = intent.target_index
    return True


def reconcile_journal(store_or_runtime: IncidentStore | Path | str) -> list[dict[str, Any]]:
    """Reconcile unfinished journal intents for runtime root or store."""
    store = store_or_runtime if isinstance(store_or_runtime, IncidentStore) else load_incident_store(store_or_runtime)
    with InterProcessFileLock(store.lock_path):
        return store.reconcile_journal()
