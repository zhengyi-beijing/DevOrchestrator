# CCP7 Live Browser Acceptance

Status: **ACCEPTED 鈥?2026-09-10**

Scope: live software-only acceptance of the ChatGPT Web browser transport. No
repository workflow action, physical detector, motion device, or X-ray hardware
was actuated by the acceptance requests.

## Accepted implementation

The deployed userscript release candidate is `0.1.10`.

Key release changes:
- support the current `#composer-submit-button` while rejecting Stop controls;
- wait up to 3 seconds for ChatGPT to make the send button ready after inserting
  the prompt instead of repeatedly rewriting an unsent draft;
- retain DOM-authoritative submission confirmation, lease renewal, reclaim, and
  fail-closed conflict behavior;
- remove temporary response-DOM diagnostics before release so assistant content
  is not persisted in diagnostic localStorage keys.

## Live carrier

Primary acceptance binding:
`6aa1784a-c160-83eb-b72b-662858373d69`.

The acceptance Bridge was isolated from the production daemon and used only
synthetic CCP7 `stop` decisions with zero repository side effects.
## Live evidence

### Single-tab baseline

Request `ccp7-live-browser4-20260909` completed:
`pending -> claimed -> responded`.
The Bridge stored the exact required response marker and structured JSON.

### Multi-tab

Request `ccp7-live-browser5-20260909` was exercised with two tabs on the same
binding. Both tabs displayed the same request and the same response; no second
request turn was present. The Bridge completed one authoritative response.

### Restart / recovery

Request `ccp7-live-browser6-20260909` was claimed, then the isolated Bridge was
stopped. The Bridge was restarted from the same runtime without reset or
resubmission. The original request id, nonce, and prompt survived and the
request eventually reached `responded`.

### Conflict / fail-closed

During `ccp7-live-browser7-20260909`:
- competing second claim returned HTTP 204;
- renew with a bad token returned HTTP 409;
- response with a bad token returned HTTP 409;
- renew with the wrong nonce returned HTTP 409.

The legitimate claimant still completed `responded` after these probes.
## Regression evidence

Final release-candidate verification after removing diagnostics:
- CCP7 targeted regression: 21/21 passed;
- full Python regression: 139/139 passed;
- `node --check browser/chatgpt-web-adapter.user.js`: passed;
- `git diff --check`: passed.

Tampermonkey fresh Installed Userscripts view confirmed version `0.1.10`.
A fresh post-update tab on binding `6a9f6f29-9728-83eb-b165-72b9d158aa4a`
created Bridge presence at `2026-09-09T22:29:32.246597+00:00` without any
pending request, proving release injection and transport without sending a test
prompt.

## Fresh technical review

One blocking MEDIUM was found before release: temporary live DOM diagnostics
persisted assistant text into localStorage. It was removed in `0.1.10` and all
tests were rerun afterward.

Final review result: **ACCEPTED**. No unresolved HIGH or blocking MEDIUM issue
remains in CCP7 scope.

## Handoff

CCP7 is complete and ready for a scoped commit. Keep unrelated AGY project
isolation changes out of the CCP7 commit. The current daemon model owns
8765 (Bridge) and 8770 (Web); the legacy standalone 8766 control listener is
not part of the current CLI/runtime contract.


## Post-acceptance runtime restoration

The isolated acceptance Bridge was stopped and the production daemon was
restored through the current `start-daemon` CLI.

Final runtime health:
- daemon PID `32344`, state `running`, `last_error=null`;
- Web `127.0.0.1:8770` returned HTTP 200;
- Bridge `127.0.0.1:8765` returned HTTP 204 for an unqueued health-check binding;
- current CLI no longer exposes the legacy standalone 8766 control listener.
