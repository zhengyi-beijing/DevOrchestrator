"""Zvec-grep adapter, capability probing, and query validation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .contracts import (
    RetrievalHit,
    ZvecProbeResult,
    sha256_bytes,
)


def _is_safe_subpath(base: Path, candidate: Path) -> bool:
    try:
        resolved_base = base.resolve()
        resolved_candidate = candidate.resolve()
        return resolved_candidate == resolved_base or resolved_base in resolved_candidate.parents
    except Exception:
        return False


class ZvecAdapter:
    """Benchmark-owned wrapper for zvec-grep retrieval tooling."""

    def __init__(
        self,
        executable_path: str | Path | None = None,
        firewall_rule_name: str = "DevOrchestrator-AIBench-OutboundDeny",
    ) -> None:
        self.executable_path = Path(executable_path) if executable_path else None
        self.firewall_rule_name = firewall_rule_name
        self._cached_probe: ZvecProbeResult | None = None

    def _find_executable(self) -> Path | None:
        if self.executable_path and self.executable_path.is_file():
            return self.executable_path
        # Look on PATH
        for name in ("zvec-grep.exe", "zvec-grep", "zvec.exe", "zvec"):
            found = shutil.which(name)
            if found:
                p = Path(found)
                if p.is_file():
                    return p
        return None

    def probe(self, force_refresh: bool = False) -> ZvecProbeResult:
        """Probe capability, version, executable hash, and firewall status."""
        if self._cached_probe is not None and not force_refresh:
            return self._cached_probe

        exe = self._find_executable()
        if not exe:
            res = ZvecProbeResult(
                supported=False,
                executable_path=None,
                version=None,
                executable_hash=None,
                local_only_verified=False,
                error_message="zvec executable not found on system or PATH",
            )
            self._cached_probe = res
            return res

        try:
            exe_bytes = exe.read_bytes()
            exe_hash = sha256_bytes(exe_bytes)
        except Exception as exc:
            res = ZvecProbeResult(
                supported=False,
                executable_path=str(exe),
                version=None,
                executable_hash=None,
                local_only_verified=False,
                error_message=f"failed to read executable bytes: {exc}",
            )
            self._cached_probe = res
            return res

        try:
            proc = subprocess.run(
                [str(exe), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10.0,
            )
            if proc.returncode != 0:
                res = ZvecProbeResult(
                    supported=False,
                    executable_path=str(exe),
                    version=None,
                    executable_hash=exe_hash,
                    local_only_verified=False,
                    error_message=f"executable --version returned exit code {proc.returncode}: {proc.stderr.strip()}",
                )
                self._cached_probe = res
                return res
            version_str = proc.stdout.strip()
        except Exception as exc:
            res = ZvecProbeResult(
                supported=False,
                executable_path=str(exe),
                version=None,
                executable_hash=exe_hash,
                local_only_verified=False,
                error_message=f"failed to execute version probe: {exc}",
            )
            self._cached_probe = res
            return res

        local_only = self.verify_network_denial()

        res = ZvecProbeResult(
            supported=True,
            executable_path=str(exe),
            version=version_str,
            executable_hash=exe_hash,
            local_only_verified=local_only,
            error_message=None,
        )
        self._cached_probe = res
        return res

    def verify_network_denial(self) -> bool:
        """Verify that an outbound-deny rule is active for zvec."""
        # Query Windows Firewall via netsh
        cmd = ["netsh", "advfirewall", "firewall", "show", "rule", f"name={self.firewall_rule_name}"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10.0,
            )
            if proc.returncode == 0:
                out = proc.stdout.lower()
                # Check for Action: Block and Direction: Out
                return ("block" in out or "deny" in out) and "out" in out
        except Exception:
            pass
        return False

    def build_index(self, repo_root: Path, output_dir: Path) -> dict[str, Any]:
        """Build index from pristine repo_root into output_dir."""
        probe = self.probe()
        if not probe.supported or not probe.executable_path:
            raise RuntimeError(f"cannot build index: zvec unsupported ({probe.error_message})")

        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            probe.executable_path,
            "index",
            "--repo", str(repo_root),
            "--output", str(output_dir),
        ]
        start_time = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120.0,
        )
        duration = time.monotonic() - start_time

        if proc.returncode != 0:
            raise RuntimeError(f"zvec index failed with code {proc.returncode}: {proc.stderr.strip()}")

        size_bytes = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
        return {
            "status": "ok",
            "index_time_seconds": round(duration, 3),
            "index_size_bytes": size_bytes,
        }

    def query(self, index_dir: Path, repo_root: Path, query_str: str, top_k: int = 10) -> list[RetrievalHit]:
        """Query index and return validated RetrievalHits strictly within repo_root."""
        probe = self.probe()
        if not probe.supported or not probe.executable_path:
            raise RuntimeError(f"cannot query index: zvec unsupported ({probe.error_message})")

        cmd = [
            probe.executable_path,
            "query",
            "--index", str(index_dir),
            "--query", query_str,
            "--top-k", str(top_k),
            "--format", "json",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30.0,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"zvec query failed with code {proc.returncode}: {proc.stderr.strip()}")

        try:
            raw_hits = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"zvec returned invalid JSON: {exc}") from exc

        if not isinstance(raw_hits, list):
            raise ValueError("zvec query output must be a JSON array")

        validated_hits: list[RetrievalHit] = []
        for hit in raw_hits:
            if not isinstance(hit, dict):
                continue
            path_str = str(hit.get("path", "")).strip()
            if not path_str:
                continue

            target_path = repo_root / path_str
            # Enforce path containment within repo_root
            if not _is_safe_subpath(repo_root, target_path):
                # Reject path traversal attack
                continue

            try:
                start_l = int(hit.get("start_line", 1))
                end_l = int(hit.get("end_line", start_l))
                score_val = float(hit.get("score", 1.0))
            except (ValueError, TypeError):
                continue

            if start_l < 1 or end_l < start_l:
                continue

            validated_hits.append(
                RetrievalHit(
                    path=path_str.replace("\\", "/"),
                    start_line=start_l,
                    end_line=end_l,
                    score=score_val,
                    mode="zvec",
                )
            )

        return validated_hits

    def stats(self, index_dir: Path) -> dict[str, Any]:
        """Return index size and file count."""
        if not index_dir.exists():
            return {"exists": False, "size_bytes": 0, "file_count": 0}
        files = [f for f in index_dir.rglob("*") if f.is_file()]
        total_size = sum(f.stat().st_size for f in files)
        return {
            "exists": True,
            "size_bytes": total_size,
            "file_count": len(files),
        }
