// ==UserScript==
// @name         DevOrchestrator ChatGPT Web binding adapter
// @namespace    devorchestrator
// @version      0.2.1
// @description  ChatGPT Web adapter for DevOrchestrator Browser Bridge plus the local Conversation Control Plane. Web Sol transport stays automatic; project binding changes require an explicit owner click.
// @author       DevOrchestrator
// @match        https://chatgpt.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
//
// Web Sol transport plus explicit local conversation-binding UI. This script
// never decides what happens next in a project workflow, never starts Workers,
// and never interprets repository or Worker state. Bind/rebind/unbind mutations
// occur only after a deliberate owner click in the local Control Plane panel.
// The binding id is always derived from the live ChatGPT conversation URL, so
// no project/conversation key is hard-coded here and each tab (conversation)
// claims only its own project queue. Assistant responses are acknowledged only
// after their marker text has been stable across the polling window.

(function (root) {
  "use strict";

  var BRIDGE_BASE = "http://127.0.0.1:8765";
  var CONTROL_BASE = "http://127.0.0.1:8766";
  var ADAPTER_ID = "chatgpt_web";
  var CONVERSATION_PATH_RE = /\/c\/([A-Za-z0-9_-]+)/;
  var REQUEST_HEAD = "[DEVORCH_WEB_SOL_REQUEST ";
  var RESPONSE_HEAD = "[DEVORCH_WEB_SOL_RESPONSE ";
  var CLAIM_PATH = "/v1/claim";
  var RENEW_PATH = "/v1/renew";
  var RESPONSE_PATH = "/v1/response";
  var HEARTBEAT_PATH = "/v1/session/heartbeat";
  var PROJECTS_PATH = "/v1/projects";
  var BIND_PATH = "/v1/bind";
  var REBIND_PATH = "/v1/rebind";
  var UNBIND_PATH = "/v1/unbind";
  var OWNER_ACTION_PATH = "/v1/owner-action";
  var CONTROL_HEARTBEAT_MS = 15000;
  var CONTROL_RETRY_MS = 5000;
  var TAB_INSTANCE_STORAGE_KEY = "devorch:control:tab-instance";
  var POLL_MS = 1500;
  var RENEW_INTERVAL_MS = 20000;
  var WAIT_DEADLINE_MS = 15 * 60 * 1000;
  var RETRY_MS = 2500;
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
  var PANEL_ELEMENT_ID = "devorch-control-panel";
  var controlHeartbeatTimer = null;
  var selectedProjectId = "";
  var transportStatus = { state: "IDLE", detail: "Adapter starting" };
  var controlStatus = {
    online: false,
    projectId: null,
    bindingState: "offline",
    bindingId: "",
    detail: "Control Plane starting",
    projects: [],
    session: null
  };

  function controlBadgeLabel(state) {
    state = state || {};
    var projectId = typeof state.projectId === "string" && state.projectId ? state.projectId : "";
    var bindingState = typeof state.bindingState === "string" && state.bindingState ? state.bindingState.toLowerCase() : "unbound";
    if (projectId) { return "DevOrch · " + projectId + " · " + bindingState.toUpperCase(); }
    if (bindingState === "offline") { return "DevOrch · CONTROL OFFLINE"; }
    if (bindingState === "stale") { return "DevOrch · STALE"; }
    return "DevOrch · UNBOUND";
  }

  function chooseBindingAction(project, bindingId) {
    if (!project || !project.orchestration_ready || !project.conversation_binding) { return "bind"; }
    var existing = project.conversation_binding.binding_id;
    if (existing === bindingId) { return "bound"; }
    return "rebind";
  }

  function wireStatusBadge(badge) {
    if (!badge || badge.getAttribute("data-devorch-click-bound") === "1") { return; }
    badge.setAttribute("data-devorch-click-bound", "1");
    badge.addEventListener("click", function (event) {
      if (event && event.stopPropagation) { event.stopPropagation(); }
      toggleControlPanel();
    });
  }

  function ensureStatusBadge() {
    if (typeof document === "undefined" || !document.body) { return null; }
    var badge = document.getElementById(STATUS_ELEMENT_ID);
    if (!badge) {
      badge = document.createElement("div");
      badge.id = STATUS_ELEMENT_ID;
      badge.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483647;padding:5px 9px;border-radius:7px;font:12px/1.2 system-ui,sans-serif;color:#fff;background:#555;box-shadow:0 1px 5px rgba(0,0,0,.25);pointer-events:auto;cursor:pointer;user-select:none;opacity:.92";
      document.body.appendChild(badge);
    }
    wireStatusBadge(badge);
    return badge;
  }

  function renderStatusBadge() {
    var badge = ensureStatusBadge();
    if (!badge) { return; }
    var bindingId = currentBindingId();
    var label;
    if (!bindingId) { label = "DevOrch · IDLE"; }
    else { label = controlBadgeLabel(controlStatus); }
    var state = (controlStatus.bindingState || "offline").toLowerCase();
    var colors = { bound: "#237a3b", unbound: "#555", stale: "#8a5a00", offline: "#a32929" };
    badge.textContent = label;
    badge.style.background = colors[state] || "#555";
    badge.setAttribute("data-state", state.toUpperCase());
    badge.title = (controlStatus.detail || "Control Plane") + " | Transport: " + transportStatus.state + " — " + (transportStatus.detail || "");
  }

  function setAdapterStatus(state, detail) {
    transportStatus.state = state;
    transportStatus.detail = detail || state;
    renderStatusBadge();
  }

  function ensureControlPanel() {
    if (typeof document === "undefined" || !document.body) { return null; }
    var panel = document.getElementById(PANEL_ELEMENT_ID);
    if (panel) { return panel; }
    panel = document.createElement("div");
    panel.id = PANEL_ELEMENT_ID;
    panel.style.cssText = "position:fixed;right:12px;bottom:48px;z-index:2147483646;width:320px;max-height:70vh;overflow:auto;padding:12px;border-radius:10px;font:12px/1.4 system-ui,sans-serif;color:#eee;background:#202123;box-shadow:0 5px 24px rgba(0,0,0,.38);display:none";
    panel.addEventListener("click", function (event) { if (event && event.stopPropagation) { event.stopPropagation(); } });
    document.body.appendChild(panel);
    return panel;
  }

  function appendPanelText(parent, text, cssText) {
    var row = document.createElement("div");
    row.textContent = text;
    if (cssText) { row.style.cssText = cssText; }
    parent.appendChild(row);
    return row;
  }

  function projectById(projectId) {
    var projects = controlStatus.projects || [];
    for (var i = 0; i < projects.length; i++) {
      if (projects[i] && projects[i].project_id === projectId) { return projects[i]; }
    }
    return null;
  }

  function renderControlPanel() {
    var panel = ensureControlPanel();
    if (!panel) { return; }
    while (panel.firstChild) { panel.removeChild(panel.firstChild); }

    var header = document.createElement("div");
    header.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-weight:600";
    appendPanelText(header, "DevOrchestrator Conversation", "font-size:13px");
    var close = document.createElement("button");
    close.textContent = "×";
    close.style.cssText = "border:0;background:transparent;color:#bbb;font-size:18px;cursor:pointer;padding:0 2px";
    close.addEventListener("click", function () { panel.style.display = "none"; });
    header.appendChild(close);
    panel.appendChild(header);

    var bindingId = currentBindingId();
    var title = controlStatus.session && controlStatus.session.title ? controlStatus.session.title : currentConversationTitle(bindingId);
    appendPanelText(panel, "Conversation: " + (title || "—"), "margin-bottom:3px;color:#ddd");
    appendPanelText(panel, "ID: " + (bindingId || "no stable /c/... URL"), "margin-bottom:3px;color:#aaa;word-break:break-all");
    appendPanelText(panel, "Current project: " + (controlStatus.projectId || "UNBOUND"), "margin-bottom:8px;color:#ddd");

    var projects = controlStatus.projects || [];
    if (!selectedProjectId && controlStatus.projectId) { selectedProjectId = controlStatus.projectId; }
    if (!projectById(selectedProjectId) && projects.length) { selectedProjectId = projects[0].project_id; }
    var select = document.createElement("select");
    select.style.cssText = "width:100%;margin-bottom:8px;padding:6px;background:#343541;color:#eee;border:1px solid #555;border-radius:6px";
    for (var i = 0; i < projects.length; i++) {
      var option = document.createElement("option");
      option.value = projects[i].project_id;
      option.textContent = projects[i].project_id + " · " + String(projects[i].binding_state || "unbound").toUpperCase();
      select.appendChild(option);
    }
    select.value = selectedProjectId;
    select.addEventListener("change", function () { selectedProjectId = select.value; renderControlPanel(); });
    panel.appendChild(select);

    var selected = projectById(selectedProjectId);
    var action = chooseBindingAction(selected, bindingId);
    var controls = document.createElement("div");
    controls.style.cssText = "display:flex;gap:7px;margin-bottom:8px";
    var bindButton = document.createElement("button");
    bindButton.textContent = action === "rebind" ? "Rebind" : (action === "bound" ? "Bound" : "Bind");
    bindButton.disabled = !bindingId || !selected || action === "bound" || !controlStatus.online;
    bindButton.style.cssText = "flex:1;padding:6px;border:1px solid #666;border-radius:6px;background:#343541;color:#eee;cursor:pointer";
    bindButton.addEventListener("click", function () {
      if (!bindButton.disabled) { controlMutation(action, selectedProjectId); }
    });
    controls.appendChild(bindButton);

    var unbindButton = document.createElement("button");
    unbindButton.textContent = "Unbind";
    unbindButton.disabled = !controlStatus.projectId || selectedProjectId !== controlStatus.projectId || !controlStatus.online;
    unbindButton.style.cssText = "flex:1;padding:6px;border:1px solid #666;border-radius:6px;background:#343541;color:#eee;cursor:pointer";
    unbindButton.addEventListener("click", function () {
      if (!unbindButton.disabled) { controlMutation("unbind", controlStatus.projectId); }
    });
    controls.appendChild(unbindButton);
    panel.appendChild(controls);

    var ownerBox = document.createElement("div");
    ownerBox.style.cssText = "margin:10px 0 8px;padding-top:9px;border-top:1px solid #45464f";
    appendPanelText(ownerBox, "Owner control", "font-weight:600;margin-bottom:5px");
    if (selected) {
      appendPanelText(ownerBox, "Project state: " + String(selected.project_state || "unknown"), "color:#aaa;margin-bottom:2px");
      appendPanelText(ownerBox, "Task: " + String(selected.current_task_id || "—"), "color:#aaa;margin-bottom:2px");
      appendPanelText(ownerBox, "Owner pause: " + (selected.owner_paused ? "PAUSED" : "running policy"), "color:#aaa;margin-bottom:5px");
      if (selected.owner_gate) {
        appendPanelText(ownerBox, "Gate: " + String(selected.owner_gate.request_id || "—") + " · " + String(selected.owner_gate.owner_state || "pending").toUpperCase(), "color:#d8b45a;margin-bottom:5px;word-break:break-all");
      }
    }
    var ownerControls = document.createElement("div");
    ownerControls.style.cssText = "display:grid;grid-template-columns:1fr 1fr;gap:7px";
    var sameOwnerProject = Boolean(selected && controlStatus.projectId === selected.project_id && controlStatus.bindingState === "bound");
    var approveButton = document.createElement("button");
    approveButton.textContent = "Approve next stage";
    approveButton.disabled = !sameOwnerProject || !selected.can_approve_next_stage || !controlStatus.online;
    approveButton.style.cssText = "padding:6px;border:1px solid #7c6a38;border-radius:6px;background:#343541;color:#eee;cursor:pointer";
    approveButton.addEventListener("click", function () {
      if (!approveButton.disabled) { ownerControlMutation("approve_next_stage", selected); }
    });
    ownerControls.appendChild(approveButton);
    var startButton = document.createElement("button");
    startButton.textContent = "Start current task";
    startButton.disabled = !sameOwnerProject || !selected.can_start_current_task || !controlStatus.online;
    startButton.style.cssText = "padding:6px;border:1px solid #4c6f50;border-radius:6px;background:#343541;color:#eee;cursor:pointer";
    startButton.addEventListener("click", function () {
      if (!startButton.disabled) { ownerControlMutation("start_current_task", selected); }
    });
    ownerControls.appendChild(startButton);
    var stopButton = document.createElement("button");
    stopButton.textContent = "Stop / Pause auto-starts";
    stopButton.disabled = !sameOwnerProject || !controlStatus.online;
    stopButton.style.cssText = "grid-column:1 / span 2;padding:6px;border:1px solid #7a4a4a;border-radius:6px;background:#343541;color:#eee;cursor:pointer";
    stopButton.addEventListener("click", function () {
      if (!stopButton.disabled) { ownerControlMutation("stop", selected); }
    });
    ownerControls.appendChild(stopButton);
    ownerBox.appendChild(ownerControls);
    panel.appendChild(ownerBox);
    appendPanelText(panel, "Control: " + (controlStatus.detail || "—"), "color:#aaa;margin-bottom:3px");
    appendPanelText(panel, "Transport: " + transportStatus.state + " — " + (transportStatus.detail || ""), "color:#888");
  }

  function toggleControlPanel() {
    var panel = ensureControlPanel();
    if (!panel) { return; }
    if (panel.style.display === "none" || !panel.style.display) {
      panel.style.display = "block";
      renderControlPanel();
      refreshControlProjects();
    } else {
      panel.style.display = "none";
    }
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

  function currentConversationTitle(bindingId) {
    if (typeof document !== "undefined" && typeof document.title === "string" && document.title.trim()) {
      return document.title.trim();
    }
    return bindingId ? "ChatGPT conversation " + bindingId : "ChatGPT conversation";
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
    controlBadgeLabel: controlBadgeLabel,
    chooseBindingAction: chooseBindingAction,
    tabInstanceId: tabInstanceId
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

  function controlRequest(method, path, payload) {
    var url = CONTROL_BASE + path;
    var body = payload === null || typeof payload === "undefined" ? null : JSON.stringify(payload);
    return new Promise(function (resolve) {
      var settled = false;
      function finish(status, text) {
        if (settled) { return; }
        settled = true;
        resolve({ status: status, text: text });
      }
      if (typeof GM_xmlhttpRequest === "function") {
        var options = {
          method: method,
          url: url,
          timeout: 8000,
          onload: function (response) { finish(response.status, response.responseText); },
          onerror: function () { finish(0, ""); },
          ontimeout: function () { finish(0, ""); }
        };
        if (body !== null) {
          options.data = body;
          options.headers = { "Content-Type": "application/json" };
        }
        GM_xmlhttpRequest(options);
        return;
      }
      if (typeof fetch === "function") {
        var fetchOptions = { method: method, headers: {} };
        if (body !== null) {
          fetchOptions.headers["Content-Type"] = "application/json";
          fetchOptions.body = body;
        }
        fetch(url, fetchOptions).then(function (response) {
          response.text().then(function (text) { finish(response.status, text); });
        }).catch(function () { finish(0, ""); });
        return;
      }
      finish(0, "");
    });
  }

  function scheduleControlHeartbeat(delayMs) {
    if (controlHeartbeatTimer !== null && typeof clearTimeout === "function") { clearTimeout(controlHeartbeatTimer); }
    controlHeartbeatTimer = setTimeout(controlHeartbeat, delayMs);
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

  function refreshControlProjects() {
    return controlRequest("GET", PROJECTS_PATH, null).then(function (result) {
      if (!result || result.status !== 200) {
        controlStatus.detail = "Project list failed: HTTP " + (result ? result.status : 0);
        renderStatusBadge();
        renderControlPanel();
        return false;
      }
      var body = parsedJsonText(result);
      controlStatus.projects = body && Array.isArray(body.projects) ? body.projects : [];
      if (controlStatus.projectId) {
        var current = projectById(controlStatus.projectId);
        if (current && typeof current.binding_state === "string") {
          controlStatus.bindingState = current.binding_state;
        }
      } else {
        controlStatus.bindingState = "unbound";
      }
      renderStatusBadge();
      renderControlPanel();
      return true;
    });
  }

  function controlHeartbeat() {
    var bindingId = currentBindingId();
    if (!bindingId) {
      controlStatus.online = false;
      controlStatus.projectId = null;
      controlStatus.bindingState = "unbound";
      controlStatus.bindingId = "";
      controlStatus.detail = "Waiting for a stable /c/<conversation-id> URL";
      renderStatusBadge();
      renderControlPanel();
      scheduleControlHeartbeat(CONTROL_RETRY_MS);
      return;
    }
    var payload = {
      adapter: ADAPTER_ID,
      binding_id: bindingId,
      title: currentConversationTitle(bindingId),
      url: String(window.location.href),
      tab_instance_id: tabInstanceId()
    };
    controlRequest("POST", HEARTBEAT_PATH, payload).then(function (result) {
      if (!result || result.status !== 200) {
        controlStatus.online = false;
        controlStatus.bindingState = "offline";
        controlStatus.bindingId = bindingId;
        controlStatus.detail = "Control Plane unavailable: HTTP " + (result ? result.status : 0);
        renderStatusBadge();
        renderControlPanel();
        scheduleControlHeartbeat(CONTROL_RETRY_MS);
        return;
      }
      var body = parsedJsonText(result) || {};
      controlStatus.online = true;
      controlStatus.bindingId = bindingId;
      controlStatus.projectId = typeof body.project_id === "string" && body.project_id ? body.project_id : null;
      controlStatus.bindingState = controlStatus.projectId ? "bound" : "unbound";
      controlStatus.session = body.session && typeof body.session === "object" ? body.session : null;
      controlStatus.detail = controlStatus.projectId ? "Bound to " + controlStatus.projectId : "Conversation is not bound to a project";
      renderStatusBadge();
      renderControlPanel();
      refreshControlProjects().then(function () { scheduleControlHeartbeat(CONTROL_HEARTBEAT_MS); });
    });
  }

  function controlMutation(action, projectId) {
    var bindingId = currentBindingId();
    if (!bindingId || !projectId || !controlStatus.online) { return; }
    var path;
    var payload = { project_id: projectId };
    if (action === "bind") {
      path = BIND_PATH;
      payload.adapter = ADAPTER_ID;
      payload.binding_id = bindingId;
    } else if (action === "rebind") {
      path = REBIND_PATH;
      payload.adapter = ADAPTER_ID;
      payload.binding_id = bindingId;
    } else if (action === "unbind") {
      path = UNBIND_PATH;
    } else {
      return;
    }
    controlStatus.detail = action + " requested for " + projectId;
    renderControlPanel();
    controlRequest("POST", path, payload).then(function (result) {
      if (result && result.status === 200) {
        selectedProjectId = projectId;
        controlStatus.detail = action + " accepted by daemon";
        scheduleControlHeartbeat(0);
        return;
      }
      var body = parsedJsonText(result);
      controlStatus.detail = body && body.message ? body.message : (action + " failed: HTTP " + (result ? result.status : 0));
      renderStatusBadge();
      renderControlPanel();
    });
  }

  function ownerControlMutation(action, project) {
    var bindingId = currentBindingId();
    if (!bindingId || !project || !controlStatus.online || controlStatus.projectId !== project.project_id) { return; }
    var payload = {
      project_id: project.project_id, action: action,
      action_id: "owner-" + newTabInstanceId(), adapter: ADAPTER_ID,
      binding_id: bindingId, expected_branch: project.branch, expected_head: project.head
    };
    if (action === "start_current_task") { payload.expected_task_id = project.current_task_id; }
    if (action === "approve_next_stage") {
      if (!project.owner_gate) { return; }
      payload.gate_request_id = project.owner_gate.request_id;
      payload.gate_task_id = project.owner_gate.task_id;
    }
    controlStatus.detail = "Owner " + action + " requested for " + project.project_id;
    renderControlPanel();
    controlRequest("POST", OWNER_ACTION_PATH, payload).then(function (result) {
      var body = parsedJsonText(result);
      if (result && result.status === 200) {
        controlStatus.detail = "Owner " + action + " accepted by daemon";
        refreshControlProjects();
        return;
      }
      controlStatus.detail = body && body.message ? body.message : ("Owner action failed: HTTP " + (result ? result.status : 0));
      renderStatusBadge(); renderControlPanel();
    });
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

  function insertAndSubmit(text) {
    if (typeof document === "undefined") {
      return false;
    }
    var composer = document.querySelector("#prompt-textarea");
    if (!composer) {
      return false;
    }
    setComposerText(composer, text);
    return clickSendButton();
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
      if (!insertAndSubmit(claim.prompt)) {
        // Composer not ready; do not record a submission that never happened.
        schedule(runAdapter, RETRY_MS);
        return;
      }
      // A send-button click is not proof of submission. Wait until ChatGPT
      // renders the exact request marker as a user turn before persisting the
      // submitted mark or waiting for an assistant response.
      setAdapterStatus("CLAIMED", "Send clicked; confirming user turn");
      confirmSubmission(bindingId, claim, Date.now() + SUBMISSION_CONFIRM_MS);
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
    scheduleControlHeartbeat(250);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
