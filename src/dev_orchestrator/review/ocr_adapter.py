"""OpenCodeReview adapter for deterministic diff/scan preparation and rule resolution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from .models import ReviewManifest, ReviewRequest, _normalize_repo_path


def _is_path_contained(parent: Path, candidate: Path) -> bool:
    c_parent = os.path.normcase(os.path.realpath(parent))
    c_cand = os.path.normcase(os.path.realpath(candidate))
    try:
        return os.path.commonpath([c_parent, c_cand]) == c_parent
    except (ValueError, OSError):
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


class OpenCodeReviewAdapter:
    """Invokes pinned, capability-checked OCR executable or deterministic git fallback."""

    def __init__(
        self,
        repo_path: Path | str,
        executable: Optional[str] = None,
        *,
        subprocess_module: Any = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.executable = executable or "opencode-review"
        self._subprocess = subprocess_module or subprocess

    def probe_capabilities(self) -> dict[str, Any]:
        """Verify OCR executable version and machine-readable capabilities."""
        if not self.executable:
            return {"available": False, "version": None, "capabilities": []}
        try:
            res = self._subprocess.run(
                [self.executable, "--capabilities"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                **hidden_subprocess_kwargs(),
            )
            if res.returncode == 0:
                try:
                    data = json.loads(res.stdout.strip())
                    if isinstance(data, dict):
                        return {"available": True, **data}
                except Exception:
                    pass
        except Exception:
            pass

        # Try --version fallback
        try:
            res = self._subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                **hidden_subprocess_kwargs(),
            )
            if res.returncode == 0:
                ver = res.stdout.strip()
                return {
                    "available": True,
                    "version": ver,
                    "capabilities": ["diff_preview", "scan_preview", "rule_resolution"],
                }
        except Exception:
            pass

        return {
            "available": False,
            "version": None,
            "capabilities": ["git_fallback"],
        }

    def prepare_diff(
        self,
        mode: str,
        refs: dict[str, str],
        limits: dict[str, int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        """Resolve changed files and exclusions for diff mode."""
        max_files = int(limits.get("max_files", 100))
        max_bytes = int(limits.get("max_total_bytes", 1024 * 1024))

        # Try OCR preview command if executable is available
        probe = self.probe_capabilities()
        if probe.get("available") and "diff_preview" in probe.get("capabilities", []):
            argv = [
                self.executable,
                "review-preview",
                "--diff-mode",
                mode,
                "--repo",
                str(self.repo_path),
            ]
            if refs.get("base"):
                argv.extend(["--base", refs["base"]])
            if refs.get("head"):
                argv.extend(["--head", refs["head"]])
            if refs.get("commit"):
                argv.extend(["--commit", refs["commit"]])

            res = self._subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                cwd=str(self.repo_path),
                **hidden_subprocess_kwargs(),
            )
            if res.returncode != 0:
                raise RuntimeError(f"OCR review-preview failed ({res.returncode}): {res.stderr.strip()}")
            try:
                data = json.loads(res.stdout.strip())
            except Exception as exc:
                raise ValueError(f"OCR review-preview output is not valid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("OCR review-preview output must be a JSON object")

            raw_sel = data.get("selected_files", [])
            raw_ex = data.get("excluded_files", [])
            resolved_refs = dict(data.get("resolved_refs") or refs)
            # Validate containment
            selected: list[dict[str, Any]] = []
            excluded: list[dict[str, Any]] = list(raw_ex)
            for f in raw_sel:
                rel_p = _normalize_repo_path(f["path"])
                full_p = self.repo_path / rel_p
                if not _is_path_contained(self.repo_path, full_p):
                    excluded.append({"path": rel_p, "reason": "escapes_repo_containment"})
                    continue
                selected.append(f)
            return selected, excluded, resolved_refs
        elif probe.get("available"):
            raise RuntimeError(f"OCR executable {self.executable} lacks required diff_preview capability")

        # Deterministic git diff fallback
        return self._git_diff_preview(mode, refs, max_files, max_bytes)

    def _git_diff_preview(
        self,
        mode: str,
        refs: dict[str, str],
        max_files: int,
        max_bytes: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        """Deterministic repository diff inspection using git."""
        git_cmd = ["git", "diff", "--name-only"]
        if mode == "workspace":
            git_cmd = ["git", "status", "--porcelain"]
        elif mode == "range":
            base = refs.get("base", "HEAD~1")
            head = refs.get("head", "HEAD")
            git_cmd = ["git", "diff", "--name-only", f"{base}...{head}"]
        elif mode == "commit":
            c = refs.get("commit", "HEAD")
            git_cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", c]

        res = self._subprocess.run(
            git_cmd,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            shell=False,
            **hidden_subprocess_kwargs(),
        )
        if res.returncode != 0:
            raise RuntimeError(f"git diff failed ({res.returncode}): {res.stderr.strip()}")

        lines = [line.rstrip("\r\n") for line in res.stdout.splitlines() if line.strip()]
        candidate_paths: list[str] = []
        excluded: list[dict[str, Any]] = []

        if mode == "workspace":
            for line in lines:
                if len(line) < 3:
                    continue
                status_code = line[:2]
                path_str = line[3:].strip() if len(line) > 3 and line[2] == " " else line[2:].strip()
                if " -> " in path_str:
                    path_str = path_str.split(" -> ")[1].strip()
                if status_code == "??":
                    # Untracked file
                    excluded.append({"path": path_str, "reason": "untracked"})
                elif status_code.startswith("D") or status_code.endswith("D"):
                    excluded.append({"path": path_str, "reason": "deleted"})
                else:
                    candidate_paths.append(path_str)
            # In post-worker workspace review, select the HEAD commit diff in addition to
            # the working-tree delta rather than gating on a fully empty porcelain, so committed
            # changes are never skipped when untracked files or unrelated edits exist.
            head_commit = refs.get("commit") or refs.get("head") or "HEAD"
            c_cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", head_commit]
            c_res = self._subprocess.run(
                c_cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                shell=False,
                **hidden_subprocess_kwargs(),
            )
            if c_res.returncode != 0:
                raise RuntimeError(f"git diff-tree failed ({c_res.returncode}): {c_res.stderr.strip()}")
            head_paths = [l.strip() for l in c_res.stdout.splitlines() if l.strip()]
            for hp in head_paths:
                if hp not in candidate_paths:
                    candidate_paths.append(hp)
        else:
            candidate_paths = [l.strip() for l in lines if l.strip()]

        selected: list[dict[str, Any]] = []
        total_bytes = 0

        for rel_str in candidate_paths:
            try:
                norm_p = _normalize_repo_path(rel_str)
            except ValueError:
                excluded.append({"path": rel_str, "reason": "invalid_path"})
                continue

            full_p = self.repo_path / norm_p
            if not _is_path_contained(self.repo_path, full_p):
                excluded.append({"path": norm_p, "reason": "escapes_repo_containment"})
                continue

            if not full_p.is_file():
                excluded.append({"path": norm_p, "reason": "not_a_file"})
                continue

            try:
                size = full_p.stat().st_size
            except OSError:
                excluded.append({"path": norm_p, "reason": "unreadable"})
                continue

            # Check limits
            if len(selected) >= max_files:
                excluded.append({"path": norm_p, "reason": "exceeds_max_files_limit"})
                continue

            if total_bytes + size > max_bytes:
                excluded.append({"path": norm_p, "reason": "exceeds_max_bytes_limit"})
                continue

            digest = _sha256_file(full_p)
            selected.append({
                "path": norm_p,
                "size_bytes": size,
                "sha256": digest,
            })
            total_bytes += size

        if mode == "workspace":
            norm_head_paths: set[str] = set()
            for hp in head_paths:
                try:
                    norm_head_paths.add(_normalize_repo_path(hp))
                except ValueError:
                    pass
            head_selected = [s for s in selected if s["path"] in norm_head_paths]
            if not head_selected:
                raise RuntimeError(f"anchored head commit {head_commit} contributes no selected files for review")

        return selected, excluded, refs

    def prepare_scan(
        self,
        roots: list[str],
        limits: dict[str, int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Scan repository files under configured scan roots within bounds."""
        max_files = int(limits.get("max_files", 100))
        max_bytes = int(limits.get("max_total_bytes", 1024 * 1024))

        probe = self.probe_capabilities()
        if probe.get("available") and "scan_preview" in probe.get("capabilities", []):
            argv = [
                self.executable,
                "scan-preview",
                "--roots",
                ",".join(roots),
                "--repo",
                str(self.repo_path),
            ]
            res = self._subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                cwd=str(self.repo_path),
                **hidden_subprocess_kwargs(),
            )
            if res.returncode != 0:
                raise RuntimeError(f"OCR scan-preview failed ({res.returncode}): {res.stderr.strip()}")
            try:
                data = json.loads(res.stdout.strip())
            except Exception as exc:
                raise ValueError(f"OCR scan-preview output is not valid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("OCR scan-preview output must be a JSON object")

            raw_sel = data.get("selected_files", [])
            raw_ex = data.get("excluded_files", [])
            selected: list[dict[str, Any]] = []
            excluded: list[dict[str, Any]] = list(raw_ex)
            for f in raw_sel:
                rel_p = _normalize_repo_path(f["path"])
                full_p = self.repo_path / rel_p
                if not _is_path_contained(self.repo_path, full_p):
                    excluded.append({"path": rel_p, "reason": "escapes_repo_containment"})
                    continue
                selected.append(f)
            return selected, excluded
        elif probe.get("available"):
            raise RuntimeError(f"OCR executable {self.executable} lacks required scan_preview capability")

        selected: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        total_bytes = 0

        ignore_dirs = {".git", ".devorch", "runtime", "node_modules", "__pycache__", ".pytest_cache"}

        for r in roots:
            root_dir = (self.repo_path / r).resolve()
            if not _is_path_contained(self.repo_path, root_dir):
                excluded.append({"path": r, "reason": "escapes_repo_containment"})
                continue
            if not root_dir.exists():
                excluded.append({"path": r, "reason": "root_not_found"})
                continue

            # Walk directory deterministically
            for dirpath, dirnames, filenames in os.walk(root_dir):
                dirnames[:] = [d for d in sorted(dirnames) if d not in ignore_dirs]
                for fname in sorted(filenames):
                    fpath = Path(dirpath) / fname
                    if not _is_path_contained(self.repo_path, fpath):
                        continue
                    rel_p = str(fpath.relative_to(self.repo_path)).replace("\\", "/")
                    try:
                        size = fpath.stat().st_size
                    except OSError:
                        excluded.append({"path": rel_p, "reason": "unreadable"})
                        continue

                    if len(selected) >= max_files:
                        excluded.append({"path": rel_p, "reason": "exceeds_max_files_limit"})
                        continue
                    if total_bytes + size > max_bytes:
                        excluded.append({"path": rel_p, "reason": "exceeds_max_bytes_limit"})
                        continue

                    digest = _sha256_file(fpath)
                    selected.append({
                        "path": rel_p,
                        "size_bytes": size,
                        "sha256": digest,
                    })
                    total_bytes += size

        return selected, excluded

    def resolve_rules(
        self,
        rule_pack_path: Optional[str],
        selected_files: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Load and resolve applicable rules from rule pack."""
        if not rule_pack_path:
            return [], None

        rp_path = (self.repo_path / rule_pack_path).resolve()
        if not _is_path_contained(self.repo_path, rp_path) or not rp_path.is_file():
            return [], None

        try:
            raw_bytes = rp_path.read_bytes()
            rule_digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            return [], None

        rules_list: list[dict[str, Any]] = []
        if isinstance(data, dict):
            raw_rules = data.get("rules", [])
        elif isinstance(data, list):
            raw_rules = data
        else:
            raw_rules = []

        for r in raw_rules:
            if isinstance(r, dict) and r.get("rule_id"):
                rules_list.append({
                    "rule_id": str(r["rule_id"]),
                    "title": str(r.get("title", r["rule_id"])),
                    "category": str(r.get("category", "correctness")),
                    "severity": str(r.get("severity", "blocking")),
                    "description": str(r.get("description", "")),
                    "provenance": rule_pack_path,
                })

        return rules_list, rule_digest

    def build_manifest(self, request: ReviewRequest) -> ReviewManifest:
        """Construct deterministic ReviewManifest with cryptographic input_digest."""
        probe = self.probe_capabilities()
        ocr_version = probe.get("version")

        if request.mode == "diff":
            selected, excluded, resolved_refs = self.prepare_diff(
                request.diff_mode, request.diff_refs, request.file_limits
            )
        else:
            selected, excluded = self.prepare_scan(
                request.scan_roots, request.file_limits
            )
            resolved_refs = {}

        rules, rule_sha256 = self.resolve_rules(
            request.rule_pack_path, selected
        )

        manifest_data = {
            "request_id": request.request_id,
            "project_id": request.project_id,
            "task_id": request.task_id,
            "branch": request.branch,
            "head": request.head,
            "status_hash": request.status_hash,
            "ocr_version": ocr_version,
            "resolved_refs": resolved_refs,
            "selected_files": selected,
            "excluded_files": excluded,
            "rules": rules,
            "rule_pack_sha256": rule_sha256,
        }
        canonical_raw = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        input_digest = "sha256:" + hashlib.sha256(canonical_raw.encode("utf-8")).hexdigest()

        return ReviewManifest(
            manifest_id=f"manifest-{input_digest[7:23]}",
            ocr_version=ocr_version,
            resolved_refs=resolved_refs,
            selected_files=selected,
            excluded_files=excluded,
            rules=rules,
            input_digest=input_digest,
        )
