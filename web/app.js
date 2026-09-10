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

async function refresh() {
  try {
    const [monitor, summary, eventsPayload, runsPayload, orchestration, brokerResources, brokerExecutions, brokerUsage] = await Promise.all([
      getJson('/api/monitor'), getJson('/api/summary'), getJson('/api/events?limit=30'), getJson('/api/runs?limit=20'), getJson('/api/orchestration'),
      getJson('/api/broker/resources'), getJson('/api/broker/executions'), getJson('/api/broker/usage')
    ]);
    const events = Array.isArray(eventsPayload.items) ? eventsPayload.items : [];
    const runs = Array.isArray(runsPayload.items) ? runsPayload.items : [];
    renderMonitor(monitor);
    renderOverview(summary);
    renderProjects(summary, events);
    renderBrokerResources(brokerResources);
    renderBrokerExecutions(brokerExecutions);
    renderBrokerUsage(brokerUsage);
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

refresh();
setInterval(refresh, 10000);
