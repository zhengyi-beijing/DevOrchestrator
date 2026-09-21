"""In-memory cache implementation with LRU eviction.

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
