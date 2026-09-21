"""Transactional containment provisioning and rollback for Windows ACLs and Firewall."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .contracts import (
    ContainmentConfig,
    canonical_json,
    sha256_bytes,
    utc_now_iso,
)


def get_default_state_dir() -> Path:
    override = os.environ.get("AIBENCH_CONTAINMENT_STATE_DIR")
    if override:
        return Path(override)
    program_data = os.environ.get("ProgramData", "C:\\ProgramData")
    return Path(program_data) / "DevOrchestrator" / "P16"


class ContainmentProvisioner:
    """Manages transactional ACL deny ACEs and Windows Firewall rules."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir if state_dir is not None else get_default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.journal_file = self.state_dir / "journal.json"
        self.state_file = self.state_dir / "installed_state.json"

    def _write_journal(self, entries: list[dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.state_dir / "journal.tmp"
        temp_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        temp_file.replace(self.journal_file)

    def _read_journal(self) -> list[dict[str, Any]]:
        if not self.journal_file.is_file():
            return []
        try:
            return json.loads(self.journal_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def rollback_incomplete(self) -> list[str]:
        """Roll back any incomplete mutations recorded in an uncompleted journal."""
        journal = self._read_journal()
        if not journal:
            return []
        actions_rolled_back: list[str] = []
        for entry in reversed(journal):
            if entry.get("status") == "completed":
                continue
            op = entry.get("op")
            if op == "create_firewall_rule":
                rule_name = entry.get("rule_name")
                if rule_name:
                    subprocess.run(
                        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule_name}"],
                        capture_output=True,
                        check=False,
                    )
                    actions_rolled_back.append(f"deleted firewall rule {rule_name}")
            elif op == "apply_deny_ace":
                target = entry.get("target")
                sid = entry.get("sid")
                if target and sid:
                    subprocess.run(
                        ["icacls", target, "/remove:d", sid],
                        capture_output=True,
                        check=False,
                    )
                    actions_rolled_back.append(f"removed deny ace on {target}")
        if self.journal_file.exists():
            self.journal_file.unlink()
        return actions_rolled_back

    def install(self, config: ContainmentConfig, dry_run: bool = False) -> dict[str, Any]:
        """Apply containment ACLs and firewall rule under a transaction journal."""
        self.rollback_incomplete()

        journal: list[dict[str, Any]] = []
        installed_roots: list[str] = []
        sddl_backups: dict[str, str] = {}

        # Step 1: Backup SDDL and record journal intent
        for root_str in config.protected_roots:
            p = Path(root_str)
            if not p.exists():
                continue
            backup_file = self.state_dir / f"sddl_{p.name}_{sha256_bytes(root_str.encode())[:8]}.acl"
            if not dry_run:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run(
                    ["icacls", str(p), "/save", str(backup_file)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    sddl_backups[root_str] = str(backup_file)

            journal.append({
                "op": "apply_deny_ace",
                "target": str(p),
                "sid": config.dedicated_sid,
                "status": "pending",
            })

        if config.firewall_rule_name and config.zvec_path:
            journal.append({
                "op": "create_firewall_rule",
                "rule_name": config.firewall_rule_name,
                "program": config.zvec_path,
                "status": "pending",
            })

        if not dry_run:
            self._write_journal(journal)

        # Step 2: Apply mutations
        for entry in journal:
            if dry_run:
                continue
            if entry["op"] == "apply_deny_ace":
                target = entry["target"]
                sid = entry["sid"]
                if sid:
                    # icacls <target> /deny <sid>:(OI)(CI)(W,D)
                    proc = subprocess.run(
                        ["icacls", target, "/deny", f"{sid}:(OI)(CI)(W,D)"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    entry["status"] = "applied" if proc.returncode == 0 else "failed"
                    installed_roots.append(target)
            elif entry["op"] == "create_firewall_rule":
                r_name = entry["rule_name"]
                prog = entry["program"]
                proc = subprocess.run(
                    [
                        "netsh", "advfirewall", "firewall", "add", "rule",
                        f"name={r_name}",
                        "dir=out",
                        "action=block",
                        f"program={prog}",
                        "enable=yes",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                entry["status"] = "applied" if proc.returncode == 0 else "failed"

        state_data = {
            "installed_at": utc_now_iso(),
            "config": config.to_dict(),
            "sddl_backups": sddl_backups,
            "installed_roots": installed_roots,
            "firewall_rule": config.firewall_rule_name,
        }
        state_digest = sha256_bytes(canonical_json(state_data).encode("utf-8"))
        state_data["state_digest"] = state_digest

        if not dry_run:
            self.state_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
            # Mark journal complete
            for entry in journal:
                entry["status"] = "completed"
            self._write_journal(journal)

        return state_data

    def uninstall(self, dry_run: bool = False) -> dict[str, Any]:
        """Restore original SDDL and remove firewall rule if state digest matches."""
        if not self.state_file.is_file():
            return {"status": "no_state_file"}

        state_data = json.loads(self.state_file.read_text(encoding="utf-8"))
        expected_digest = state_data.get("state_digest")

        # Verify state digest integrity
        check_copy = dict(state_data)
        check_copy.pop("state_digest", None)
        actual_digest = sha256_bytes(canonical_json(check_copy).encode("utf-8"))
        if expected_digest and actual_digest != expected_digest:
            raise RuntimeError("state file digest mismatch; stopping uninstall for owner review")

        removed_rules: list[str] = []
        restored_roots: list[str] = []

        # 1. Remove firewall rule
        fw_rule = state_data.get("firewall_rule")
        if fw_rule and not dry_run:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={fw_rule}"],
                capture_output=True,
                check=False,
            )
            removed_rules.append(fw_rule)

        # 2. Restore SDDL / remove deny ACEs
        sid = state_data.get("config", {}).get("dedicated_sid")
        for root_str in state_data.get("installed_roots", []):
            if dry_run:
                continue
            if sid:
                subprocess.run(
                    ["icacls", root_str, "/remove:d", sid],
                    capture_output=True,
                    check=False,
                )
            restored_roots.append(root_str)

        if not dry_run:
            if self.state_file.exists():
                self.state_file.unlink()
            if self.journal_file.exists():
                self.journal_file.unlink()

        return {
            "status": "uninstalled",
            "removed_rules": removed_rules,
            "restored_roots": restored_roots,
        }
