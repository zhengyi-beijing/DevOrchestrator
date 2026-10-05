# P20.4 failed-run remediation — implementation checkpoint

Scope and pre-implementation invariant declaration:
[remediation contract](P20_4_REMEDIATION_CONTRACT_20261005.md).
Original failure: [diagnosis](P20_4_FAILED_RUN_DIAGNOSIS_20261005.md).

Implemented D01–D07 within the approved P20.4 direction:

- OS boot identity replaces wall-clock boot estimates for new runs. Windows reads
  BootIdentifier via SystemBootEnvironmentInformation plus the successful-boot
  BootId counter; the GUID alone identifies a BCD entry. Linux reads kernel boot_id.
  Original supervisor PID/create-time, monotonic rollback, reboot and the existing
  >10-second wall/monotonic anomaly fences remain. Both wall and monotonic duration
  must reach six hours. FAILED runs cannot qualify. Windows structure reference:
  [PHNT native headers](https://github.com/winsiderss/phnt/blob/master/ntexapi.h).
  Successful-boot counter contract:
  [Microsoft HVCI enablement](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement).
- Static ready launch is task-scoped across changed HEAD. Pending review/unapplied
  decision fences ordinary Worker launches; existing validated remediation remains
  permitted. Current-task failed Planner/Reviewer facts stay authoritative, existing
  bounded Watchdog recovery stays visible. Ambiguous recovery_required opens an
  explicit stable gate; current recovery exhaustion/blocked
  same-task review obligations expose durable authority gates. Historical task
  failures cannot regress a successor.
- Native roadmap resolver preflight supplements LF generation/checkout protection.
  Task qualification now requires consumed native handoff or terminal task_complete
  settlement, in addition to real implementation and independent Review ACCEPT.
- Observations keep verifier-required compact fields, 8-MiB append-only segments,
  a SHA-256 sealed-segment index, at most 16 segments and 128 MiB total. No measured
  history is evicted. New collections are scoped to explicit reliability run IDs;
  ordinary unmeasured production ticks do not append. Closed runs stop appending,
  and a capacity failure gates only the measured project. Legacy history remains
  untouched. Gate identity includes task and full-reason SHA-256, with a bounded
  reason preview. All verifier/telemetry paths read every segment; missing,
  corrupt or exhausted evidence fails closed and exposes a stable authority gate
  plus degraded daemon heartbeat. Whole growing Watchdog histories are not copied
  every tick. A twelve-hour synthetic window verifies segmented coverage, not live
  duration acceptance.
- Duplicate execution telemetry includes terminal launch history, same-task launches
  from different sources, overlapping intervals and authenticated recovery/remediation
  ancestry. Missing launch history is UNKNOWN.
- Two resource failures of one provider/model prohibit blind third-account rotation
  but allow heterogeneous failover inside the existing budget. High quality and
  provider independence remain unchanged. Real account authentication/quota conditions
  still need a usable execution path before a future live run.
- Case finally cleanup covers timeout/exception/success; supervisor exit closes the
  case driver and primary normally. Permitted case daemons self-stop after parent
  terminal state/process loss. Normal stop-daemon optionally validates exact PID and
  create-time. Ownership mismatch refuses effects, and cleanup failure fails the run.

TDD source receipts are retained under runtime: original suite 10 RED, independently
reproduced static relaunch RED, failover/cleanup 2 RED; subsequent focused and relevant
regression GREEN. Original authority regression: 172 tests + 22 subtests. Recovery,
permit, binding and CPF regression: 88 tests. Daemon/control/roles/Broker/router/
Watchdog/invariant/jobs/P18/P19 regression: 384 tests + 34 subtests. Latest targeted
fixture regression: 30 tests; earlier clock/telemetry/qualification batch: 69 tests.
Counts overlap and must not be added as distinct tests. Compile and diff checks pass.

All 15 original case daemons and the primary daemon were stopped through normal,
permit/identity-checked API cleanup after confirming no active AI roles. Private
backup: runtime/backup-p20-failed-owned-cleanup-20261005/. Original run SHA-256 is
unchanged. Task/role/decision evidence is preserved; normal daemon updated_at can
advance before shutdown. Canonical daemon and Broker were untouched during cleanup.

Independent Technical Review R1 returned CHANGES_REQUIRED (three blockers).
The bounded delta above and Windows boot counter regression were implemented;
six negative tests reproduced the defects before the fix. R2 independently
ACCEPTED the exact hashes of 20 source/test files through the normal Broker
(anthropic / claude-opus-5). The Reviewer independently ran 69 focused tests plus
the native checkout test; all 70 passed. Root's final delta suite: 167 passed;
authority/daemon regression: 69 passed and 18 subtests. Counts overlap.
Source-correlated receipts and remaining NON_BLOCKING notes:
[remediation receipts](evidence/P20_4_REMEDIATION_RECEIPTS_20261005.json).
Canonical daemon reload is pending at this source-commit checkpoint. The reload
must preserve the original gate and failed run, confirm quiescence, back up ledgers,
and observe fresh normal ticks with all six invariants holding.
No final P20.4 acceptance,
replacement soak, canonical gate clearing, P20.5 activation, P21, push or history rewrite.
