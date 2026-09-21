"""Staleness and operating-cost probe evaluation for zvec retrieval."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .contracts import (
    StalenessProbeResult,
    utc_now_iso,
)
from .zvec import ZvecAdapter


def run_staleness_probe(
    corpus_root: Path,
    zvec_adapter: ZvecAdapter,
    scratch_dir: Path,
) -> StalenessProbeResult:
    """Evaluate retrieval staleness when files are modified without re-indexing."""
    probe = zvec_adapter.probe()
    if not probe.supported:
        return StalenessProbeResult(
            probe_id="probe_unsupported",
            latency_ms=0.0,
            index_time_seconds=0.0,
            index_size_bytes=0,
            stale_false_hits=0,
            stale_misses=0,
            stale_error_rate=0.0,
        )

    probe_work_dir = scratch_dir / "staleness_probe_work"
    probe_index_dir = scratch_dir / "staleness_probe_index"

    if probe_work_dir.exists():
        shutil.rmtree(probe_work_dir, ignore_errors=True)
    if probe_index_dir.exists():
        shutil.rmtree(probe_index_dir, ignore_errors=True)

    probe_work_dir.mkdir(parents=True, exist_ok=True)
    probe_index_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy pristine files
    for src_file in corpus_root.rglob("*"):
        if src_file.is_file() and ".git" not in src_file.parts:
            rel = src_file.relative_to(corpus_root)
            dest = probe_work_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)

    # 2. Build index on pristine copy
    index_res = zvec_adapter.build_index(probe_work_dir, probe_index_dir)
    index_time = float(index_res.get("index_time_seconds", 0.0))
    index_size = int(index_res.get("index_size_bytes", 0))

    # 3. Mutate repository symbols without refreshing the index
    # Mutation A: Delete 'issue_token' from auth.py
    auth_file = probe_work_dir / "src" / "synth_app" / "core" / "auth.py"
    if auth_file.is_file():
        auth_text = auth_file.read_text(encoding="utf-8")
        auth_mutated = auth_text.replace("def issue_token", "def _deleted_token_func")
        auth_file.write_text(auth_mutated, encoding="utf-8")

    # Mutation B: Rename 'LRUCache' in cache.py
    cache_file = probe_work_dir / "src" / "synth_app" / "core" / "cache.py"
    if cache_file.is_file():
        cache_text = cache_file.read_text(encoding="utf-8")
        cache_mutated = cache_text.replace("class LRUCache:", "class RenamedCacheStore:")
        cache_file.write_text(cache_mutated, encoding="utf-8")

    # Mutation C: Add new symbol in api.py
    api_file = probe_work_dir / "src" / "synth_app" / "handlers" / "api.py"
    if api_file.is_file():
        api_text = api_file.read_text(encoding="utf-8")
        api_mutated = api_text + "\ndef brand_new_unindexed_symbol():\n    return 'fresh'\n"
        api_file.write_text(api_mutated, encoding="utf-8")

    # 4. Measure query latencies and detect stale hits
    queries = [
        ("issue_token", "deleted"),
        ("LRUCache", "renamed"),
        ("brand_new_unindexed_symbol", "added"),
    ]

    total_latency_ms = 0.0
    stale_false_hits = 0
    stale_misses = 0

    for query_str, q_type in queries:
        t0 = time.monotonic()
        try:
            hits = zvec_adapter.query(probe_index_dir, probe_work_dir, query_str, top_k=5)
        except Exception:
            hits = []
        latency_ms = (time.monotonic() - t0) * 1000.0
        total_latency_ms += latency_ms

        if q_type == "deleted":
            # If query for deleted symbol returns hit on auth.py, verify if symbol actually exists in file
            for hit in hits:
                target = probe_work_dir / hit.path
                if target.is_file():
                    content = target.read_text(encoding="utf-8")
                    if "def issue_token" not in content:
                        stale_false_hits += 1
                        break
        elif q_type == "renamed":
            # If query for old name returns hit on cache.py, verify if old name exists in file
            for hit in hits:
                target = probe_work_dir / hit.path
                if target.is_file():
                    content = target.read_text(encoding="utf-8")
                    if "class LRUCache:" not in content:
                        stale_false_hits += 1
                        break
        elif q_type == "added":
            # Newly added symbol should be missed by stale index
            if not hits:
                stale_misses += 1

    avg_latency = round(total_latency_ms / len(queries), 2)
    total_stale_errors = stale_false_hits + stale_misses
    error_rate = round(total_stale_errors / len(queries), 4)

    # Cleanup probe directories
    shutil.rmtree(probe_work_dir, ignore_errors=True)
    shutil.rmtree(probe_index_dir, ignore_errors=True)

    return StalenessProbeResult(
        probe_id=f"probe_{int(time.time())}",
        latency_ms=avg_latency,
        index_time_seconds=index_time,
        index_size_bytes=index_size,
        stale_false_hits=stale_false_hits,
        stale_misses=stale_misses,
        stale_error_rate=error_rate,
    )
