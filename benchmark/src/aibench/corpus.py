"""Synthetic corpus generation and manifest verification for aibench."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .contracts import canonical_json, sha256_bytes

_SYNTH_PYPROJECT = """[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "synth-app"
version = "1.0.0"
description = "Synthetic reference codebase for AI benchmarking"
requires-python = ">=3.10"
"""

_AUTH_PY = '''"""Authentication and authorization module.

OWNERSHIP_DOMAIN: Security Infrastructure Team
RESPONSIBILITIES:
- Token validation and JWT issuance
- Role-based access control (RBAC)
- Identity verification
"""
from __future__ import annotations

from typing import Any


class AuthenticationError(Exception):
    """Raised when authentication credentials are invalid or missing."""


class TokenManager:
    """Manages secure access tokens."""

    def __init__(self, secret_key: str = "synth-secret") -> None:
        self.secret_key = secret_key
        self._tokens: dict[str, str] = {}

    def issue_token(self, user_id: str, role: str) -> str:
        token = f"tok_{user_id}_{role}_{len(self._tokens)}"
        self._tokens[token] = user_id
        return token

    def validate_token(self, token: str) -> str:
        if token not in self._tokens:
            raise AuthenticationError(f"invalid token: {token}")
        return self._tokens[token]


class Authenticator:
    """RBAC authentication gate."""

    def __init__(self, token_manager: TokenManager) -> None:
        self.token_manager = token_manager
        self.allowed_roles: set[str] = {"admin", "operator", "user"}

    def authenticate_request(self, auth_header: str | None) -> str:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationError("missing or malformed Authorization header")
        token = auth_header[7:].strip()
        return self.token_manager.validate_token(token)
'''

_CACHE_PY = '''"""In-memory cache implementation with LRU eviction.

OWNERSHIP_DOMAIN: Platform Performance Team
RESPONSIBILITIES:
- Low-latency data caching
- Cache expiration and eviction policy
"""
from __future__ import annotations

import time
from typing import Any, Callable


class CacheEntry:
    def __init__(self, value: Any, ttl: float | None = None) -> None:
        self.value = value
        self.expires_at = (time.monotonic() + ttl) if ttl is not None else None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.monotonic() > self.expires_at


class LRUCache:
    """Simple LRU cache with expiration support."""

    def __init__(self, capacity: int = 100) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._store: dict[str, CacheEntry] = {}
        self._access_order: list[str] = []

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            self.delete(key)
            return None
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
        return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if key in self._store:
            if key in self._access_order:
                self._access_order.remove(key)
        elif len(self._store) >= self.capacity:
            oldest = self._access_order.pop(0)
            self._store.pop(oldest, None)
        self._store[key] = CacheEntry(value, ttl)
        self._access_order.append(key)

    def delete(self, key: str) -> bool:
        if key in self._store:
            self._store.pop(key, None)
            if key in self._access_order:
                self._access_order.remove(key)
            return True
        return False

    def clear(self) -> None:
        self._store.clear()
        self._access_order.clear()

    # NOTE FOR BENCHMARK WORKER:
    # get_or_set(self, key: str, default_fn: Callable[[], Any], ttl: float | None = None) -> Any
    # is intentionally not implemented in this revision.
'''

_BILLING_PY = '''"""Billing and invoicing business service.

OWNERSHIP_DOMAIN: Core Business Logic Team
RESPONSIBILITIES:
- Order invoice generation
- Transaction coordination
- Interaction with AccountRepository
"""
from __future__ import annotations

from typing import Any
from synth_app.repository.account_repo import AccountRepository, CurrencyConversionError


class BillingService:
    """Coordinates account billing transactions."""

    def __init__(self, account_repo: AccountRepository) -> None:
        self.account_repo = account_repo

    def process_charge(self, account_id: str, amount: float, currency: str) -> dict[str, Any]:
        """Process a debit charge against an account balance."""
        if amount <= 0:
            raise ValueError("charge amount must be positive")
        # Line 24: delegates directly to account repository
        tx_id = self.account_repo.charge_account(account_id, amount, currency)
        return {
            "status": "success",
            "account_id": account_id,
            "transaction_id": tx_id,
            "amount": amount,
            "currency": currency,
        }
'''

_ACCOUNT_REPO_PY = '''"""Account data repository with persistent storage abstractions.

OWNERSHIP_DOMAIN: Data Layer Team
RESPONSIBILITIES:
- Account balance persistence
- Transaction journal records
- Currency validation
"""
from __future__ import annotations

from typing import Any
import uuid


class CurrencyConversionError(Exception):
    """Raised when currency conversion fails or unsupported currency is passed."""


class AccountRepository:
    """In-memory account repository."""

    def __init__(self) -> None:
        self.balances: dict[str, float] = {
            "acc_1001": 1500.0,
            "acc_1002": 500.0,
            "acc_1003": 25.0,
        }
        self.transactions: list[dict[str, Any]] = []

    def get_balance(self, account_id: str) -> float:
        if account_id not in self.balances:
            raise KeyError(f"unknown account: {account_id}")
        return self.balances[account_id]

    def charge_account(self, account_id: str, amount: float, currency: str) -> str:
        """Debit the account balance in specified currency.

        Cross-file defect location:
        Fails if currency is not string or unsupported.
        """
        if account_id not in self.balances:
            raise KeyError(f"unknown account: {account_id}")
        if currency != "USD":
            # Line 44: defect triggered when currency is None or non-USD
            raise CurrencyConversionError(f"unsupported currency {currency!r}")
        if self.balances[account_id] < amount:
            raise ValueError(f"insufficient funds in account {account_id}")
        self.balances[account_id] -= amount
        tx_id = f"tx_{uuid.uuid4().hex[:8]}"
        self.transactions.append({
            "tx_id": tx_id,
            "account_id": account_id,
            "amount": amount,
            "currency": currency,
        })
        return tx_id
'''

_API_HANDLER_PY = '''"""HTTP API endpoint handlers for account services.

OWNERSHIP_DOMAIN: Application API Gateway Team
RESPONSIBILITIES:
- Request payload deserialization and validation
- Invocation of billing service
- Error envelope mapping
"""
from __future__ import annotations

from typing import Any
from synth_app.services.billing import BillingService


class TransactionHandler:
    """Handles external transaction requests."""

    def __init__(self, billing_service: BillingService) -> None:
        self.billing_service = billing_service

    def handle_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle incoming charge request payload.

        Root cause defect origin:
        Payload key 'currency' is expected by clients, but code reads 'curr'.
        When 'curr' is missing, it evaluates to None, passing None into BillingService.
        """
        account_id = payload.get("account_id")
        if not account_id:
            return {"status": "error", "message": "missing account_id"}
        amount = payload.get("amount")
        if amount is None or amount <= 0:
            return {"status": "error", "message": "invalid amount"}
        # Line 32: BUG ORIGIN: payload.get('curr') instead of payload.get('currency', 'USD')
        currency = payload.get("curr")
        try:
            result = self.billing_service.process_charge(account_id, float(amount), currency)  # type: ignore[arg-type]
            return {"status": "ok", "data": result}
        except Exception as exc:
            return {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
'''

_LEGACY_API_PY = '''"""Legacy v1 API adapter for backward compatibility.

OWNERSHIP_DOMAIN: Application API Gateway Team / Compatibility Guild
RATIONALE:
- Preserves v1 URL query parameters ('acc_id', 'val', 'cur')
- Maps legacy payloads into v2 canonical format
- Emits deprecation warnings while maintaining zero breaking changes
- Deprecation horizon: scheduled removal in v3.0 (Q4 2027)
"""
from __future__ import annotations

import warnings
from typing import Any
from synth_app.handlers.api import TransactionHandler


class LegacyTransactionAdapter:
    """Adapts legacy v1 requests to current TransactionHandler."""

    def __init__(self, handler: TransactionHandler) -> None:
        self.handler = handler

    def handle_v1_request(self, query_params: dict[str, Any]) -> dict[str, Any]:
        """Map legacy query params to modern transaction payload."""
        warnings.warn(
            "v1 transaction endpoint is deprecated; migrate to v2 payload format",
            DeprecationWarning,
            stacklevel=2,
        )
        # Compatibility mapping:
        # 'acc_id' -> 'account_id'
        # 'val' -> 'amount'
        # 'cur' -> 'curr' (or 'currency')
        account_id = query_params.get("acc_id")
        amount = query_params.get("val")
        currency = query_params.get("cur", "USD")
        v2_payload = {
            "account_id": account_id,
            "amount": amount,
            "curr": currency,
        }
        return self.handler.handle_charge(v2_payload)
'''

_TEST_AUTH_PY = '''import unittest
from synth_app.core.auth import TokenManager, Authenticator, AuthenticationError

class TestAuth(unittest.TestCase):
    def test_token_issue_and_validation(self):
        mgr = TokenManager()
        tok = mgr.issue_token("u123", "admin")
        self.assertEqual(mgr.validate_token(tok), "u123")

    def test_invalid_token_fails(self):
        mgr = TokenManager()
        with self.assertRaises(AuthenticationError):
            mgr.validate_token("nonexistent")
'''

_TEST_CACHE_PY = '''import unittest
from synth_app.core.cache import LRUCache

class TestCache(unittest.TestCase):
    def test_basic_set_get(self):
        cache = LRUCache(capacity=2)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("b"), 2)

    def test_lru_eviction(self):
        cache = LRUCache(capacity=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")
        cache.set("c", 3)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)

    def test_cache_get_or_set(self):
        cache = LRUCache(capacity=5)
        # Tests get_or_set method when implemented by benchmark worker
        if not hasattr(cache, "get_or_set"):
            self.skipTest("get_or_set not implemented yet")
        called = 0
        def factory():
            nonlocal called
            called += 1
            return "computed_val"
        val1 = cache.get_or_set("key1", factory)
        self.assertEqual(val1, "computed_val")
        self.assertEqual(called, 1)
        val2 = cache.get_or_set("key1", factory)
        self.assertEqual(val2, "computed_val")
        self.assertEqual(called, 1)  # factory not called again
'''

_TEST_BILLING_PY = '''import unittest
from synth_app.repository.account_repo import AccountRepository
from synth_app.services.billing import BillingService

class TestBilling(unittest.TestCase):
    def test_successful_charge(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        res = svc.process_charge("acc_1001", 100.0, "USD")
        self.assertEqual(res["status"], "success")
        self.assertEqual(repo.get_balance("acc_1001"), 1400.0)
'''

_TEST_API_PY = '''import unittest
from synth_app.repository.account_repo import AccountRepository
from synth_app.services.billing import BillingService
from synth_app.handlers.api import TransactionHandler

class TestAPI(unittest.TestCase):
    def test_charge_success_with_curr(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        handler = TransactionHandler(svc)
        # When 'curr' is explicitly supplied:
        res = handler.handle_charge({"account_id": "acc_1001", "amount": 50.0, "curr": "USD"})
        self.assertEqual(res["status"], "ok")

    def test_reproduce_cross_file_currency_defect(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        handler = TransactionHandler(svc)
        # Normal client passes 'currency': 'USD', but handler looks for 'curr',
        # causing currency=None, triggering CurrencyConversionError in account_repo
        res = handler.handle_charge({"account_id": "acc_1001", "amount": 50.0, "currency": "USD"})
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_type"], "CurrencyConversionError")
'''

_TEST_COMPAT_PY = '''import unittest
import warnings
from synth_app.repository.account_repo import AccountRepository
from synth_app.services.billing import BillingService
from synth_app.handlers.api import TransactionHandler
from synth_app.compat.legacy_api import LegacyTransactionAdapter

class TestCompat(unittest.TestCase):
    def test_v1_compatibility_wrapper(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        handler = TransactionHandler(svc)
        adapter = LegacyTransactionAdapter(handler)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            res = adapter.handle_v1_request({"acc_id": "acc_1001", "val": 25.0, "cur": "USD"})
            self.assertEqual(res["status"], "ok")
            self.assertTrue(any(issubclass(item.category, DeprecationWarning) for item in w))
'''

_ARCH_MD = """# SynthApp Architecture Ownership and Dependency Boundaries

## Ownership Matrix

| Domain | Responsible Team | Files |
| --- | --- | --- |
| Security & Auth | Security Infrastructure Team | `src/synth_app/core/auth.py` |
| Performance / Caching | Platform Performance Team | `src/synth_app/core/cache.py` |
| Billing Logic | Core Business Logic Team | `src/synth_app/services/billing.py` |
| Data Layer | Data Layer Team | `src/synth_app/repository/account_repo.py` |
| API Gateway | Application API Gateway Team | `src/synth_app/handlers/api.py` |
| Backward Compatibility | Compatibility Guild | `src/synth_app/compat/legacy_api.py` |

## Dependency Flow
`handlers/api.py` -> `services/billing.py` -> `repository/account_repo.py`
`compat/legacy_api.py` wraps `handlers/api.py`.
`core/auth.py` and `core/cache.py` provide cross-cutting foundation without downstream dependencies.
"""

_COMPAT_MD = """# Backward Compatibility and Migration Rationale

## Rationale
V1 client integrations use query parameter notation (`acc_id`, `val`, `cur`).
To avoid breaking customer automations, `LegacyTransactionAdapter` maps these fields
to the canonical v2 schema and emits runtime deprecation warnings.
Removal is scheduled for v3.0 (Q4 2027).
"""

CORPUS_FILES: dict[str, str] = {
    "pyproject.toml": _SYNTH_PYPROJECT,
    "src/synth_app/__init__.py": '"""SynthApp package."""\n__version__ = "1.0.0"\n',
    "src/synth_app/core/auth.py": _AUTH_PY,
    "src/synth_app/core/cache.py": _CACHE_PY,
    "src/synth_app/services/billing.py": _BILLING_PY,
    "src/synth_app/repository/account_repo.py": _ACCOUNT_REPO_PY,
    "src/synth_app/handlers/api.py": _API_HANDLER_PY,
    "src/synth_app/compat/legacy_api.py": _LEGACY_API_PY,
    "tests/test_auth.py": _TEST_AUTH_PY,
    "tests/test_cache.py": _TEST_CACHE_PY,
    "tests/test_billing.py": _TEST_BILLING_PY,
    "tests/test_api.py": _TEST_API_PY,
    "tests/test_compat.py": _TEST_COMPAT_PY,
    "docs/architecture.md": _ARCH_MD,
    "docs/compatibility.md": _COMPAT_MD,
}


def build_manifest(root: Path) -> dict[str, Any]:
    """Compute normalized manifest of relative paths, sizes, and sha256 hashes."""
    manifest_entries: dict[str, dict[str, Any]] = {}
    for rel_path, content in sorted(CORPUS_FILES.items()):
        file_path = root / rel_path
        if not file_path.exists():
            continue
        data = file_path.read_bytes()
        # Normalize CRLF to LF for deterministic hash computation across platforms
        normalized_data = data.replace(b"\r\n", b"\n")
        manifest_entries[rel_path.replace("\\", "/")] = {
            "size_bytes": len(normalized_data),
            "sha256": sha256_bytes(normalized_data),
        }
    manifest_hash = sha256_bytes(canonical_json(manifest_entries).encode("utf-8"))
    return {
        "corpus_manifest_hash": manifest_hash,
        "files": manifest_entries,
    }


def generate_synthetic_corpus(target_dir: Path) -> tuple[Path, str, dict[str, Any]]:
    """Generate synthetic repository with manifest and deterministic git commit."""
    target_dir.mkdir(parents=True, exist_ok=True)

    # Write files
    for rel_path, content in CORPUS_FILES.items():
        file_path = target_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure LF line endings for deterministic git hashing
        file_path.write_bytes(content.replace("\r\n", "\n").encode("utf-8"))

    manifest = build_manifest(target_dir)
    manifest_file = target_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Attempt git init and commit with fixed identity and timestamps
    head_sha = "0000000000000000000000000000000000000001"
    git_bin = shutil.which("git")
    if git_bin:
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = "Benchmark Author"
        env["GIT_AUTHOR_EMAIL"] = "bench@example.com"
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_NAME"] = "Benchmark Committer"
        env["GIT_COMMITTER_EMAIL"] = "bench@example.com"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"

        try:
            subprocess.run([git_bin, "init", "-q"], cwd=str(target_dir), env=env, check=True, capture_output=True)
            subprocess.run([git_bin, "add", "."], cwd=str(target_dir), env=env, check=True, capture_output=True)
            subprocess.run(
                [git_bin, "commit", "-q", "-m", "chore: synthetic benchmark baseline revision"],
                cwd=str(target_dir), env=env, check=True, capture_output=True
            )
            res = subprocess.run([git_bin, "rev-parse", "HEAD"], cwd=str(target_dir), env=env, check=True, capture_output=True, text=True)
            head_sha = res.stdout.strip()
        except Exception:
            # Fall back to manifest hash if git execution fails
            head_sha = manifest["corpus_manifest_hash"][:40]
    else:
        head_sha = manifest["corpus_manifest_hash"][:40]

    return target_dir, head_sha, manifest


def verify_corpus_manifest(root: Path) -> tuple[bool, list[str]]:
    """Verify that files on disk match the recorded manifest."""
    manifest_file = root / "manifest.json"
    if not manifest_file.exists():
        return False, ["manifest.json does not exist"]
    try:
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"failed to read manifest.json: {exc}"]

    expected_files = manifest_data.get("files", {})
    errors: list[str] = []
    for rel_path, meta in expected_files.items():
        fpath = root / rel_path
        if not fpath.exists():
            errors.append(f"missing file: {rel_path}")
            continue
        data = fpath.read_bytes().replace(b"\r\n", b"\n")
        actual_hash = sha256_bytes(data)
        if actual_hash != meta["sha256"]:
            errors.append(f"hash mismatch for {rel_path}: expected {meta['sha256']} got {actual_hash}")

    return len(errors) == 0, errors
