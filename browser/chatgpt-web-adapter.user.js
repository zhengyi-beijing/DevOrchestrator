// ==UserScript==
// @name         DevOrchestrator ChatGPT Web binding adapter
// @namespace    devorchestrator
// @version      0.1.14
// @description  Dumb ChatGPT Web adapter for the DevOrchestrator browser bridge plus paired 8770 conversation-presence heartbeat.
// @author       DevOrchestrator
// @match        https://chatgpt.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
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
  var CONTROL_BASE = "http://127.0.0.1:8770";
  var ADAPTER_ID = "chatgpt_web";
  var CONVERSATION_PATH_RE = /\/c\/([A-Za-z0-9_-]+)/;
  var REQUEST_HEAD = "[DEVORCH_WEB_SOL_REQUEST ";
  var RESPONSE_HEAD = "[DEVORCH_WEB_SOL_RESPONSE ";
  var CLAIM_PATH = "/v1/claim";
  var RENEW_PATH = "/v1/renew";
  var RESPONSE_PATH = "/v1/response";
  var PROGRESS_PATH = "/v1/progress";
  var PAIRING_REDEEM_PATH = "/api/v1/control/adapter-pairings/redeem";
  var SESSION_HEARTBEAT_PATH = "/api/v1/control/session-heartbeats";
  var CAPABILITY_STORAGE_KEY = "devorch:control:heartbeat-capability";
  var TAB_INSTANCE_STORAGE_KEY = "devorch:control:tab-instance";
  var LAST_URGENT_NOTIFICATION_KEY = "devorch:progress:last-urgent";
  var BROWSER_CONTROL_ACTION_PATH = "/v1/control/action";
  var BROWSER_CONTROL_TOKEN_STORAGE_KEY = "devorch:control:browser-control-capability";
  var BROWSER_CONTROL_PROCESSED_STORAGE_KEY = "devorch:control:processed-actions";
  var DEVORCH_ACTION_V1 = "DEVORCH_ACTION_V1";
  var DEVORCH_ACTION_RESULT_V1 = "DEVORCH_ACTION_RESULT_V1";
  var READ_ONLY_ACTIONS = ["status", "job_status", "read_log", "read_file", "commands_catalog"];
  var EFFECTFUL_ACTIONS = ["start_task", "cancel_job"];
  var ACTION_ENVELOPE_RE = /(?:\[DEVORCH_ACTION_V1\]?|```(?:devorch_action|json:devorch_action|action))\s*(\{[\s\S]*?\})\s*(?:\[\/DEVORCH_ACTION_V1\]|\]|```)/i;
  var FALLBACK_ACTION_RE = /DEVORCH_ACTION_V1\s*(\{[\s\S]*?\})/i;
  var ACTION_RESULT_HEAD = "[DEVORCH_ACTION_RESULT_V1 ";
  var CONTROL_HEARTBEAT_MS = 15000;
  var controlHeartbeatTimer = null;
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
  var lastHealthSnapshot = null;
  var lastHeartbeatTime = 0;

  var STATE_RANKS = {
    ATTENTION: 6,
    CLAIMED: 5,
    WAITING: 5,
    PAIRING_REQUIRED: 4,
    OFFLINE: 3,
    DEGRADED: 2,
    LIVE: 1,
    IDLE: 0
  };

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
    var colors = {
      LIVE: "#237a3b",
      IDLE: "#555",
      CLAIMED: "#8a5a00",
      WAITING: "#2457a6",
      OFFLINE: "#a32929",
      ATTENTION: "#b42318",
      PAIRING_REQUIRED: "#d97706",
      DEGRADED: "#b25e00"
    };
    badge.textContent = "DevOrch · " + state;
    badge.style.background = colors[state] || "#555";
    badge.setAttribute("data-state", state);
    badge.title = detail || state;
    // Supported badge states:
    // setAdapterStatus("LIVE", detail);
    // setAdapterStatus("IDLE", detail);
    // setAdapterStatus("CLAIMED", detail);
    // setAdapterStatus("WAITING", detail);
    // setAdapterStatus("OFFLINE", detail);
    // setAdapterStatus("DEGRADED", detail);
    // setAdapterStatus("PAIRING_REQUIRED", detail);
  }

  function updateAdapterStatusDemoting(state, detail) {
    var badge = ensureStatusBadge();
    if (!badge) { return; }
    var currentState = badge.getAttribute("data-state") || "IDLE";
    var currentRank = STATE_RANKS[currentState] !== undefined ? STATE_RANKS[currentState] : 0;
    var targetRank = STATE_RANKS[state] !== undefined ? STATE_RANKS[state] : 0;
    if (targetRank >= currentRank || currentRank <= 1) {
      setAdapterStatus(state, detail);
    }
  }

  function progressAlertText(notification) {
    if (!notification) { return "DevOrchestrator requires attention"; }
    var project = notification.project_id || "project";
    var task = notification.task_id ? (" / " + notification.task_id) : "";
    var msg = notification.message || notification.summary || "Attention required";
    return project + task + ": " + msg;
  }

  function playUrgentTone() {
    try {
      var AudioContextCtor = root.AudioContext || root.webkitAudioContext;
      if (typeof AudioContextCtor !== "function") { return false; }
      var ctx = new AudioContextCtor();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      gain.gain.value = 0.08;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.35);
      osc.onended = function () { try { ctx.close(); } catch (err) {} };
      return true;
    } catch (err) {
      return false;
    }
  }

  function showUrgentProgressAlert(notification) {
    if (!notification || notification.attention !== "urgent") { return false; }
    var notificationId = notification.notification_id || "";
    if (notificationId && typeof GM_getValue === "function" && GM_getValue(LAST_URGENT_NOTIFICATION_KEY, "") === notificationId) {
      return false;
    }
    if (notificationId && typeof GM_setValue === "function") {
      GM_setValue(LAST_URGENT_NOTIFICATION_KEY, notificationId);
    }
    var text = progressAlertText(notification);
    setAdapterStatus("ATTENTION", text);
    playUrgentTone();
    if (typeof GM_notification === "function") {
      try {
        GM_notification({
          title: "DevOrchestrator requires attention",
          text: text,
          timeout: 0,
          tag: notificationId || "devorch-attention"
        });
      } catch (err) {}
    }
    return true;
  }

  function showProgressToast(notification) {
    if (typeof document === "undefined" || !document.body || !notification) { return null; }
    var urgent = notification.attention === "urgent";
    var toast = document.createElement("div");
    toast.className = "devorch-progress-toast";
    toast.style.cssText = "position:fixed;right:12px;bottom:42px;z-index:2147483646;padding:8px 12px;border-radius:7px;font:12px/1.35 system-ui,sans-serif;color:#fff;background:" + (urgent ? "#b42318" : "#1a5276") + ";box-shadow:0 2px 8px rgba(0,0,0,.35);pointer-events:none;opacity:.97;transition:opacity 0.5s ease;max-width:420px";
    var milestone = notification.milestone || "PROGRESS";
    var msg = notification.message || notification.summary || notification.task_id || "";
    toast.textContent = (urgent ? "ATTENTION 路 " : "") + "[" + milestone + "] " + msg;
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.style.opacity = "0";
      setTimeout(function () {
        if (toast.parentNode) { toast.parentNode.removeChild(toast); }
      }, 600);
    }, urgent ? 30000 : 5000);
    return toast;
  }

  function handleProgressNotification(notification) {
    var toast = showProgressToast(notification);
    showUrgentProgressAlert(notification);
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

  function pairingFields(raw) {
    if (typeof raw !== "string") { return null; }
    var separator = raw.indexOf(":");
    if (separator <= 0 || separator >= raw.length - 1) { return null; }
    return { pairing_id: raw.slice(0, separator).trim(), code: raw.slice(separator + 1).trim() };
  }

  function newTabInstanceId() {
    if (root.crypto && typeof root.crypto.randomUUID === "function") { return root.crypto.randomUUID(); }
    return "tab-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function tabInstanceId() {
    try {
      if (typeof sessionStorage !== "undefined") {
        var existing = sessionStorage.getItem(TAB_INSTANCE_STORAGE_KEY);
        if (existing) { return existing; }
        var created = newTabInstanceId();
        sessionStorage.setItem(TAB_INSTANCE_STORAGE_KEY, created);
        return created;
      }
    } catch (err) {}
    return newTabInstanceId();
  }

  function storedCapabilityRecord() {
    try {
      if (typeof GM_getValue !== "function") { return null; }
      var raw = GM_getValue(CAPABILITY_STORAGE_KEY, null);
      if (!raw) { return null; }
      if (typeof raw === "string") {
        try {
          var parsed = JSON.parse(raw);
          if (parsed && typeof parsed === "object" && parsed.token) {
            return parsed;
          }
        } catch (e) {
          // Legacy plain string token: migrate to JSON record
          var migrated = {
            token: raw.trim(),
            pairing_id: "",
            stored_at: new Date().toISOString()
          };
          if (typeof GM_setValue === "function") {
            GM_setValue(CAPABILITY_STORAGE_KEY, JSON.stringify(migrated));
          }
          return migrated;
        }
      } else if (typeof raw === "object" && raw.token) {
        return raw;
      }
    } catch (err) {}
    return null;
  }

  function storedCapability() {
    var rec = storedCapabilityRecord();
    return rec ? rec.token : "";
  }

  function saveCapability(recordOrToken, pairingId) {
    if (typeof GM_setValue !== "function") { return; }
    if (typeof recordOrToken === "string") {
      var record = {
        token: recordOrToken.trim(),
        pairing_id: pairingId ? String(pairingId).trim() : "",
        stored_at: new Date().toISOString()
      };
      GM_setValue(CAPABILITY_STORAGE_KEY, JSON.stringify(record));
    } else if (recordOrToken && typeof recordOrToken === "object") {
      GM_setValue(CAPABILITY_STORAGE_KEY, JSON.stringify(recordOrToken));
    }
  }

  function clearCapability() {
    if (typeof GM_deleteValue === "function") {
      GM_deleteValue(CAPABILITY_STORAGE_KEY);
    }
  }

  function storedBrowserControlToken() {
    try {
      if (typeof GM_getValue !== "function") { return ""; }
      var val = GM_getValue(BROWSER_CONTROL_TOKEN_STORAGE_KEY, "");
      return typeof val === "string" ? val.trim() : "";
    } catch (e) {
      return "";
    }
  }

  function saveBrowserControlToken(token) {
    if (typeof GM_setValue === "function" && typeof token === "string") {
      GM_setValue(BROWSER_CONTROL_TOKEN_STORAGE_KEY, token.trim());
    }
  }

  function clearBrowserControlToken() {
    if (typeof GM_deleteValue === "function") {
      GM_deleteValue(BROWSER_CONTROL_TOKEN_STORAGE_KEY);
    }
  }

  function isReadOnlyAction(action) {
    if (typeof action !== "string") { return false; }
    return READ_ONLY_ACTIONS.indexOf(action.trim().toLowerCase()) !== -1;
  }

  function isEffectfulAction(action) {
    if (typeof action !== "string") { return false; }
    return EFFECTFUL_ACTIONS.indexOf(action.trim().toLowerCase()) !== -1;
  }

  function validateActionEnvelope(data) {
    if (!data || typeof data !== "object") { return null; }
    if (data.protocol !== DEVORCH_ACTION_V1) { return null; }
    if (typeof data.request_id !== "string" || !data.request_id.trim()) { return null; }
    if (typeof data.action !== "string" || !data.action.trim()) { return null; }
    var action = data.action.trim().toLowerCase();
    if (READ_ONLY_ACTIONS.indexOf(action) === -1 && EFFECTFUL_ACTIONS.indexOf(action) === -1) {
      return null;
    }
    return {
      protocol: DEVORCH_ACTION_V1,
      request_id: data.request_id.trim(),
      action: action,
      project_id: (typeof data.project_id === "string" && data.project_id.trim()) ? data.project_id.trim() : "devorchestrator",
      parameters: (data.parameters && typeof data.parameters === "object") ? data.parameters : {},
      confirmed: Boolean(data.confirmed)
    };
  }

  function parseActionEnvelope(rawText) {
    if (!rawText || typeof rawText !== "string") {
      return null;
    }
    var trimmed = rawText.trim();
    if (trimmed.charAt(0) === "{" && trimmed.charAt(trimmed.length - 1) === "}") {
      try {
        var parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === "object" && parsed.protocol === DEVORCH_ACTION_V1) {
          return validateActionEnvelope(parsed);
        }
      } catch (e) {}
    }
    var match = ACTION_ENVELOPE_RE.exec(rawText);
    if (!match) {
      match = FALLBACK_ACTION_RE.exec(rawText);
    }
    if (match && match[1]) {
      try {
        var parsed2 = JSON.parse(match[1].trim());
        if (parsed2 && typeof parsed2 === "object") {
          return validateActionEnvelope(parsed2);
        }
      } catch (e2) {}
    }
    return null;
  }

  function formatActionResult(requestId, status, dataOrError) {
    var payload = {
      protocol: DEVORCH_ACTION_RESULT_V1,
      request_id: requestId,
      status: status
    };
    if (status === "success") {
      payload.data = dataOrError;
    } else {
      var errMsg = "Execution failed";
      if (typeof dataOrError === "string") {
        errMsg = dataOrError;
      } else if (dataOrError && typeof dataOrError === "object") {
        errMsg = dataOrError.error || dataOrError.message || "Execution failed";
        if (dataOrError.reason) { payload.reason = dataOrError.reason; }
        if (dataOrError.details) { payload.details = dataOrError.details; }
      }
      payload.error = errMsg;
    }
    return "[" + DEVORCH_ACTION_RESULT_V1 + " " + requestId + "]\n" + JSON.stringify(payload, null, 2) + "\n[/" + DEVORCH_ACTION_RESULT_V1 + "]";
  }

  function getProcessedActions() {
    try {
      if (typeof sessionStorage !== "undefined") {
        var raw = sessionStorage.getItem(BROWSER_CONTROL_PROCESSED_STORAGE_KEY);
        if (raw) {
          var arr = JSON.parse(raw);
          if (Array.isArray(arr)) { return arr; }
        }
      }
    } catch (e) {}
    return [];
  }

  function isActionProcessed(requestId) {
    if (!requestId) { return true; }
    var list = getProcessedActions();
    return list.indexOf(requestId) !== -1;
  }

  function markActionProcessed(requestId) {
    if (!requestId) { return; }
    try {
      if (typeof sessionStorage !== "undefined") {
        var list = getProcessedActions();
        if (list.indexOf(requestId) === -1) {
          list.push(requestId);
          if (list.length > 200) { list.shift(); }
          sessionStorage.setItem(BROWSER_CONTROL_PROCESSED_STORAGE_KEY, JSON.stringify(list));
        }
      }
    } catch (e) {}
  }

  function isActionResultInConversation(requestId) {
    if (typeof document === "undefined" || !requestId) { return false; }
    var marker = ACTION_RESULT_HEAD + requestId + "]";
    var messages = document.querySelectorAll("[data-message-author-role]");
    for (var i = 0; i < messages.length; i++) {
      if ((messages[i].innerText || "").indexOf(marker) !== -1) {
        return true;
      }
    }
    return false;
  }

  function classifyHeartbeatResult(result) {
    if (!result || typeof result !== "object") {
      return { verdict: "network_error", shouldClear: false, reason: "missing_result" };
    }
    var status = typeof result.status === "number" ? result.status : 0;
    var body = null;
    if (typeof result.text === "string" && result.text) {
      try { body = JSON.parse(result.text); } catch (e) {}
    } else if (result.body && typeof result.body === "object") {
      body = result.body;
    }
    var errReason = (body && (body.message || body.error || "")) || "";

    if (status === 200) {
      return { verdict: "ok", shouldClear: false, body: body };
    }
    if (status === 503 || errReason.indexOf("capability_store_unavailable") !== -1) {
      return { verdict: "store_unavailable", shouldClear: false, reason: "capability_store_unavailable" };
    }
    if (status === 0 || (status >= 500 && status <= 599)) {
      return { verdict: "network_error", shouldClear: false, reason: "http_" + status };
    }
    if (status === 401) {
      var reason = "unknown_capability";
      if (errReason.indexOf("revoked") !== -1) {
        reason = "revoked_capability";
      }
      return { verdict: "unauthorized", shouldClear: true, reason: reason };
    }
    return { verdict: "error", shouldClear: false, reason: "http_" + status };
  }

  function computeAdapterState(input) {
    if (!input || typeof input !== "object") {
      return { state: "IDLE", detail: "Idle" };
    }
    if (!input.hasCapability) {
      return { state: "PAIRING_REQUIRED", detail: "Heartbeat pairing required" };
    }
    if (!input.bindingId) {
      return { state: "IDLE", detail: "Waiting for ChatGPT conversation (/c/...)" };
    }
    if (input.attentionMessage) {
      return { state: "ATTENTION", detail: input.attentionMessage };
    }

    var rawAvail = input.healthAvailability || (input.health && input.health.availability) || "";
    var healthAvail = typeof rawAvail === "string" ? rawAvail.trim().toUpperCase() : "";

    var now = typeof input.now === "number" ? input.now : Date.now();
    var isExpired = false;
    if (input.health && typeof input.health === "object" && typeof input.health.valid_until === "string") {
      var vu = Date.parse(input.health.valid_until);
      if (!isNaN(vu) && now > vu) {
        isExpired = true;
      }
    }
    var bindingMismatch = false;
    if (input.health && typeof input.health === "object" && input.health.binding_id && input.bindingId) {
      if (input.health.binding_id !== input.bindingId) {
        bindingMismatch = true;
      }
    }

    // 1. Authoritative pairing required
    if (healthAvail === "PAIRING_REQUIRED") {
      return { state: "PAIRING_REQUIRED", detail: input.healthDetail || (input.health && input.health.reason) || "Heartbeat pairing required" };
    }

    // 2. Bridge transport errors or offline health
    if (input.bridgeError) {
      return { state: "OFFLINE", detail: input.bridgeError };
    }
    if (input.bridgeConnected === false) {
      return { state: "OFFLINE", detail: "Bridge disconnected" };
    }
    if (healthAvail === "OFFLINE" || healthAvail === "PROBE_FAILED") {
      return { state: "OFFLINE", detail: input.healthDetail || (input.health && input.health.reason) || ("Web Sol " + healthAvail.toLowerCase()) };
    }

    // 3. Degradations (duplicate tabs, stale heartbeat, expired snapshot, binding mismatch, or DEGRADED health)
    if (input.duplicateTabs) {
      return { state: "DEGRADED", detail: "Duplicate active tabs detected" };
    }
    if (input.staleHeartbeat) {
      return { state: "DEGRADED", detail: "Stale control heartbeat" };
    }
    if (bindingMismatch) {
      return { state: "DEGRADED", detail: "Binding mismatch with authoritative health" };
    }
    if (isExpired) {
      return { state: "DEGRADED", detail: "Authoritative health snapshot expired" };
    }
    if (healthAvail === "DEGRADED") {
      return { state: "DEGRADED", detail: input.healthDetail || (input.health && input.health.reason) || "Web Sol degraded" };
    }

    // 4. Authoritative AVAILABLE required to promote LIVE
    // Missing, wrong-key or expired health snapshots are explicitly non-LIVE.
    if (healthAvail !== "AVAILABLE") {
      return { state: "DEGRADED", detail: input.healthDetail || "Authoritative AVAILABLE health required" };
    }

    // 5. Activity details (CLAIMED / WAITING) or idle/live
    if (input.claimPhase === "waiting") {
      return { state: "WAITING", detail: input.claimDetail || "Waiting for ChatGPT response" };
    }
    if (input.claimPhase === "claimed") {
      return { state: "CLAIMED", detail: input.claimDetail || "Claim active" };
    }
    if (input.bridgeConnected) {
      return { state: "LIVE", detail: input.bridgeDetail || "Bridge connected; listening for work" };
    }
    return { state: "IDLE", detail: "Idle" };
  }

  function applyComputedState(extra) {
    var rec = storedCapabilityRecord();
    var bindingId = currentBindingId();
    var isHeartbeatFresh = lastHeartbeatTime > 0 && (Date.now() - lastHeartbeatTime) <= CONTROL_HEARTBEAT_MS * 3;
    var input = {
      hasCapability: !!(rec ? rec.token : storedCapability()),
      bindingId: bindingId,
      health: lastHealthSnapshot,
      healthAvailability: lastHealthSnapshot ? lastHealthSnapshot.availability : null,
      staleHeartbeat: !isHeartbeatFresh,
      now: Date.now()
    };
    if (extra && typeof extra === "object") {
      for (var k in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, k)) {
          input[k] = extra[k];
        }
      }
    }
    var res = computeAdapterState(input);
    setAdapterStatus(res.state, res.detail);
    return res;
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
    findResponseText: findResponseText,
    advanceResponseStability: advanceResponseStability,
    classifyRenewResult: classifyRenewResult,
    requestAlreadySubmitted: requestAlreadySubmitted,
    userMessageHasRequest: userMessageHasRequest,
    markRequestSubmitted: markRequestSubmitted,
    shouldRemoteSyncReload: shouldRemoteSyncReload,
    isStopComposerButton: isStopComposerButton,
    findSendButton: findSendButton,
    showProgressToast: showProgressToast,
    showUrgentProgressAlert: showUrgentProgressAlert,
    handleProgressNotification: handleProgressNotification,
    pollProgress: pollProgress,
    pairingFields: pairingFields,
    tabInstanceId: tabInstanceId,
    storedCapabilityRecord: storedCapabilityRecord,
    storedCapability: storedCapability,
    saveCapability: saveCapability,
    clearCapability: clearCapability,
    classifyHeartbeatResult: classifyHeartbeatResult,
    computeAdapterState: computeAdapterState,
    applyComputedState: applyComputedState,
    setAdapterStatus: setAdapterStatus,
    updateAdapterStatusDemoting: updateAdapterStatusDemoting,
    sendControlHeartbeat: sendControlHeartbeat,
    redeemControlPairing: redeemControlPairing,
    parseActionEnvelope: parseActionEnvelope,
    validateActionEnvelope: validateActionEnvelope,
    formatActionResult: formatActionResult,
    isReadOnlyAction: isReadOnlyAction,
    isEffectfulAction: isEffectfulAction,
    storedBrowserControlToken: storedBrowserControlToken,
    saveBrowserControlToken: saveBrowserControlToken,
    clearBrowserControlToken: clearBrowserControlToken,
    isActionProcessed: isActionProcessed,
    markActionProcessed: markActionProcessed,
    isActionResultInConversation: isActionResultInConversation,
    dispatchBrowserControlAction: dispatchBrowserControlAction,
    showEffectfulConfirmationModal: showEffectfulConfirmationModal,
    submitActionResult: submitActionResult,
    scanAndProcessBrowserActions: scanAndProcessBrowserActions
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

  function controlPost(path, payload, capability) {
    var url = CONTROL_BASE + path;
    var body = JSON.stringify(payload);
    return new Promise(function (resolve) {
      var settled = false;
      function finish(status, text) {
        if (settled) { return; }
        settled = true; resolve({ status: status, text: text });
      }
      var headers = { "Content-Type": "application/json", "Origin": "https://chatgpt.com" };
      if (capability) { headers.Authorization = "Bearer " + capability; }
      if (typeof GM_xmlhttpRequest === "function") {
        GM_xmlhttpRequest({
          method: "POST", url: url, data: body, headers: headers, timeout: 8000,
          onload: function (response) { finish(response.status, response.responseText); },
          onerror: function () { finish(0, ""); }, ontimeout: function () { finish(0, ""); }
        });
        return;
      }
      if (typeof fetch === "function") {
        delete headers.Origin;
        fetch(url, { method: "POST", headers: headers, body: body }).then(function (response) {
          response.text().then(function (text) { finish(response.status, text); });
        }).catch(function () { finish(0, ""); });
        return;
      }
      finish(0, "");
    });
  }

  function redeemControlPairing(raw) {
    var fields = pairingFields(raw);
    if (!fields) { return Promise.resolve(false); }
    return controlPost(PAIRING_REDEEM_PATH, fields, "").then(function (result) {
      var body = parsedJsonText(result);
      var capability = body && body.data && body.data.capability;
      if (result.status !== 200 || typeof capability !== "string" || !capability) { return false; }
      saveCapability(capability, fields.pairing_id);
      scheduleControlHeartbeat(0);
      return true;
    });
  }

  function sendControlHeartbeat() {
    var capRecord = storedCapabilityRecord();
    var capability = capRecord ? capRecord.token : storedCapability();
    var bindingId = currentBindingId();
    if (!capability || !bindingId || typeof window === "undefined") {
      if (!capability) {
        updateAdapterStatusDemoting("PAIRING_REQUIRED", "Heartbeat pairing required");
      }
      return Promise.resolve(false);
    }
    return controlPost(SESSION_HEARTBEAT_PATH, {
      adapter: ADAPTER_ID, binding_id: bindingId,
      title: (typeof document.title === "string" && document.title.trim()) || ("ChatGPT conversation " + bindingId),
      url: String(window.location.href), tab_instance_id: tabInstanceId()
    }, capability).then(function (result) {
      var classification = classifyHeartbeatResult(result);
      if (classification.shouldClear) {
        clearCapability();
        lastHealthSnapshot = null;
        applyComputedState({ hasCapability: false, healthDetail: "Capability " + classification.reason });
        return false;
      }
      if (classification.verdict === "ok") {
        if (capRecord) {
          capRecord.verified_at = new Date().toISOString();
          saveCapability(capRecord);
        }
        lastHeartbeatTime = Date.now();
        var health = classification.body && classification.body.data && classification.body.data.websol_health;
        lastHealthSnapshot = health || null;
        var sData = classification.body && classification.body.data;
        var activeTabs = (sData && sData.session && sData.session.active_tab_count) || (sData && sData.active_tab_count) || 0;
        var dupTabs = activeTabs > 1;
        applyComputedState({ duplicateTabs: dupTabs });
        return true;
      }
      if (classification.verdict === "store_unavailable") {
        applyComputedState({ healthAvailability: "DEGRADED", healthDetail: "Capability store unavailable" });
        return false;
      }
      return false;
    });
  }

  function scheduleControlHeartbeat(delayMs) {
    if (controlHeartbeatTimer !== null && typeof clearTimeout === "function") { clearTimeout(controlHeartbeatTimer); }
    controlHeartbeatTimer = setTimeout(function () {
      sendControlHeartbeat().then(function () { scheduleControlHeartbeat(CONTROL_HEARTBEAT_MS); });
    }, delayMs);
  }

  function registerControlPairingMenu() {
    if (typeof GM_registerMenuCommand !== "function") { return; }
    GM_registerMenuCommand("Pair DevOrchestrator 8770 heartbeat", function () {
      var raw = typeof root.prompt === "function" ? root.prompt("Paste pairing-id:code from the 8770 dashboard") : "";
      redeemControlPairing(raw || "").then(function (ok) {
        if (typeof root.alert === "function") { root.alert(ok ? "DevOrchestrator heartbeat paired" : "Pairing failed"); }
      });
    });
    GM_registerMenuCommand("Forget DevOrchestrator heartbeat capability", clearCapability);
    GM_registerMenuCommand("Pair DevOrchestrator browser control", function () {
      var raw = typeof root.prompt === "function" ? root.prompt("Paste browser control capability token (from dev-orchestrator browser-token mint):") : "";
      if (raw && raw.trim()) {
        saveBrowserControlToken(raw.trim());
        if (typeof root.alert === "function") { root.alert("DevOrchestrator browser control token paired"); }
      }
    });
    GM_registerMenuCommand("Forget DevOrchestrator browser control token", function () {
      clearBrowserControlToken();
      if (typeof root.alert === "function") { root.alert("DevOrchestrator browser control token forgotten"); }
    });
    GM_registerMenuCommand("Test DevOrchestrator attention alert", function () {
      handleProgressNotification({
        notification_id: "manual-test-" + Date.now(),
        project_id: "devorchestrator",
        task_id: "manual-test",
        milestone: "ATTENTION_TEST",
        message: "Manual browser alert test",
        attention: "urgent"
      });
    });
  }

  function claimOnce(bindingId) {
    return bridgePost(CLAIM_PATH, { adapter: ADAPTER_ID, binding_id: bindingId }).then(function (result) {
      if (result.status === 204) {
        applyComputedState({ bridgeConnected: true, bridgeDetail: "Bridge connected; no queued request" });
        return null;
      }
      if (result.status !== 200 || !result.text) {
        applyComputedState({ bridgeError: "Bridge claim failed: HTTP " + result.status, bridgeConnected: false });
        return null;
      }
      try {
        var claim = JSON.parse(result.text);
        if (claim && claim.request_id) {
          applyComputedState({ claimPhase: "claimed", claimDetail: "Claim active: " + claim.request_id });
          return claim;
        }
        return null;
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
              handleProgressNotification(items[i]);
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
    if (typeof document === "undefined") { return ""; }
    var assistants = document.querySelectorAll("[data-message-author-role='assistant']");
    for (var i = assistants.length - 1; i >= 0; i--) {
      var messageText = assistants[i].innerText || "";
      if (responseMatches(messageText, requestId)) { return messageText; }
    }
    var marker = REQUEST_HEAD + requestId + "]";
    var turns = document.querySelectorAll("[data-message-author-role]");
    var seenExactRequest = false;
    for (var j = 0; j < turns.length; j++) {
      var turn = turns[j];
      var role = turn.getAttribute ? turn.getAttribute("data-message-author-role") : "";
      var text = turn.innerText || "";
      if (role === "user") {
        if (text.indexOf(marker) !== -1) { seenExactRequest = true; continue; }
        if (seenExactRequest) { return ""; }
      }
      if (seenExactRequest && role === "assistant" && text.trim()) {
        return RESPONSE_HEAD + requestId + "]\n" + text;
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
          applyComputedState({ bridgeConnected: true, bridgeDetail: "Response acknowledged by Bridge" });
        } else {
          applyComputedState({ bridgeError: "Response acknowledgement failed", bridgeConnected: false });
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
      applyComputedState({ bridgeError: "Bridge renewal failed; retrying inside lease window", bridgeConnected: false });
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
    applyComputedState({ bridgeConnected: true, bridgeDetail: "Claim abandoned; returning to idle polling" });
    schedule(runAdapter, RETRY_MS);
  }

  // ------------------------------------------------------------------
  // P19.3 ChatGPT Plus Browser Control Bridge execution & scanning
  // ------------------------------------------------------------------

  function dispatchBrowserControlAction(actionPayload, confirmed) {
    var token = storedBrowserControlToken();
    if (!token) {
      return Promise.resolve({
        status: 401,
        text: JSON.stringify({
          status: "error",
          reason: "Unauthorized",
          error: "Browser control capability token not paired in Tampermonkey. Use 'Pair DevOrchestrator browser control' menu command."
        })
      });
    }

    var payload = Object.assign({}, actionPayload);
    if (confirmed) {
      payload.confirmed = true;
    }

    var url = BRIDGE_BASE + BROWSER_CONTROL_ACTION_PATH;
    var body = JSON.stringify(payload);
    return new Promise(function (resolve) {
      var settled = false;
      function finish(status, text) {
        if (settled) { return; }
        settled = true;
        resolve({ status: status, text: text });
      }
      var headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "Origin": "https://chatgpt.com"
      };
      if (typeof GM_xmlhttpRequest === "function") {
        GM_xmlhttpRequest({
          method: "POST",
          url: url,
          data: body,
          headers: headers,
          timeout: 15000,
          onload: function (response) { finish(response.status, response.responseText); },
          onerror: function () { finish(0, ""); },
          ontimeout: function () { finish(0, ""); }
        });
        return;
      }
      if (typeof fetch === "function") {
        delete headers.Origin;
        fetch(url, {
          method: "POST",
          headers: headers,
          body: body
        }).then(function (response) {
          response.text().then(function (text) { finish(response.status, text); });
        }).catch(function () { finish(0, ""); });
        return;
      }
      finish(0, "");
    });
  }

  function showEffectfulConfirmationModal(actionEnvelope, onApprove, onReject) {
    if (typeof document === "undefined" || !document.body) {
      if (typeof onReject === "function") { onReject("Document body not available"); }
      return null;
    }

    var existing = document.getElementById("devorch-confirm-modal");
    if (existing && existing.parentNode) {
      existing.parentNode.removeChild(existing);
    }

    var overlay = document.createElement("div");
    overlay.id = "devorch-confirm-modal";
    overlay.style.cssText = "position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.55);z-index:2147483646;display:flex;align-items:center;justify-content:center;font-family:system-ui,-apple-system,sans-serif;";

    var modal = document.createElement("div");
    modal.style.cssText = "background:#1e1e1e;color:#fff;border:1px solid #444;border-radius:12px;padding:24px;max-width:520px;width:90%;box-shadow:0 12px 36px rgba(0,0,0,0.6);";

    var title = document.createElement("h3");
    title.style.cssText = "margin:0 0 12px 0;font-size:18px;color:#f59e0b;display:flex;align-items:center;gap:8px;";
    title.textContent = "⚠️ DevOrchestrator Effectful Action Confirmation";
    modal.appendChild(title);

    var desc = document.createElement("p");
    desc.style.cssText = "margin:0 0 16px 0;font-size:14px;color:#d1d5db;line-height:1.5;";
    desc.textContent = "ChatGPT has requested to execute an effectful operation on your local environment:";
    modal.appendChild(desc);

    var detailsBox = document.createElement("div");
    detailsBox.style.cssText = "background:#111;border:1px solid #333;border-radius:8px;padding:12px;margin-bottom:20px;font-family:monospace;font-size:13px;color:#e5e7eb;word-break:break-all;";

    var actRow = document.createElement("div");
    actRow.innerHTML = "<strong>Action:</strong> <span style='color:#60a5fa;'>" + (actionEnvelope.action || "") + "</span>";
    detailsBox.appendChild(actRow);

    var projRow = document.createElement("div");
    projRow.innerHTML = "<strong>Project:</strong> " + (actionEnvelope.project_id || "devorchestrator");
    detailsBox.appendChild(projRow);

    var reqRow = document.createElement("div");
    reqRow.innerHTML = "<strong>Request ID:</strong> " + (actionEnvelope.request_id || "");
    detailsBox.appendChild(reqRow);

    var params = actionEnvelope.parameters || {};
    if (params.command_ref) {
      var cmdRow = document.createElement("div");
      cmdRow.innerHTML = "<strong>Command:</strong> <span style='color:#34d399;'>" + params.command_ref + "</span>";
      detailsBox.appendChild(cmdRow);
    }
    if (params.job_id) {
      var jobRow = document.createElement("div");
      jobRow.innerHTML = "<strong>Job ID:</strong> " + params.job_id;
      detailsBox.appendChild(jobRow);
    }
    if (params.reason) {
      var rRow = document.createElement("div");
      rRow.innerHTML = "<strong>Reason:</strong> " + params.reason;
      detailsBox.appendChild(rRow);
    }
    modal.appendChild(detailsBox);

    var btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;justify-content:flex-end;gap:12px;";

    var rejectBtn = document.createElement("button");
    rejectBtn.textContent = "Reject";
    rejectBtn.style.cssText = "padding:8px 16px;border-radius:6px;border:1px solid #555;background:#333;color:#fff;cursor:pointer;font-weight:500;font-size:14px;";
    rejectBtn.onclick = function () {
      if (overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
      if (typeof onReject === "function") { onReject("Action rejected by user in browser confirmation dialog"); }
    };
    btnRow.appendChild(rejectBtn);

    var approveBtn = document.createElement("button");
    approveBtn.textContent = "Approve & Execute";
    approveBtn.style.cssText = "padding:8px 16px;border-radius:6px;border:none;background:#2563eb;color:#fff;cursor:pointer;font-weight:600;font-size:14px;";
    approveBtn.onclick = function () {
      if (overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
      if (typeof onApprove === "function") { onApprove(); }
    };
    btnRow.appendChild(approveBtn);

    modal.appendChild(btnRow);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    return overlay;
  }

  function submitActionResult(resultText) {
    if (typeof document === "undefined") { return false; }
    var composer = document.querySelector("#prompt-textarea");
    if (!composer) { return false; }
    setComposerText(composer, resultText);
    setTimeout(function () {
      clickSendButton();
    }, 150);
    return true;
  }

  var actionScanActive = false;

  function scanAndProcessBrowserActions() {
    if (actionScanActive || typeof document === "undefined") { return; }
    var assistants = document.querySelectorAll("[data-message-author-role='assistant']");
    if (!assistants || assistants.length === 0) { return; }

    for (var i = assistants.length - 1; i >= 0; i--) {
      var text = assistants[i].innerText || "";
      var envelope = parseActionEnvelope(text);
      if (!envelope || !envelope.request_id) { continue; }

      var reqId = envelope.request_id;
      if (isActionProcessed(reqId) || isActionResultInConversation(reqId)) {
        continue;
      }

      actionScanActive = true;
      markActionProcessed(reqId);

      var isEffectful = isEffectfulAction(envelope.action);
      if (isEffectful) {
        showEffectfulConfirmationModal(
          envelope,
          function onApprove() {
            dispatchBrowserControlAction(envelope, true).then(function (result) {
              handleActionResult(envelope, result);
              actionScanActive = false;
            });
          },
          function onReject(reason) {
            var rejectedResult = formatActionResult(reqId, "rejected", reason);
            submitActionResult(rejectedResult);
            actionScanActive = false;
          }
        );
      } else {
        dispatchBrowserControlAction(envelope, false).then(function (result) {
          handleActionResult(envelope, result);
          actionScanActive = false;
        });
      }
      break;
    }
  }

  function handleActionResult(envelope, result) {
    var reqId = envelope.request_id;
    var status = (result.status >= 200 && result.status < 300) ? "success" : "error";
    var body = null;
    try {
      body = JSON.parse(result.text);
    } catch (e) {
      body = { error: result.text || "Empty response from bridge" };
    }
    var dataOrError = (status === "success" && body && body.data !== undefined) ? body.data : body;
    var formatted = formatActionResult(reqId, status, dataOrError);
    submitActionResult(formatted);
  }

  if (typeof document !== "undefined" && !root.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__) {
    setAdapterStatus("IDLE", "Adapter starting");
    registerControlPairingMenu();
    scheduleControlHeartbeat(250);
    schedule(runAdapter, 1000);
    schedule(function () {
      setInterval(scanAndProcessBrowserActions, 2000);
    }, 1500);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
