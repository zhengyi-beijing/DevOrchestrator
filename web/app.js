'use strict';

const $ = (id) => document.getElementById(id);
const UNKNOWN = '—';

function text(value) {
  if (value === null || value === undefined || value === '') return UNKNOWN;
  return String(value);
}

function seconds(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return UNKNOWN;
  const s = Math.max(0, Math.round(Number(value)));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function ratio(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return UNKNOWN;
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function ago(iso) {
  if (!iso) return UNKNOWN;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return text(iso);
  return `${seconds((Date.now() - t) / 1000)} ago`;
}

function shortHead(value) {
  const s = text(value);
  return s === UNKNOWN ? s : s.slice(0, 9);
}
function stateTone(state, health) {
  const s = String(state || '').toUpperCase();
  const h = String(health || '').toUpperCase();
  if (h === 'TIMEOUT' || s.includes('FAILED') || s.includes('LOST') || s.includes('BLOCKED') || s.includes('ERROR')) return 'bad';
  if (h === 'STALLED_WARNING' || s.includes('REVIEW') || s.includes('GATE')) return 'warn';
  if (s.includes('RUNNING') || s.includes('READY')) return 'info';
  if (s.includes('ACCEPTED') || s === 'IDLE') return 'ok';
  return 'info';
}

function kv(container, key, value, mono = false) {
  const item = document.createElement('div');
  item.className = 'kv';
  const k = document.createElement('div');
  k.className = 'k';
  k.textContent = key;
  const v = document.createElement('div');
  v.className = mono ? 'v mono' : 'v';
  v.textContent = text(value);
  item.append(k, v);
  container.appendChild(item);
}

async function getJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

function renderMonitor(monitor) {
  const badge = $('monitorBadge');
  const stale = Boolean(monitor.stale);
  badge.className = `badge ${stale ? 'bad' : monitor.state === 'degraded' ? 'warn' : 'ok'}`;
  badge.textContent = stale ? 'Monitor STALE' : monitor.state === 'degraded' ? 'Monitor degraded' : 'Monitor healthy';
  const box = $('monitorDetails');
  box.replaceChildren();
  kv(box, 'State', monitor.state);
  kv(box, 'PID', monitor.pid, true);
  kv(box, 'Process', monitor.process_alive ? 'alive' : 'not alive');
  kv(box, 'Heartbeat age', seconds(monitor.heartbeat_age_seconds));
  kv(box, 'Interval', seconds(monitor.interval_seconds));
  kv(box, 'Last error', monitor.last_error || 'none');
}
function projectState(project) {
  return project.lifecycle_state || project.state;
}

function renderOverview(summary) {
  const box = $('overviewDetails');
  box.replaceChildren();
  const projects = Array.isArray(summary.projects) ? summary.projects : [];
  kv(box, 'Projects', summary.project_count ?? projects.length);
  kv(box, 'Observed', ago(summary.observed_at));
  kv(box, 'Running', projects.filter(p => projectState(p) === 'EXECUTING' || projectState(p) === 'WORKER_RUNNING').length);
  kv(box, 'Review / gate', projects.filter(p => String(projectState(p)).includes('REVIEW') || String(projectState(p)).includes('GATE')).length);
  kv(box, 'Blocked / failed', projects.filter(p => /BLOCKED|FAILED|LOST|ERROR|RECOVERY/.test(String(projectState(p)))).length);
  kv(box, 'Healthy telemetry', projects.filter(p => p.telemetry && p.telemetry.health === 'OK').length);
}

function projectEta(project) {
  const eta = project.telemetry && project.telemetry.eta;
  if (!eta) return UNKNOWN;
  const lo = seconds(eta.remaining_min_seconds);
  const hi = seconds(eta.remaining_max_seconds);
  return `${lo} – ${hi}`;
}

function renderProjects(summary, events) {
  const host = $('projects');
  host.replaceChildren();
  const projects = Array.isArray(summary.projects) ? summary.projects : [];
  const latest = new Map();
  for (const ev of events) latest.set(ev.project_id, ev);
  if (!projects.length) {
    const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'No registered project snapshots.'; host.appendChild(empty); return;
  }
  for (const project of projects) {
    const card = document.createElement('article');
    card.className = `project-card ${stateTone(projectState(project), project.telemetry && project.telemetry.health)}`;
    const head = document.createElement('div'); head.className = 'project-head';
    const nameBox = document.createElement('div');
    const name = document.createElement('div'); name.className = 'project-name'; name.textContent = text(project.name || project.id);
    const state = document.createElement('div'); state.className = 'project-state'; state.textContent = text(projectState(project));
    nameBox.append(name, state);
    const health = document.createElement('div');
    health.className = `badge ${stateTone(projectState(project), project.telemetry && project.telemetry.health)}`;
    health.textContent = text(project.telemetry && project.telemetry.health);
    head.append(nameBox, health); card.appendChild(head);
    const next = document.createElement('div'); next.className = 'project-next';
    const nextLabel = document.createElement('div'); nextLabel.className = 'label'; nextLabel.textContent = 'Current gate / next';
    const nextValue = document.createElement('div'); nextValue.className = 'value'; nextValue.textContent = text(project.next_title || project.phase_hint);
    next.append(nextLabel, nextValue); card.appendChild(next);

    const grid = document.createElement('div'); grid.className = 'kv-grid';
    kv(grid, 'Git', `${text(project.git && project.git.branch)} @ ${shortHead(project.git && project.git.head)}`, true);
    kv(grid, 'Workspace', project.git ? (project.git.dirty ? `dirty · ${project.git.changed_entries}` : 'clean') : UNKNOWN);
    kv(grid, 'Worker', project.worker ? `${text(project.worker.state)} · PID ${text(project.worker.pid)}` : UNKNOWN);
    kv(grid, 'Task', project.telemetry && project.telemetry.task_id, true);
    kv(grid, 'Elapsed', seconds(project.telemetry && project.telemetry.elapsed_seconds));
    kv(grid, 'ETA remaining', projectEta(project));
    kv(grid, 'ETA source', project.telemetry && project.telemetry.eta ? `${text(project.telemetry.eta.source)} / ${text(project.telemetry.eta.confidence)}` : UNKNOWN);
    kv(grid, 'Last activity', ago(project.last_activity_at));
    const ev = latest.get(project.id);
    kv(grid, 'Latest transition', ev ? `${text(ev.from_state)} → ${text(ev.to_state)}` : UNKNOWN);
    kv(grid, 'Observed', ago(project.observed_at));
    card.appendChild(grid);
    host.appendChild(card);
  }
}

function renderTimeline(hostId, items, formatter) {
  const host = $(hostId); host.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'No history yet.'; host.appendChild(empty); return;
  }
  for (const item of items.slice().reverse().slice(0, 10)) {
    const row = document.createElement('div'); row.className = 'timeline-item';
    const main = document.createElement('div'); main.textContent = formatter(item);
    const meta = document.createElement('div'); meta.className = 'meta'; meta.textContent = `${text(item.project_id)} · ${ago(item.observed_at || item.recorded_at)}`;
    row.append(main, meta); host.appendChild(row);
  }
}

function renderOrchestration(orchestration) {
  const host = $('orchestration');
  host.replaceChildren();
  const entries = (orchestration && orchestration.projects) || {};
  const ids = Object.keys(entries).sort();
  if (!ids.length) {
    const empty = document.createElement('div'); empty.className = 'empty';
    empty.textContent = 'No pending orchestration requests.';
    host.appendChild(empty);
    return;
  }
  for (const id of ids) {
    const entry = entries[id];
    const card = document.createElement('article');
    card.className = `project-card ${entry.state === 'UNBOUND' ? 'bad' : 'ok'}`;
    const head = document.createElement('div'); head.className = 'project-head';
    const nameBox = document.createElement('div');
    const name = document.createElement('div'); name.className = 'project-name'; name.textContent = text(id);
    const state = document.createElement('div'); state.className = 'project-state'; state.textContent = `${text(entry.state)} / BLOCKED`;
    nameBox.append(name, state);
    const badge = document.createElement('div'); badge.className = 'badge bad'; badge.textContent = entry.state === 'UNBOUND' ? 'UNBOUND / BLOCKED' : text(entry.state);
    head.append(nameBox, badge); card.appendChild(head);
    const next = document.createElement('div'); next.className = 'project-next';
    const nextLabel = document.createElement('div'); nextLabel.className = 'label'; nextLabel.textContent = 'Resume path';
    const nextValue = document.createElement('div'); nextValue.className = 'value';
    nextValue.textContent = 'Rebind the ChatGPT conversation to resume the pending request.';
    next.append(nextLabel, nextValue); card.appendChild(next);
    const grid = document.createElement('div'); grid.className = 'kv-grid';
    kv(grid, 'Delivery', text(entry.delivery_state));
    kv(grid, 'Request', entry.request_id, true);
    kv(grid, 'Binding', text(entry.binding_id), true);
    kv(grid, 'Prepared', ago(entry.prepared_at));
    card.appendChild(grid);
    host.appendChild(card);
  }
}

function renderBrokerResources(payload) {
  const host = $('brokerResources'); host.replaceChildren();
  if (!payload.available) { const e=document.createElement('div'); e.className='empty'; e.textContent=`AIBroker unavailable: ${text(payload.error)}`; host.appendChild(e); return; }
  for (const r of (payload.resources || [])) {
    const card=document.createElement('article'); card.className=`project-card ${r.enabled && r.state && r.state.available ? 'ok' : 'warn'}`;
    const h=document.createElement('div'); h.className='project-name'; h.textContent=r.resource_id; card.appendChild(h);
    const g=document.createElement('div'); g.className='kv-grid'; kv(g,'Provider',r.provider); kv(g,'Account',r.account); kv(g,'Model',r.model); kv(g,'Enabled',r.enabled); kv(g,'Available',r.state && r.state.available); kv(g,'Health',r.state && r.state.health); card.appendChild(g); host.appendChild(card);
  }
}
function renderBrokerExecutions(payload) {
  const items=payload.available ? (payload.executions || []) : [];
  renderTimeline('brokerExecutions', items, e => `${text(e.role)} · ${text(e.resource_id)} · ${text(e.status)} · ${seconds(e.duration_seconds)}`);
}
function renderBrokerUsage(payload) {
  const host=$('brokerUsage'); host.replaceChildren();
  if (!payload.available) { const e=document.createElement('div'); e.className='empty'; e.textContent=`AIBroker unavailable: ${text(payload.error)}`; host.appendChild(e); return; }
  for (const u of (payload.usage || [])) { const row=document.createElement('div'); row.className='timeline-item'; row.textContent=`${text(u.resource_id)} · executions ${text(u.executions)} · tokens ${u.token_source === 'reported' ? text(u.total_tokens) : 'unknown'}`; host.appendChild(row); }
  if (!(payload.usage || []).length) { const e=document.createElement('div'); e.className='empty'; e.textContent='No usage records yet.'; host.appendChild(e); }
}

let controlCsrf = null;

async function ensureControlSession() {
  if (controlCsrf) return controlCsrf;
  const response = await fetch('/api/v1/control/browser-sessions', {
    method: 'POST', credentials: 'same-origin', headers: {'Sec-Fetch-Site': 'same-origin'}
  });
  if (!response.ok) throw new Error(`control session: HTTP ${response.status}`);
  const payload = await response.json();
  controlCsrf = payload.data.csrf_token;
  return controlCsrf;
}

async function pollControlCommand(commandId, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const payload = await getJson(`/api/v1/control/commands/${encodeURIComponent(commandId)}`);
    if (payload.data && payload.data.state !== 'pending') return payload.data;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  return {command_id: commandId, state: 'pending', reason: 'daemon result is still pending'};
}

async function sendControl(project, action, target = {}) {
  if ((action === 'stop' || action === 'approve_owner_gate') &&
      !window.confirm(`Confirm ${action} for ${text(project.project_id || project.id)}?`)) return null;
  const csrf = await ensureControlSession();
  const response = await fetch('/api/v1/control/commands', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-DevOrch-CSRF': csrf, 'Sec-Fetch-Site': 'same-origin'},
    body: JSON.stringify({
      schema_version: 1,
      command_id: `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      project_id: project.project_id || project.id,
      action,
      expected: project.control_identity,
      target,
    }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `control command: HTTP ${response.status}`);
  return payload.data && payload.data.state === 'pending'
    ? pollControlCommand(payload.data.command_id)
    : payload.data;
}

function renderControlSurface(payload) {
  const data = payload && payload.data || {};
  const projectsHost = $('controlProjects'); projectsHost.replaceChildren();
  $('controlStatus').textContent = data.control_enabled ? 'guarded actions enabled' : 'observation only';
  for (const project of (data.projects || [])) {
    const card = document.createElement('article'); card.className = `project-card ${stateTone(projectState(project))}`;
    const head = document.createElement('div'); head.className = 'project-head';
    const title = document.createElement('div'); title.className = 'project-name'; title.textContent = text(project.name || project.project_id || project.id);
    const badge = document.createElement('div'); badge.className = `badge ${project.owner_control && project.owner_control.paused ? 'warn' : 'ok'}`;
    badge.textContent = project.owner_control && project.owner_control.paused ? 'PAUSED' : text(projectState(project));
    head.append(title, badge); card.appendChild(head);
    const grid = document.createElement('div'); grid.className = 'kv-grid';
    kv(grid, 'Repository', project.repo_path, true);
    kv(grid, 'Task', project.control_identity && project.control_identity.task_id, true);
    kv(grid, 'Branch / HEAD', project.control_identity ? `${text(project.control_identity.branch)} @ ${shortHead(project.control_identity.head)}` : UNKNOWN, true);
    kv(grid, 'Owner gate', project.control_identity && project.control_identity.gate_id, true);
    kv(grid, 'Active role', project.active_execution && (project.active_execution.role || project.active_execution.engine));
    kv(grid, 'Binding', project.conversation && project.conversation.binding ? `${text(project.conversation.binding.adapter)} / ${text(project.conversation.binding.binding_id)}` : 'unbound');
    card.appendChild(grid);
    const controls = document.createElement('div'); controls.className = 'control-actions';
    const targetSelect = document.createElement('select'); targetSelect.className = 'control-target';
    const currentBindingId = project.conversation && project.conversation.binding && project.conversation.binding.binding_id;
    for (const session of (data.sessions || []).filter(item => item.state === 'live' && item.binding_id !== currentBindingId)) {
      const option = document.createElement('option');
      option.value = JSON.stringify({adapter: session.adapter, binding_id: session.binding_id});
      option.textContent = `${text(session.title)} · ${text(session.adapter)} / ${text(session.binding_id)}`;
      targetSelect.appendChild(option);
    }
    if (targetSelect.children.length) controls.appendChild(targetSelect);
    for (const capability of (project.controls || [])) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = capability.action;
      button.disabled = !data.control_enabled || !capability.available;
      button.title = capability.reason || '';
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const needsTarget = capability.action === 'bind_conversation' || capability.action === 'rebind_conversation';
          const target = needsTarget && targetSelect.value ? JSON.parse(targetSelect.value) : {};
          const result = await sendControl(project, capability.action, target);
          if (result) { button.textContent = `${capability.action}: ${result.state}`; await refresh(); }
        } catch (error) { button.textContent = `${capability.action}: failed`; $('controlStatus').textContent = String(error.message || error); }
        finally { button.disabled = !data.control_enabled || !capability.available; }
      });
      controls.appendChild(button);
    }
    card.appendChild(controls); projectsHost.appendChild(card);
  }
  if (!(data.projects || []).length) { const e=document.createElement('div'); e.className='empty'; e.textContent='No project control projections.'; projectsHost.appendChild(e); }
  const bindings = $('controlBindings'); bindings.replaceChildren();
  for (const item of (data.bindings || [])) { const row=document.createElement('div'); row.className='timeline-item'; row.textContent=`${text(item.project_id)} · ${text(item.state)} · ${text(item.adapter)} / ${text(item.binding_id)}`; bindings.appendChild(row); }
  if (!(data.bindings || []).length) { const e=document.createElement('div'); e.className='empty'; e.textContent='No runtime bindings.'; bindings.appendChild(e); }
  renderTimeline('controlCommands', data.commands || [], item => `${text(item.action)} · ${text(item.state)}${item.reason ? ` · ${item.reason}` : ''}`);
}

function renderAccounting(payload) {
  const summary = $('accountingSummary'); summary.replaceChildren();
  const bottleneck = $('accountingBottleneck'); bottleneck.replaceChildren();
  const provider = $('providerEvidence'); provider.replaceChildren();
  const rdc = $('rdcEvidence'); rdc.replaceChildren();
  const hypotheses = $('hypothesisEvidence'); hypotheses.replaceChildren();
  const breakdown = $('scopeBreakdown'); breakdown.replaceChildren();
  const gates = $('acceptanceGates'); gates.replaceChildren();
  const warnings = $('evidenceWarnings'); warnings.replaceChildren();
  if (!payload.available) {
    for (const host of [summary, bottleneck, provider, rdc, hypotheses, breakdown, gates, warnings]) {
      const item = document.createElement('div'); item.className = 'empty';
      item.textContent = `Execution evidence unavailable: ${text(payload.error)}`;
      host.appendChild(item);
    }
    return;
  }
  const status = payload.data_status || {};
  const accounting = payload.accounting || {};
  const phases = accounting.duration_by_phase || {};
  kv(summary, 'Accounting evidence', status.accounting);
  kv(summary, 'EDR', status.accounting === 'measured' ? ratio(accounting.edr) : 'unavailable');
  kv(summary, 'Accepted productive', status.accounting === 'measured' ? seconds(accounting.accepted_productive_seconds) : 'unavailable');
  kv(summary, 'Rejected work', status.accounting === 'measured' ? seconds(accounting.rejected_attempt_seconds) : 'unavailable');
  kv(summary, 'Owner wait', status.accounting === 'measured' ? seconds(accounting.owner_wait_seconds) : 'unavailable');
  kv(summary, 'Retry wall time', status.accounting === 'measured' ? seconds(accounting.retry_wall_time_seconds) : 'unavailable');
  kv(summary, 'Idle', status.accounting === 'measured' ? seconds(phases.idle) : 'unavailable');
  kv(summary, 'Plan-review churn', status.accounting === 'measured' ? accounting.plan_review_churn : 'unavailable');
  kv(summary, 'Inference', payload.inference);

  const dominant = payload.dominant_bottleneck;
  if (dominant) {
    kv(bottleneck, 'Kind', dominant.kind);
    kv(bottleneck, 'Lost time', seconds(dominant.lost_seconds));
    kv(bottleneck, 'Provenance', dominant.provenance);
    kv(bottleneck, 'Evidence', (dominant.evidence_ids || []).join(', '), true);
    kv(bottleneck, 'Action', dominant.recommendation);
  } else {
    const item = document.createElement('div'); item.className = 'empty';
    item.textContent = 'No measured bottleneck for this window.'; bottleneck.appendChild(item);
  }

  const providerData = payload.provider || {};
  kv(provider, 'Evidence', status.provider);
  kv(provider, 'Results', status.provider === 'measured' ? providerData.result_count : 'unavailable');
  kv(provider, 'Context hits', status.provider === 'measured' ? providerData.context_hits : 'unavailable');
  kv(provider, 'Context switches', status.provider === 'measured' ? providerData.context_switches : 'unavailable');
  kv(provider, 'Resource switches', status.provider === 'measured' ? providerData.resource_switches : 'unavailable');
  kv(provider, 'Failover latency', status.provider === 'measured' ? seconds(providerData.failover_latency_seconds) : 'unavailable');
  kv(provider, 'Quota observations', status.provider === 'measured' ? providerData.quota_observation_count : 'unavailable');
  kv(provider, 'Rate limits', status.provider === 'measured' ? providerData.rate_limit_observation_count : 'unavailable');

  const classifications = payload.rdc && payload.rdc.classifications || [];
  if (status.rdc !== 'measured') {
    const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'RDC evidence unavailable.'; rdc.appendChild(item);
  } else if (!classifications.length) {
    const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'No RDC contention classified.'; rdc.appendChild(item);
  } else {
    for (const finding of classifications) {
      const row = document.createElement('div'); row.className = 'timeline-item';
      const main = document.createElement('div'); main.textContent = text(finding.kind);
      const meta = document.createElement('div'); meta.className = 'meta';
      meta.textContent = `${(finding.project_ids || []).join(', ')} · ${(finding.invocation_ids || []).join(', ')}`;
      row.append(main, meta); rdc.appendChild(row);
    }
  }

  for (const [name, hypothesis] of Object.entries(payload.hypotheses || {})) {
    const row = document.createElement('div'); row.className = 'timeline-item';
    const main = document.createElement('div');
    const result = hypothesis.observed === null || hypothesis.observed === undefined
      ? 'UNAVAILABLE' : (hypothesis.observed ? 'OBSERVED' : 'NOT OBSERVED');
    main.textContent = `${text(name)} · ${result}`;
    const meta = document.createElement('div'); meta.className = 'meta';
    const evidence = (hypothesis.evidence_ids || []).join(', ') || 'no evidence ids';
    meta.textContent = `${text(hypothesis.status)} · ${evidence}`;
    row.append(main, meta); hypotheses.appendChild(row);
  }
  if (!Object.keys(payload.hypotheses || {}).length) {
    const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'No hypothesis comparison.'; hypotheses.appendChild(item);
  }

  for (const item of (payload.time_breakdown || [])) {
    const row = document.createElement('div'); row.className = 'timeline-item';
    const main = document.createElement('div');
    main.textContent = `${text(item.project_id)} / ${text(item.task_id)} / ${text(item.role)}`;
    const meta = document.createElement('div'); meta.className = 'meta';
    meta.textContent = `EDR ${ratio(item.edr)} · accepted ${seconds(item.accepted_productive_seconds)} · rejected ${seconds(item.rejected_attempt_seconds)} · ${text(item.provenance)}`;
    row.append(main, meta); breakdown.appendChild(row);
  }
  if (!(payload.time_breakdown || []).length) {
    const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'No measured project/task/role time.'; breakdown.appendChild(item);
  }

  const acceptance = payload.acceptance || {};
  for (const gate of (acceptance.gates || [])) {
    const row = document.createElement('div'); row.className = `timeline-item gate-${text(gate.state)}`;
    const main = document.createElement('div'); main.textContent = `${text(gate.name)} · ${text(gate.state).toUpperCase()}`;
    const meta = document.createElement('div'); meta.className = 'meta';
    meta.textContent = `${text(gate.actual)} ${text(gate.operator)} ${text(gate.threshold)} · ${text(gate.provenance)}`;
    row.append(main, meta); gates.appendChild(row);
  }
  if (!(acceptance.gates || []).length) {
    const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'No acceptance gates.'; gates.appendChild(item);
  }
  for (const warning of (payload.warnings || [])) {
    const row = document.createElement('div'); row.className = 'timeline-item'; row.textContent = text(warning); warnings.appendChild(row);
  }
  if (!(payload.warnings || []).length) {
    const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'No evidence warnings.'; warnings.appendChild(item);
  }
}

async function refresh() {
  try {
    const [monitor, summary, eventsPayload, runsPayload, orchestration, brokerResources, brokerExecutions, brokerUsage, accounting, control] = await Promise.all([
      getJson('/api/monitor'), getJson('/api/summary'), getJson('/api/events?limit=30'), getJson('/api/runs?limit=20'), getJson('/api/orchestration'),
      getJson('/api/broker/resources'), getJson('/api/broker/executions'), getJson('/api/broker/usage'), getJson('/api/accounting'), getJson('/api/v1/control/overview')
    ]);
    const events = Array.isArray(eventsPayload.items) ? eventsPayload.items : [];
    const runs = Array.isArray(runsPayload.items) ? runsPayload.items : [];
    renderMonitor(monitor);
    renderOverview(summary);
    renderProjects(summary, events);
    renderBrokerResources(brokerResources);
    renderBrokerExecutions(brokerExecutions);
    renderBrokerUsage(brokerUsage);
    renderAccounting(accounting);
    renderControlSurface(control);
    renderOrchestration(orchestration);
    renderTimeline('events', events, e => `${text(e.from_state)} → ${text(e.to_state)} · ${text(e.task_id)}`);
    renderTimeline('runs', runs, r => `${text(r.task_id)} · ${text(r.result)} · ${seconds(r.duration_seconds)}`);
    $('lastRefresh').textContent = `refreshed ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    const badge = $('monitorBadge');
    badge.className = 'badge bad';
    badge.textContent = 'Dashboard data unavailable';
    $('lastRefresh').textContent = String(error.message || error);
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { renderAccounting, renderControlSurface, sendControl };
}
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  refresh();
  setInterval(refresh, 10000);
}
