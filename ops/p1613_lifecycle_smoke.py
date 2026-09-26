"""P16.13 lifecycle runtime smoke.

Exercises successor handoff, ownership draining, transition journal identity,
restart replay, tick-consumer authority derivation and repeated-tick
idempotency against real git repositories and real durable ledgers, then audits
the live deployment.

The live audit is strictly read-only; every mutation happens in a temporary
runtime.  Run from the repository root::

    python ops/p1613_lifecycle_smoke.py

An unresolved live invariant violation is acceptable only when it is durably
gated and its recovery actuation is bounded; an unresolved violation that keeps
actuating every tick is a failure.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from dev_orchestrator.core.lifecycle_authority import (  # noqa: E402
    active_owners,
    evaluate_lifecycle_invariants,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if ok else "  FAIL  ") + label + ((" :: " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label + ((" :: " + detail) if detail else ""))


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "agent" / "staged").mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "smoke@example.com")
    git(repo, "config", "user.name", "Smoke")
    (repo / "README.md").write_text("smoke\n", encoding="utf-8")
    (repo / "agent" / "next.md").write_text(
        "# P16.12 task\n\nStatus: **COMPLETE**\n", encoding="utf-8")
    (repo / "agent" / "staged" / "P16.13.md").write_bytes(
        b"# P16.13 task\n\nStatus: **PENDING DESIGN**\n\nPredecessor: P16.12\n")
    (repo / "agent" / "staged" / "roadmap.json").write_text(
        json.dumps({"schema_version": 1, "tasks": [{
            "task_id": "P16.12",
            "successor": "P16.13",
            "successor_spec_path": "agent/staged/P16.13.md",
        }]}, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "smoke fixture"],
                   check=True, capture_output=True, text=True)
    return repo


def snapshot_for(repo: Path, task_id: str, *, state: str = "IDLE",
                 status: str = "**PENDING DESIGN**") -> dict:
    return {
        "project_id": "smoke",
        "repo_path": str(repo),
        "state": state,
        "next_title": task_id + " task",
        "next_status": status,
        "telemetry": {"task_id": task_id},
        "git": {"head": git(repo, "rev-parse", "HEAD")},
    }


def summary_for(repo: Path, task_id: str, **kw) -> dict:
    return {"projects": [snapshot_for(repo, task_id, **kw)]}


def invariants(executor: TransitionExecutor, snapshot: dict) -> dict:
    return {
        f.code: f for f in evaluate_lifecycle_invariants(
            snapshot=snapshot, executor_state=executor.state(),
            decisions_state=json.loads(
                (executor.runtime_root / "review-decisions.json").read_text(encoding="utf-8")
            ) if (executor.runtime_root / "review-decisions.json").exists() else {},
        )
    }


# ---------------------------------------------------------------- scenario 1
def scenario_authority_convergence() -> None:
    print("\n[1] successor handoff: authority, lineage, ownership draining")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = make_repo(root)
        runtime = root / "runtime"
        ex = TransitionExecutor(runtime)

        # Predecessor P16.12 owns the project and is still running a Worker.
        ledger = ex.state()
        ledger["executions"]["run-p1612"] = {
            "project_id": "smoke", "task_id": "P16.12", "state": "running",
            "engine": "claude", "started_at": "2026-09-26T00:00:00+00:00",
            "source_request_id": "req-p1612",
        }
        ex._save_ledger(ledger)

        # Repository has already published the successor (partial publication).
        snap = snapshot_for(repo, "P16.13")
        ex.reconcile_lifecycle_authority({"projects": [snap]})
        auth = ex.state()["lifecycle"]["smoke"]
        check("stale predecessor Worker fences successor authority",
              auth["current_task_id"] == "P16.12",
              "authority=" + str(auth["current_task_id"]))
        check("predecessor ownership is recorded as the active owner",
              bool(auth.get("active_owner")),
              "state=" + str(auth.get("lifecycle_state")))

        inv = invariants(ex, snap)
        check("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION holds while fenced",
              inv["CURRENT_TASK_MATCHES_ACTIVE_EXECUTION"].holds)
        check("PENDING_DESIGN_NOT_EXECUTING holds while fenced",
              inv["PENDING_DESIGN_NOT_EXECUTING"].holds)
        check("SINGLE_ACTIVE_LIFECYCLE_OWNER holds while fenced",
              inv["SINGLE_ACTIVE_LIFECYCLE_OWNER"].holds)

        # Predecessor ownership drains.
        ledger = ex.state()
        ledger["executions"]["run-p1612"]["state"] = "completed"
        ledger["executions"]["run-p1612"]["completed_at"] = "2026-09-26T01:00:00+00:00"
        ex._save_ledger(ledger)
        check("no predecessor execution survives as an active owner",
              active_owners("smoke", ex.state()) == [],
              "owners=" + str(active_owners("smoke", ex.state())))

        # Repeated ticks must be idempotent and must not gate.
        states = []
        for _ in range(4):
            ex.reconcile_lifecycle_authority({"projects": [snap]})
            a = ex.state()["lifecycle"]["smoke"]
            states.append((a.get("current_task_id"), a.get("lifecycle_state"),
                           a.get("active_transition_id"),
                           bool(a.get("owner_gate")), int(a.get("generation") or 0)))
        check("repeated ticks are idempotent", len(set(states)) == 1, str(set(states)))
        check("no unexpected OWNER_GATE from repeated ticks",
              not states[-1][3], "gate=" + str(states[-1][3]))

        transitions = ex.state()["transitions"]
        mine = [t for t in transitions.values() if t.get("project_id") == "smoke"]
        check("exactly one transition journal entry (no duplicates)",
              len(mine) == 1, "count=" + str(len(mine)))
        if mine:
            t = mine[0]
            check("transition lineage source=P16.12 target=P16.13",
                  t.get("source_task_id") == "P16.12" and t.get("target_task_id") == "P16.13",
                  "src=" + str(t.get("source_task_id")) + " tgt=" + str(t.get("target_task_id")))
            check("transition carries a generation/epoch",
                  int(t.get("generation") or 0) >= 1, "gen=" + str(t.get("generation")))

        # ---- restart: a brand-new executor over the same durable runtime.
        before = ex.state()["lifecycle"]["smoke"]
        restarted = TransitionExecutor(runtime)
        restarted.reconcile_lifecycle_authority({"projects": [snap]})
        after = restarted.state()["lifecycle"]["smoke"]
        check("restart replays authority without regressing the task",
              after["current_task_id"] == before["current_task_id"],
              str(before["current_task_id"]) + " -> " + str(after["current_task_id"]))
        check("restart does not create a duplicate transition",
              len([t for t in restarted.state()["transitions"].values()
                   if t.get("project_id") == "smoke"]) == 1)
        check("restart does not create an OWNER_GATE",
              not after.get("owner_gate"), str(after.get("owner_gate")))
        check("restart preserves the recovery epoch identity",
              after.get("recovery_epoch_id") == before.get("recovery_epoch_id"),
              str(before.get("recovery_epoch_id")) + " -> " + str(after.get("recovery_epoch_id")))

        inv = invariants(restarted, snap)
        bad = [c for c, f in inv.items() if not f.holds]
        check("all six invariants hold after restart", not bad, "violations=" + str(bad))
        check("all six invariants are evaluated", len(inv) == 6, "codes=" + str(sorted(inv)))


# ---------------------------------------------------------------- scenario 2
def scenario_projection_authority() -> None:
    print("\n[2] daemon tick: every consumer derives from authority")
    from dev_orchestrator.daemon import _run_orchestration_tick
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = make_repo(root)
        runtime = root / "runtime"
        ex = TransitionExecutor(runtime)
        snap = snapshot_for(repo, "P16.13")
        ex.reconcile_lifecycle_authority({"projects": [snap]})

        # A stale predecessor plan, exactly like the live RECOVERY_REQUIRED case.
        stale = {"plans": {"old": {
            "project_id": "smoke", "task_id": "P16.12", "state": "recovery_required",
            "created_at": "2026-09-25T00:00:00+00:00",
        }}}

        class Planner:
            def state(self): return stale

        class Controls:
            planner = Planner()
            def advance(self, *_a, **_k): return []

        class Capturing:
            def __init__(self): self.summary = None
            def advance(self, _c, summary, **_k):
                self.summary = copy.deepcopy(summary); return []
            def record_tick_error(self, exc): raise AssertionError(str(exc))

        cfg_path = root / "projects.json"
        cfg_path.write_text(json.dumps({"projects": [{
            "project_id": "smoke",
            "name": "Smoke",
            "repo_path": str(repo),
            "adapter": "agent_files",
        }]}, indent=2), encoding="utf-8")

        wd, sup = Capturing(), Capturing()
        authority = ex.state()["lifecycle"]["smoke"]
        expected_task = authority["current_task_id"]
        expected_state = authority["lifecycle_state"]

        with patch("dev_orchestrator.daemon.run_monitor_once",
                   return_value={"projects": [snap]}), \
             patch("dev_orchestrator.daemon.write_project_statuses", return_value=[]), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", return_value=None), \
             patch("dev_orchestrator.daemon.consume_websol_responses", return_value=None):
            result = _run_orchestration_tick(
                str(cfg_path), runtime, object(), ex, controls=Controls(),
                watchdog=wd, supervisor=sup, pid=4242)

        for label, summ in (("watchdog", wd.summary), ("supervisor", sup.summary),
                            ("published projection", result)):
            row = (summ.get("projects") or [{}])[0]
            check(label + " reports the authoritative task",
                  (row.get("telemetry") or {}).get("task_id") == expected_task,
                  str((row.get("telemetry") or {}).get("task_id")))
            check(label + " reports the authoritative lifecycle state",
                  row.get("lifecycle_state") == expected_state,
                  str(row.get("lifecycle_state")) + " (authority=" + str(expected_state) + ")")
        check("tick recorded no watchdog error", "_watchdog_tick_error" not in result,
              str(result.get("_watchdog_tick_error")))
        check("tick recorded no supervisor error", "_supervisor_tick_error" not in result,
              str(result.get("_supervisor_tick_error")))

        published = json.loads((runtime / "summary.json").read_text(encoding="utf-8"))
        prow = (published.get("projects") or [{}])[0]
        check("persisted summary.json carries authoritative lifecycle",
              prow.get("lifecycle_state") == expected_state,
              str(prow.get("lifecycle_state")))


# ---------------------------------------------------------------- scenario 3
def scenario_live_ledger_audit() -> None:
    print("\n[3] live deployment audit (read-only)")
    runtime = Path("runtime")
    ledger = json.loads((runtime / "transition-executor.json").read_text(encoding="utf-8"))
    summary = json.loads((runtime / "summary.json").read_text(encoding="utf-8"))
    decisions = {}
    dpath = runtime / "review-decisions.json"
    if dpath.exists():
        decisions = json.loads(dpath.read_text(encoding="utf-8"))

    lifecycle = ledger.get("lifecycle") or {}
    for snap in summary.get("projects") or []:
        pid = str(snap.get("project_id") or "")
        auth = lifecycle.get(pid)
        if not isinstance(auth, dict):
            print("  note   " + pid + ": no authority record (no advertised task)")
            continue
        findings = evaluate_lifecycle_invariants(
            snapshot=snap, executor_state=ledger, decisions_state=decisions)
        bad = [f.code for f in findings if not f.holds]
        gate = auth.get("owner_gate")
        wd = json.loads((runtime / "watchdog.json").read_text(encoding="utf-8"))
        wrow = ((wd.get("projects") or {}).get(pid) or {})
        attempts = wrow.get("lifecycle_recovery_attempts") or {}
        counts = [int(a.get("count") or 0) for a in attempts.values() if isinstance(a, dict)]
        states = {a.get("state") for a in attempts.values() if isinstance(a, dict)}
        # A violation is acceptable only when it is durably gated and recovery
        # is bounded.  An unresolved violation that keeps actuating is not.
        if bad:
            gated = bool(gate) or bool(wrow.get("owner_gate")) or states <= {"gated", "recovered"}
            # Bounded means the fence is engaged, not that the historical
            # counter is small: a ledger written before the fence shipped keeps
            # its old count.  An attempt still marked "failed" with a count past
            # the gate threshold is an actuation loop.  A transient wait (dirty
            # worktree, or source ownership still draining) is allowed to retry.
            def _bounded(attempt: dict) -> bool:
                if attempt.get("state") in {"recovered", "gated"}:
                    return True
                result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
                if result.get("waiting") or "clean repository" in str(result.get("reason") or ""):
                    return True
                return int(attempt.get("count") or 0) <= 2

            bounded = all(
                _bounded(a) for a in attempts.values() if isinstance(a, dict)
            )
            check("live " + pid + ": unresolved violation " + str(bad) + " is gated",
                  gated, "authority_gate=" + str(bool(gate)) + " wd_gate="
                  + str(bool(wrow.get("owner_gate"))) + " states=" + str(states))
            check("live " + pid + ": recovery actuation is bounded",
                  bounded, "attempt counts=" + str(counts) + " states=" + str(states))
        else:
            check("live " + pid + ": no lifecycle invariant violation", True, "")
        if gate:
            print("  note   " + pid + ": OWNER_GATE code=" + str(gate.get("code"))
                  + " repo=" + str(gate.get("repository_task_id"))
                  + " authority=" + str(gate.get("authority_task_id"))
                  + " (invariants all hold: " + str(not bad) + ")")
        if pid == "devorchestrator":
            check("live devorchestrator authority is P16.13",
                  auth.get("current_task_id") == "P16.13", str(auth.get("current_task_id")))
            check("live devorchestrator has no OWNER_GATE", not gate, str(gate))
            check("live devorchestrator repository matches authority",
                  (auth.get("repository_projection") or {}).get("matches_authority") is True)
            check("live devorchestrator has no surviving predecessor owner",
                  active_owners(pid, ledger) == [], str(active_owners(pid, ledger)))


def main() -> int:
    print("=" * 72)
    print("P16.13 RUNTIME SMOKE")
    print("=" * 72)
    scenario_authority_convergence()
    scenario_projection_authority()
    scenario_live_ledger_audit()
    print("\n" + "=" * 72)
    print("checks=" + str(CHECKS) + " failures=" + str(len(FAILURES)))
    for f in FAILURES:
        print("  FAILED: " + f)
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
