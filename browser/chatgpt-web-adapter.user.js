// ==UserScript==
// @name         DevOrchestrator ChatGPT Web binding adapter
// @namespace    devorchestrator
// @version      0.1.10
// @description  Dumb ChatGPT Web adapter for the DevOrchestrator browser bridge. Derives the binding id from the current /c/<conversation-id> URL, claims only that binding, submits the rendered prompt, waits for a stable matching response marker, and posts the raw assistant text back.
// @author       DevOrchestrator
// @match        https://chatgpt.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
//
// Transport only. This script performs DOM adaptation and transport
// acknowledgement: it never decides what happens next in a project workflow,
// never starts Workers, and never interprets repository or Worker state.
// The binding id is always derived from the live ChatGPT conversation URL, so
// no project/conversation key is hard-coded here and each tab (conversation)
// claims only its own project queue. Assistant responses are acknowledged only
// after their marker text has been stable across the polling window.

(function (root) {
  "use strict";

  var BRIDGE_BASE = "http://127.0.0.1:8765";
  var ADAPTER_ID = "chatgpt_web";
  var CONVERSATION_PATH_RE = /\/c\/([A-Za-z0-9_-]+)/;
  var REQUEST_HEAD = "[DEVORCH_WEB_SOL_REQUEST ";
  var RESPONSE_HEAD = "[DEVORCH_WEB_SOL_RESPONSE ";
  var CLAIM_PATH = "/v1/claim";
  var RENEW_PATH = "/v1/renew";
  var RESPONSE_PATH = "/v1/response";
  var PROGRESS_PATH = "/v1/progress";
  var POLL_MS = 1500;
  var RENEW_INTERVAL_MS = 20000;
  var WAIT_DEADLINE_MS = 15 * 60 * 1000;
  var RETRY_MS = 2500;
  var SUBMIT_BUTTON_WAIT_MS = 3000;
  var SUBMISSION_CONFIRM_MS = 10000;
  var REMOTE_SYNC_RELOAD_AFTER_MS = 30000;
  var REMOTE_SYNC_RELOAD_COOLDOWN_MS = 60000;

  // Renewal-failure policy verdicts (transport authority only):
  //   continue - 200, lease extended, keep waiting;
  //   retry    - transient network/5xx, may retry inside the lease window;
  //   abandon  - authoritative rejection (e.g. 409) or no safe window left.
  var RENEW_CONTINUE = "continue";
  var RENEW_RETRY = "retry";
  var RENEW_ABANDON = "abandon";
  var RENEW_RETRY_MS = 5000;

  // Response-stability gate. ChatGPT streams assistant output, so a DOM
  // snapshot that already carries the response marker may still be growing.
  // The adapter only acknowledges text whose content has been unchanged for
  // RESPONSE_STABILITY_MS, so a partial answer is never POSTed as final.
  var RESPONSE_STABILITY_MS = 1500;
  // Cross-poll stability state for the response currently being awaited:
  // responseStability.ready turns true only after the same complete text has
  // been observed unchanged for the full stability window.
  var responseStability = { text: null, since: 0, ready: false };


  var STATUS_ELEMENT_ID = "devorch-web-status";

  function ensureStatusBadge() {
    if (typeof document === "undefined" || !document.body) { return null; }
    var badge = document.getElementById(STATUS_ELEMENT_ID);
    if (badge) { return badge; }
    badge = document.createElement("div");
    badge.id = STATUS_ELEMENT_ID;
    badge.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483647;padding:5px 9px;border-radius:7px;font:12px/1.2 system-ui,sans-serif;color:#fff;background:#555;box-shadow:0 1px 5px rgba(0,0,0,.25);pointer-events:none;opacity:.92";
    document.body.appendChild(badge);
    return badge;
  }

  function setAdapterStatus(state, detail) {
    var badge = ensureStatusBadge();
    if (!badge) { return; }
    var colors = { LIVE: "#237a3b", IDLE: "#555", CLAIMED: "#8a5a00", WAITING: "#2457a6", OFFLINE: "#a32929" };
    badge.textContent = "DevOrch · " + state;
    badge.style.background = colors[state] || "#555";
    badge.setAttribute("data-state", state);
    badge.title = detail || state;
  }

  function showProgressToast(notification) {
    if (typeof document === "undefined" || !document.body || !notification) { return null; }
    var toast = document.createElement("div");
    toast.className = "devorch-progress-toast";
    toast.style.cssText = "position:fixed;right:12px;bottom:42px;z-index:2147483646;padding:6px 10px;border-radius:6px;font:11px/1.3 system-ui,sans-serif;color:#fff;background:#1a5276;box-shadow:0 1px 4px rgba(0,0,0,.3);pointer-events:none;opacity:.95;transition:opacity 0.5s ease";
    var milestone = notification.milestone || "PROGRESS";
    var msg = notification.message || notification.summary || notification.task_id || "";
    toast.textContent = "[" + milestone + "] " + msg;
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.style.opacity = "0";
      setTimeout(function () {
        if (toast.parentNode) { toast.parentNode.removeChild(toast); }
      }, 600);
    }, 5000);
    return toast;
  }

  // ------------------------------------------------------------------
  // pure helpers (also exercised by the automated Node test harness)
  // ------------------------------------------------------------------

  function conversationIdFromUrl(rawUrl) {
    if (typeof rawUrl !== "string" || rawUrl.length === 0) {
      return "";
    }
    var match = CONVERSATION_PATH_RE.exec(rawUrl);
    return match ? match[1] : "";
  }

  function currentBindingId() {
    if (typeof window === "undefined" || !window.location) {
      return "";
    }
    return conversationIdFromUrl(String(window.location.href));
  }

  function responseMatches(text, requestId) {
    if (typeof text !== "string" || typeof requestId !== "string") {
      return false;
    }
    return text.indexOf(RESPONSE_HEAD + requestId + "]") !== -1;
  }

  // Advances one poll's response-stability state. ``state`` may be null on the
  // first observation. A text change resets ``since``/``ready``; unchanged
  // text flips ``ready`` once it has persisted for RESPONSE_STABILITY_MS.
  // Exposed through the test API so the stability contract is deterministic.
  function advanceResponseStability(state, text, now) {
    if (!state || typeof state !== "object") {
      state = { text: null, since: 0, ready: false };
    }
    var current = typeof text === "string" ? text : "";
    if (current !== state.text) {
      state.text = current;
      state.since = now;
      state.ready = false;
    } else if (!state.ready && now - state.since >= RESPONSE_STABILITY_MS) {
      state.ready = true;
    }
    return state;
  }

  function remoteSyncReloadStorageKey(bindingId, requestId) {
    return "devorch:websol:remote-sync-reload:" + bindingId + ":" + requestId;
  }

  function shouldRemoteSyncReload(lastReloadMs, now) {
    return !lastReloadMs || now - lastReloadMs >= REMOTE_SYNC_RELOAD_COOLDOWN_MS;
  }

  function submittedStorageKey(bindingId, requestId) {
    return "devorch:websol:submitted:" + bindingId + ":" + requestId;
  }

  function markRequestSubmitted(binding, requestId) {
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem(submittedStorageKey(binding, requestId), String(Date.now()));
      }
    } catch (err) {
      // Storage can be unavailable; DOM evidence remains the fallback.
    }
  }

  function hasSubmittedStorageMark(bindingId, requestId) {
    try {
      return typeof localStorage !== "undefined" &&
        localStorage.getItem(submittedStorageKey(bindingId, requestId)) !== null;
    } catch (err) {
      return false;
    }
  }

  // Pure classifier for one /v1/renew HTTP result. It reasons only about
  // transport authority - it never parses or applies any workflow content.
  function classifyRenewResult(result) {
    if (!result || typeof result !== "object" || typeof result.status !== "number") {
      return RENEW_ABANDON;
    }
    if (result.status === 200) {
      return RENEW_CONTINUE;
    }
    if (result.status === 0 || (result.status >= 500 && result.status <= 599)) {
      return RENEW_RETRY;
    }
    return RENEW_ABANDON;
  }

  // Reads the lease_expires_at transport metadata (UTC ISO-8601) from a claim
  // or renew envelope into an epoch-millisecond timestamp; 0 when absent.
  function leaseExpiryMs(metadata) {
    if (metadata && typeof metadata.lease_expires_at === "string") {
      var parsed = Date.parse(metadata.lease_expires_at);
      if (!isNaN(parsed)) {
        return parsed;
      }
    }
    return 0;
  }

  function remoteSyncReloadAllowed(bindingId, requestId, now) {
    try {
      var raw = localStorage.getItem(remoteSyncReloadStorageKey(bindingId, requestId));
      var last = raw ? Number(raw) : 0;
      return shouldRemoteSyncReload(last, now);
    } catch (err) { return true; }
  }

  function markRemoteSyncReload(bindingId, requestId, now) {
    try { localStorage.setItem(remoteSyncReloadStorageKey(bindingId, requestId), String(now)); } catch (err) {}
  }

  function userMessageHasRequest(requestId) {
    if (typeof document === "undefined") {
      return false;
    }
    var marker = REQUEST_HEAD + requestId + "]";
    var messages = document.querySelectorAll("[data-message-author-role='user']");
    for (var i = messages.length - 1; i >= 0; i--) {
      if ((messages[i].innerText || "").indexOf(marker) !== -1) {
        return true;
      }
    }
    return false;
  }

  function requestAlreadySubmitted(bindingId, requestId) {
    // A localStorage mark is only telemetry. It is not submission authority:
    // a click can leave text in ChatGPT's conversationDrafts without creating
    // a real user turn. Only live user-message DOM evidence proves submission.
    if (!userMessageHasRequest(requestId)) {
      return false;
    }
    markRequestSubmitted(bindingId, requestId);
    return true;
  }

  var adapterApi = {
    conversationIdFromUrl: conversationIdFromUrl,
    currentBindingId: currentBindingId,
    responseMatches: responseMatches,
    advanceResponseStability: advanceResponseStability,
    classifyRenewResult: classifyRenewResult,
    requestAlreadySubmitted: requestAlreadySubmitted,
    userMessageHasRequest: userMessageHasRequest,
    markRequestSubmitted: markRequestSubmitted,
    shouldRemoteSyncReload: shouldRemoteSyncReload,
    isStopComposerButton: isStopComposerButton,
    findSendButton: findSendButton,
    showProgressToast: showProgressToast,
    pollProgress: pollProgress
  };

  // Exposed for the automated adapter test (Node `require`); harmless in a
  // real browser content-script context.
  root.__DEVORCH_CHATGPT_ADAPTER_TEST__ = adapterApi;

  // ------------------------------------------------------------------
  // bridge transport calls (GM_xmlhttpRequest first, fetch fallback)
  // ------------------------------------------------------------------

  function bridgePost(path, payload) {
    var url = BRIDGE_BASE + path;
    var body = JSON.stringify(payload);
    return new Promise(function (resolve) {
      var settled = false;
      function finish(status, text) {
        if (settled) { return; }
        settled = true;
        resolve({ status: status, text: text });
      }
      if (typeof GM_xmlhttpRequest === "function") {
        GM_xmlhttpRequest({
          method: "POST",
          url: url,
          data: body,
          headers: { "Content-Type": "application/json" },
          timeout: 8000,
          onload: function (response) { finish(response.status, response.responseText); },
          onerror: function () { finish(0, ""); },
          ontimeout: function () { finish(0, ""); }
        });
        return;
      }
      if (typeof fetch === "function") {
        fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body
        }).then(function (response) {
          response.text().then(function (text) { finish(response.status, text); });
        }).catch(function () { finish(0, ""); });
        return;
      }
      finish(0, "");
    });
  }

  function claimOnce(bindingId) {
    return bridgePost(CLAIM_PATH, { adapter: ADAPTER_ID, binding_id: bindingId }).then(function (result) {
      if (result.status === 204) {
        setAdapterStatus("LIVE", "Bridge connected; no queued request");
        return null;
      }
      if (result.status !== 200 || !result.text) {
        setAdapterStatus("OFFLINE", "Bridge claim failed: HTTP " + result.status);
        return null;
      }
      try {
        var claim = JSON.parse(result.text);
        return claim && claim.request_id ? claim : null;
      } catch (err) {
        return null;
      }
    });
  }

  function respond(bindingId, claim, responseText) {
    return bridgePost(RESPONSE_PATH, {
      adapter: ADAPTER_ID,
      binding_id: bindingId,
      request_id: claim.request_id,
      nonce: claim.nonce,
      claim_token: claim.claim_token,
      response_text: responseText
    });
  }

  function pollProgress(bindingId) {
    return bridgePost(PROGRESS_PATH, { adapter: ADAPTER_ID, binding_id: bindingId }).then(function (result) {
      if (result && result.status === 200 && result.text) {
        try {
          var data = JSON.parse(result.text);
          var items = data ? (data.claimed || data.notifications) : null;
          if (Array.isArray(items)) {
            for (var i = 0; i < items.length; i++) {
              showProgressToast(items[i]);
            }
          }
        } catch (err) {}
      }
      return result;
    });
  }

  // Renews the exact active claim while ChatGPT is still answering. Purely
  // transport: posts back the claim identity so the server can extend the
  // lease; never inspects or decides any workflow content. The caller must
  // inspect the returned HTTP result (see classifyRenewResult) so a failed
  // renewal can never silently lose claim authority.
  function renewClaim(bindingId, claim) {
    return bridgePost(RENEW_PATH, {
      adapter: ADAPTER_ID,
      binding_id: bindingId,
      request_id: claim.request_id,
      nonce: claim.nonce,
      claim_token: claim.claim_token
    });
  }

  function parsedJsonText(result) {
    if (!result || typeof result.text !== "string") {
      return null;
    }
    try {
      var body = JSON.parse(result.text);
      return body && typeof body === "object" ? body : null;
    } catch (err) {
      return null;
    }
  }

  // ------------------------------------------------------------------
  // ChatGPT DOM adaptation (best effort; live DOM acceptance is a
  // separate deployment gate - automated tests never touch this path)
  // ------------------------------------------------------------------

  function setComposerText(composer, text) {
    composer.focus();
    // Replace the entire composer, never append to a stale ChatGPT draft.
    // The old adapter could leave Pn+1 concatenated after Pn, then mistake the
    // click for a successful submission.
    try {
      if (typeof window !== "undefined" && window.getSelection && document.createRange) {
        var selection = window.getSelection();
        var range = document.createRange();
        range.selectNodeContents(composer);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      if (document.execCommand("insertText", false, text)) {
        return true;
      }
    } catch (err) {
      // fall through to direct replacement
    }
    composer.textContent = "";
    composer.textContent = text;
    if (typeof InputEvent === "function") {
      composer.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: text
      }));
    }
    return true;
  }

  function isStopComposerButton(button) {
    if (!button) { return false; }
    var parts = [
      button.getAttribute && button.getAttribute("data-testid"),
      button.getAttribute && button.getAttribute("aria-label"),
      button.getAttribute && button.getAttribute("title"),
      button.innerText
    ];
    var text = parts.filter(function (part) { return typeof part === "string"; }).join(" ").toLowerCase();
    return text.indexOf("stop") !== -1 || text.indexOf("停止") !== -1;
  }

  function findSendButton() {
    var legacy = document.querySelector("button[data-testid='send-button']");
    if (legacy && !isStopComposerButton(legacy)) { return legacy; }

    // ChatGPT's newer composer exposes the submit control by id. The same
    // control can become "Stop answering" while a turn is streaming, so
    // never use this fallback unless its accessible/test metadata is non-stop.
    var composerSubmit = document.querySelector("button#composer-submit-button");
    if (composerSubmit && !isStopComposerButton(composerSubmit)) { return composerSubmit; }
    return null;
  }

  function clickSendButton() {
    var send = findSendButton();
    if (send && !send.disabled) {
      send.click();
      return true;
    }
    return false;
  }

  function prepareComposer(text) {
    if (typeof document === "undefined") {
      return false;
    }
    var composer = document.querySelector("#prompt-textarea");
    if (!composer) {
      return false;
    }
    setComposerText(composer, text);
    return true;
  }

  function submitWhenReady(bindingId, claim, submitDeadline) {
    if (clickSendButton()) {
      setAdapterStatus("CLAIMED", "Send clicked; confirming user turn");
      confirmSubmission(bindingId, claim, Date.now() + SUBMISSION_CONFIRM_MS);
      return;
    }
    if (Date.now() >= submitDeadline) {
      setAdapterStatus("IDLE", "Send button not ready; retrying claim loop");
      schedule(runAdapter, RETRY_MS);
      return;
    }
    schedule(function () { submitWhenReady(bindingId, claim, submitDeadline); }, 100);
  }

  function findResponseText(requestId) {
    if (typeof document === "undefined") {
      return "";
    }
    var messages = document.querySelectorAll("[data-message-author-role='assistant']");
    for (var i = messages.length - 1; i >= 0; i--) {
      var messageText = messages[i].innerText || "";
      if (responseMatches(messageText, requestId)) {
        return messageText;
      }
    }
    return "";
  }

  function schedule(fn, delayMs) {
    setTimeout(fn, delayMs);
  }

  // ------------------------------------------------------------------
  // adapter loop: claim -> submit prompt -> await marker -> post back
  // ------------------------------------------------------------------

  function makeRenewalState(claim) {
    return {
      nextRenewAt: Date.now() + RENEW_INTERVAL_MS,
      leaseExpiresMs: leaseExpiryMs(claim),
      remoteSyncAt: Date.now() + REMOTE_SYNC_RELOAD_AFTER_MS
    };
  }

  function runAdapter() {
    if (typeof window === "undefined" || !window.document) {
      return;
    }
    var bindingId = currentBindingId();
    if (!bindingId) {
      // Tab without a stable conversation id stays idle.
      setAdapterStatus("IDLE", "Waiting for a stable /c/<conversation-id> URL");
      schedule(runAdapter, RETRY_MS);
      return;
    }

    pollProgress(bindingId);
    claimOnce(bindingId).then(function (claim) {
      if (!claim) {
        schedule(runAdapter, RETRY_MS);
        return;
      }
      setAdapterStatus("CLAIMED", "Claimed " + claim.request_id);
      if (requestAlreadySubmitted(bindingId, claim.request_id)) {
        // Reclaimed after lease expiry/reload: resume waiting for the existing
        // ChatGPT turn instead of inserting the same request a second time.
        setAdapterStatus("WAITING", "Waiting for matching ChatGPT response");
        waitForResponse(bindingId, claim, Date.now() + WAIT_DEADLINE_MS, makeRenewalState(claim));
        return;
      }
      if (!prepareComposer(claim.prompt)) {
        // Composer not ready; do not record a submission that never happened.
        schedule(runAdapter, RETRY_MS);
        return;
      }
      // ChatGPT updates composer/send-button state asynchronously. Poll the
      // submit control briefly instead of treating an immediate disabled/missing
      // button as a failed submission and rewriting the same draft forever.
      setAdapterStatus("CLAIMED", "Prompt inserted; waiting for send button");
      submitWhenReady(bindingId, claim, Date.now() + SUBMIT_BUTTON_WAIT_MS);
    });
  }

  function confirmSubmission(bindingId, claim, confirmDeadline) {
    if (requestAlreadySubmitted(bindingId, claim.request_id)) {
      setAdapterStatus("WAITING", "Prompt submitted; waiting for ChatGPT response");
      waitForResponse(bindingId, claim, Date.now() + WAIT_DEADLINE_MS, makeRenewalState(claim));
      return;
    }
    if (Date.now() >= confirmDeadline) {
      setAdapterStatus("WAITING", "Submission not confirmed; refreshing conversation");
      if (typeof window !== "undefined" && window.location && typeof window.location.reload === "function") {
        window.location.reload();
        return;
      }
      schedule(runAdapter, RETRY_MS);
      return;
    }
    schedule(function () { confirmSubmission(bindingId, claim, confirmDeadline); }, 500);
  }

  function waitForResponse(bindingId, claim, deadline, renewal) {
    var responseText = findResponseText(claim.request_id);
    responseStability = advanceResponseStability(responseStability, responseText, Date.now());
    if (responseText && responseStability.ready) {
      // The matching assistant text has been unchanged for the full stability
      // window, so it is no longer streaming: stop polling/renewing and
      // acknowledge the raw text. A still-growing answer is never acked early.
      respond(bindingId, claim, responseText).then(function (result) {
        if (result && result.status === 200) {
          setAdapterStatus("LIVE", "Response acknowledged by Bridge");
        } else {
          setAdapterStatus("OFFLINE", "Response acknowledgement failed");
        }
        schedule(runAdapter, RETRY_MS);
      });
      return;
    }
    if (!responseText && Date.now() >= renewal.remoteSyncAt && requestAlreadySubmitted(bindingId, claim.request_id) && remoteSyncReloadAllowed(bindingId, claim.request_id, Date.now())) {
      markRemoteSyncReload(bindingId, claim.request_id, Date.now());
      setAdapterStatus("WAITING", "Refreshing conversation to sync a remote-client response");
      if (typeof window !== "undefined" && window.location && typeof window.location.reload === "function") { window.location.reload(); return; }
    }
    if (Date.now() >= deadline) {
      // Wait timed out: stop renewing; the lease will expire server-side and
      // the request may be reclaimed later.
      schedule(runAdapter, RETRY_MS);
      return;
    }
    if (renewal.leaseExpiresMs > 0 && Date.now() >= renewal.leaseExpiresMs) {
      // The current lease safety window is over: do not keep waiting as if
      // the claim were still authoritative; abandon the stale claim now.
      abandonClaim(bindingId, claim);
      return;
    }
    if (Date.now() < renewal.nextRenewAt) {
      // Not yet time to renew; keep watching for the response marker.
      setTimeout(function () {
        waitForResponse(bindingId, claim, deadline, renewal);
      }, POLL_MS);
      return;
    }
    // Time to renew: inspect the HTTP result instead of firing and forgetting,
    // so a failed renewal can never silently lose the claim authority.
    renewClaim(bindingId, claim).then(function (result) {
      handleRenewOutcome(bindingId, claim, deadline, renewal, result);
    });
  }

  function handleRenewOutcome(bindingId, claim, deadline, renewal, result) {
    var verdict = classifyRenewResult(result);
    if (verdict === RENEW_CONTINUE) {
      // 200: the lease was extended. Refresh the lease metadata from the
      // renew envelope when present and keep waiting for the marker.
      var body = parsedJsonText(result);
      var renewedLease = leaseExpiryMs(body);
      if (renewedLease > 0) {
        renewal.leaseExpiresMs = renewedLease;
      }
      renewal.nextRenewAt = Date.now() + RENEW_INTERVAL_MS;
      waitForResponse(bindingId, claim, deadline, renewal);
      return;
    }
    if (verdict === RENEW_RETRY) {
      setAdapterStatus("OFFLINE", "Bridge renewal failed; retrying inside lease window");
      // Transient network/5xx failure: retry is allowed only inside the
      // current lease safety window; past expiry the claim authority is gone.
      if (Date.now() < renewal.leaseExpiresMs) {
        renewal.nextRenewAt = Date.now() + RENEW_RETRY_MS;
        waitForResponse(bindingId, claim, deadline, renewal);
        return;
      }
      abandonClaim(bindingId, claim);
      return;
    }
    // Authoritative rejection (e.g. HTTP 409 Conflict) or an unreadable
    // result: the stale claim must be abandoned immediately.
    abandonClaim(bindingId, claim);
  }

  // Explicit abandon path. The claim is stale/expired at the transport level,
  // so the adapter stops waiting and renewing for it and returns to the idle
  // claim loop. No response is posted for a stale claim; server-side lease
  // expiry lets the request be reclaimed by another adapter.
  function abandonClaim(bindingId, claim) {
    setAdapterStatus("IDLE", "Claim abandoned; returning to idle polling");
    schedule(runAdapter, RETRY_MS);
  }

  if (typeof document !== "undefined" && !root.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__) {
    setAdapterStatus("IDLE", "Adapter starting");
    schedule(runAdapter, 1000);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
