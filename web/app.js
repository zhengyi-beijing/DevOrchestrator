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
  if (h === 'TIMEOUT' || /REVIEW_FAILED|FAILED|DEAD|LOST|BLOCKED|ERROR|CRITICAL/.test(s)) return 'bad';
  if (h === 'STALLED_WARNING' || /STALL|REVIEW|GATE|WARN|DEGRADED|RECOVERY/.test(s)) return 'warn';
  if (/RUNNING|READY|EXECUTING|PLANNING/.test(s)) return 'info';
  if (/ACCEPTED|COMPLETED|IDLE|OK/.test(s)) return 'ok';
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

function projectState(project) {
  if (!project) return UNKNOWN;
  return project.lifecycle_state || project.state;
}

function severityRank(status) {
  const s = String(status || '').toUpperCase();
  if (/REVIEW_FAILED|FAILED|DEAD|ERROR|LOST|CRITICAL/.test(s)) return 0;
  if (/STALL|TIMEOUT|GATE|WARN|DEGRADED|RECOVERY/.test(s)) return 1;
  if (/EXECUTING|WORKER_RUNNING|RUNNING|PLANNING|REVIEWING|REMEDIATING/.test(s)) return 2;
  if (/READY|PAUSED/.test(s)) return 3;
  if (/ACCEPTED|COMPLETED|IDLE|OK/.test(s)) return 4;
  return 5;
}

function compareSeverityThenIdThenTime(a, b) {
  const sA = severityRank(a.severity ?? a.status ?? a.lifecycle_state ?? a.state);
  const sB = severityRank(b.severity ?? b.status ?? b.lifecycle_state ?? b.state);
  if (sA !== sB) return sA - sB;
  const idA = String(a.id ?? a.project_id ?? a.resource_id ?? a.task_id ?? '');
  const idB = String(b.id ?? b.project_id ?? b.resource_id ?? b.task_id ?? '');
  const idCmp = idA.localeCompare(idB);
  if (idCmp !== 0) return idCmp;
  const tA = Date.parse(a.observed_at || a.recorded_at || a.timestamp || a.updated_at || 0) || 0;
  const tB = Date.parse(b.observed_at || b.recorded_at || b.timestamp || b.updated_at || 0) || 0;
  return tB - tA;
}

function computeFreshnessState(monitor, overview, fetchFailed = false) {
  if (fetchFailed) return 'Disconnected';
  if (!monitor && !overview) return 'Unknown';
  if (!monitor || monitor.available === false || monitor.process_alive === false) return 'Disconnected';
  if (overview && overview.available === false) return 'Disconnected';
  if (monitor.stale) return 'Stale';
  if (overview && overview.stale) return 'Stale';
  if (monitor.state === 'running') return 'Live';
  if (overview && overview.observed_at) return 'Live';
  return 'Unknown';
}

function computeIncidentCount(summary, controlOverview, watchdogData) {
  const sum = summary && summary.available !== false ? summary : null;
  const ctrl = controlOverview && controlOverview.available !== false ? controlOverview : null;
  const wd = (watchdogData && watchdogData.available !== false)
    ? (watchdogData.data || watchdogData)
    : (ctrl && ctrl.data && ctrl.data.watchdog ? ctrl.data.watchdog : null);

  if (!sum && !ctrl && !wd) return 'unavailable';

  const projects = Array.isArray(sum && sum.projects)
    ? sum.projects
    : (ctrl && ctrl.data && Array.isArray(ctrl.data.projects)
        ? ctrl.data.projects
        : null);

  if (!projects && !wd) return 'unavailable';

  let count = 0;
  if (projects) {
    for (const p of projects) {
      const state = String(p.lifecycle_state || p.state || '').toUpperCase();
      const health = String(p.telemetry && p.telemetry.health || '').toUpperCase();
      const wdState = p.watchdog && (p.watchdog.state || p.watchdog.diagnostic_code);
      const isIncident = /REVIEW_FAILED|BLOCKED|STALL|FAIL|LOST|ERROR|RECOVERY|GATE/.test(state) ||
                         /TIMEOUT|STALLED_WARNING/.test(health) ||
                         (wdState && wdState !== 'ok' && wdState !== 'healthy_slow');
      if (isIncident) count += 1;
    }
  }

  if (wd && wd.degraded) {
    count += 1;
  }
  if (wd && wd.projects && typeof wd.projects === 'object') {
    for (const [pid, entry] of Object.entries(wd.projects)) {
      if (entry && entry.state && entry.state !== 'ok' && entry.state !== 'healthy_slow') {
        if (!projects || !projects.some(p => (p.id || p.project_id) === pid)) {
          count += 1;
        }
      }
    }
  }
  return count;
}

function computeKPIs({ summary, control, brokerResources, brokerExecutions, watchdog, monitor } = {}) {
  const sum = summary && summary.available !== false ? summary : null;
  const ctrl = control && control.available !== false ? control : null;
  const summaryProjects = sum && Array.isArray(sum.projects) ? sum.projects : null;
  const controlProjects = ctrl && ctrl.data && Array.isArray(ctrl.data.projects) ? ctrl.data.projects : null;
  const projects = summaryProjects || controlProjects;

  const wd = (watchdog && watchdog.available !== false)
    ? (watchdog.data || watchdog)
    : (ctrl && ctrl.data && ctrl.data.watchdog ? ctrl.data.watchdog : null);

  let projectsKpi = 'unavailable';
  let rolesKpi = 'unavailable';
  let incidentsKpi = 'unavailable';

  if (projects !== null) {
    const total = sum && sum.project_count !== undefined ? sum.project_count : projects.length;
    const active = projects.filter(p => {
      const s = String(p.lifecycle_state || p.state || '').toUpperCase();
      return /EXECUTING|WORKER_RUNNING|RUNNING|PLANNING|REVIEWING|REMEDIATING/.test(s);
    }).length;
    projectsKpi = `${active} / ${total}`;

    let activeRolesCount = 0;
    for (const p of projects) {
      if (Array.isArray(p.active_roles) && p.active_roles.length) {
        activeRolesCount += p.active_roles.length;
      } else {
        const s = String(p.lifecycle_state || p.state || '').toUpperCase();
        if (/EXECUTING|WORKER_RUNNING/.test(s)) activeRolesCount += 1;
        else if (/REVIEW/.test(s)) activeRolesCount += 1;
        else if (/PLANNING/.test(s)) activeRolesCount += 1;
      }
    }
    rolesKpi = String(activeRolesCount);
    incidentsKpi = String(computeIncidentCount(sum, ctrl, wd));
  } else if (wd) {
    incidentsKpi = String(computeIncidentCount(sum, ctrl, wd));
  }

  let executionsKpi = 'unavailable';
  if (brokerExecutions && brokerExecutions.available) {
    const execs = Array.isArray(brokerExecutions.executions) ? brokerExecutions.executions : [];
    const succ = execs.filter(e => String(e.status).toLowerCase() === 'succeeded' || String(e.status).toLowerCase() === 'completed').length;
    const fail = execs.filter(e => String(e.status).toLowerCase() === 'failed' || String(e.status).toLowerCase() === 'interrupted').length;
    executionsKpi = `${succ} succ · ${fail} fail`;
  }

  let resourcesKpi = 'unavailable';
  if (brokerResources && brokerResources.available) {
    const res = Array.isArray(brokerResources.resources) ? brokerResources.resources : [];
    const avail = res.filter(r => r.enabled && r.state && r.state.available).length;
    const degraded = res.filter(r => r.enabled && r.state && !r.state.available && r.state.health === 'degraded').length;
    const unavail = res.length - avail - degraded;
    resourcesKpi = `${avail} avail / ${degraded} deg / ${unavail} unavail`;
  }

  return {
    projects: projectsKpi,
    roles: rolesKpi,
    incidents: incidentsKpi,
    executions: executionsKpi,
    resources: resourcesKpi,
    freshness: computeFreshnessState(
      monitor,
      summary,
      Boolean((monitor && monitor.available === false) || (summary && !monitor))
    ),
  };
}

function buildControlTarget(project, capability, selectedSession) {
  const action = (capability && capability.action) || (typeof capability === 'string' ? capability : null);
  if (!action) return null;

  switch (action) {
    case 'continue':
    case 'pause':
    case 'resume':
    case 'stop':
    case 'unbind_conversation':
      return {};

    case 'retry':
    case 'rereview':
    case 'reconcile': {
      const targetId = capability && capability.target_id;
      if (targetId && String(targetId).trim()) {
        return { target_id: String(targetId).trim() };
      }
      return null;
    }

    case 'approve_owner_gate': {
      const gateId = project && project.control_identity && project.control_identity.gate_id;
      if (gateId && String(gateId).trim()) {
        return { gate_id: String(gateId).trim() };
      }
      return null;
    }

    case 'bind_conversation':
    case 'rebind_conversation': {
      let session = selectedSession;
      if (typeof session === 'string') {
        try { session = JSON.parse(session); } catch (_) { session = null; }
      }
      if (session && session.adapter && session.binding_id &&
          String(session.adapter).trim() && String(session.binding_id).trim()) {
        return {
          adapter: String(session.adapter).trim(),
          binding_id: String(session.binding_id).trim(),
        };
      }
      return null;
    }

    default:
      return null;
  }
}

function describeGuardedAction(project, capability, target) {
  const action = (capability && capability.action) || (typeof capability === 'string' ? capability : 'action');
  const identity = (project && project.control_identity) || {};
  const projectId = identity.project_id || (project && (project.project_id || project.id)) || 'unavailable';
  const taskId = identity.task_id || (project && project.task_id) || 'unavailable';
  const lifecycle = identity.lifecycle_state || (project && (project.lifecycle_state || project.state)) || 'unavailable';
  const branch = identity.branch || (project && project.git && project.git.branch) || null;
  const head = identity.head || (project && project.git && project.git.head) || null;
  const branchHead = (branch || head) ? `${branch || 'unavailable'}@${head || 'unavailable'}` : 'unavailable';
  const dirtyVal = identity.dirty !== undefined && identity.dirty !== null
    ? (identity.dirty ? 'dirty' : 'clean')
    : (project && project.git && project.git.dirty !== undefined ? (project.git.dirty ? 'dirty' : 'clean') : 'unavailable');
  const revision = identity.revision || 'unavailable';

  const lines = [
    `project_id: ${projectId}`,
    `task_id: ${taskId}`,
    `lifecycle_state: ${lifecycle}`,
    `branch@head: ${branchHead}`,
    `dirty: ${dirtyVal}`,
    `revision: ${revision}`,
  ];

  if (action === 'retry' || action === 'rereview' || action === 'reconcile') {
    const targetId = (target && target.target_id) || (capability && capability.target_id) || 'unavailable';
    lines.push(`target_id: ${targetId}`);
  } else if (action === 'approve_owner_gate') {
    const gateId = (target && target.gate_id) || identity.gate_id || 'unavailable';
    lines.push(`gate_id: ${gateId}`);
  }

  const consequences = {
    stop: 'Consequence: Pauses future launches and interrupts active execution if supported.',
    retry: 'Consequence: Retries the failed technical review with a new review execution.',
    rereview: 'Consequence: Launches a technical re-review at the current descendant HEAD.',
    reconcile: 'Consequence: Re-anchors the stale review at the current HEAD.',
    approve_owner_gate: 'Consequence: Approves the owner gate without mutating the repository or launching a worker.',
  };
  const consequence = consequences[action] || `Consequence: Executes guarded action ${action}.`;
  lines.push(consequence);

  return lines.join('\n');
}

function renderDaemonBadge(control, monitor) {
  const badge = $('daemonBadge');
  if (!badge) return;
  const controlAvail = control && control.available !== false;
  const monitorAvail = monitor && monitor.available !== false;

  if (!controlAvail && !monitorAvail) {
    badge.className = 'badge bad';
    badge.textContent = 'Daemon unavailable';
    return;
  }
  if (monitorAvail && monitor.process_alive === false) {
    badge.className = 'badge bad';
    badge.textContent = 'Daemon stopped';
    return;
  }
  if (monitorAvail && monitor.last_error) {
    badge.className = 'badge bad';
    badge.textContent = 'Daemon error';
    return;
  }
  const ctrlData = control && control.data;
  if (ctrlData && ctrlData.control_health && ctrlData.control_health.degraded) {
    badge.className = 'badge warn';
    badge.textContent = 'Daemon degraded';
    return;
  }
  if (ctrlData && ctrlData.control_enabled === false) {
    badge.className = 'badge warn';
    badge.textContent = 'Daemon observation-only';
    return;
  }
  if (!controlAvail) {
    badge.className = 'badge warn';
    badge.textContent = 'Daemon degraded';
    return;
  }
  badge.className = 'badge ok';
  badge.textContent = 'Daemon healthy';
}

function renderWatchdogBadge(watchdog) {
  const badge = $('watchdogBadge');
  if (!badge) return;
  if (!watchdog || watchdog.available === false) {
    badge.className = 'badge bad';
    badge.textContent = 'Watchdog unavailable';
    return;
  }
  const wd = watchdog.data || watchdog;
  if (wd.degraded) {
    badge.className = 'badge warn';
    badge.textContent = 'Watchdog degraded';
    return;
  }
  const projects = Object.values(wd.projects || {});
  const hasAlert = projects.some(p => p && p.state && p.state !== 'ok' && p.state !== 'healthy_slow');
  if (hasAlert) {
    badge.className = 'badge warn';
    badge.textContent = 'Watchdog alert';
    return;
  }
  if (!projects.length) {
    badge.className = 'badge ok';
    badge.textContent = 'No watchdog projects';
    return;
  }
  badge.className = 'badge ok';
  badge.textContent = 'Watchdog healthy';
}

function renderMonitor(monitor) {
  const badge = $('monitorBadge');
  if (badge) {
    const stale = Boolean(monitor.stale);
    const stopped = monitor.process_alive === false;
    badge.className = `badge ${stale || stopped ? 'bad' : monitor.state === 'degraded' ? 'warn' : 'ok'}`;
    badge.textContent = stopped ? 'Monitor stopped' : (stale ? 'Monitor STALE' : (monitor.state === 'degraded' ? 'Monitor degraded' : 'Monitor healthy'));
  }
  const box = $('monitorDetails');
  if (box) {
    box.replaceChildren();
    kv(box, 'State', monitor.state);
    kv(box, 'PID', monitor.pid, true);
    kv(box, 'Process', monitor.process_alive ? 'alive' : 'not alive');
    kv(box, 'Heartbeat age', seconds(monitor.heartbeat_age_seconds));
    kv(box, 'Interval', seconds(monitor.interval_seconds));
    kv(box, 'Last error', monitor.last_error || 'none');
  }
}

function renderOverview(summary) {
  const box = $('overviewDetails');
  if (!box) return;
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
  if (!host) return;
  host.replaceChildren();
  const rawProjects = Array.isArray(summary.projects) ? summary.projects : [];
  const projects = rawProjects.slice().sort(compareSeverityThenIdThenTime);
  const latest = new Map();
  for (const ev of events || []) latest.set(ev.project_id, ev);

  if (!projects.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No registered project snapshots.';
    host.appendChild(empty);
    return;
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
  const host = $(hostId);
  if (!host) return;
  host.replaceChildren();
  if (!items || !items.length) {
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
  if (!host) return;
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
  const host = $('brokerResources');
  if (!host) return;
  host.replaceChildren();
  if (!payload || !payload.available) {
    const e = document.createElement('div'); e.className = 'empty';
    e.textContent = `AI resource evidence unavailable: ${text(payload && payload.error)}`;
    host.appendChild(e);
    return;
  }
  const resources = (payload.resources || []).slice().sort(compareSeverityThenIdThenTime);
  for (const r of resources) {
    const card = document.createElement('article');
    card.className = `project-card ${r.enabled && r.state && r.state.available ? 'ok' : 'warn'}`;
    const h = document.createElement('div'); h.className = 'project-name'; h.textContent = r.resource_id; card.appendChild(h);
    const g = document.createElement('div'); g.className = 'kv-grid';
    kv(g, 'Provider', r.provider);
    kv(g, 'Account', r.account);
    kv(g, 'Model', r.model);
    kv(g, 'Enabled', r.enabled);
    kv(g, 'Available', r.state && r.state.available);
    kv(g, 'Health', r.state && r.state.health);
    card.appendChild(g); host.appendChild(card);
  }
}

function renderBrokerExecutions(payload) {
  const host = $('brokerExecutions');
  if (!payload || !payload.available) {
    if (host) {
      host.replaceChildren();
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = `AI executions unavailable: ${text(payload && payload.error)}`;
      host.appendChild(e);
    }
    return;
  }
  const items = (payload.executions || []).slice().sort(compareSeverityThenIdThenTime);
  renderTimeline('brokerExecutions', items, e => `${text(e.role)} · ${text(e.resource_id)} · ${text(e.status)} · ${seconds(e.duration_seconds)}`);
}

function renderBrokerUsage(payload) {
  const host = $('brokerUsage');
  if (!host) return;
  host.replaceChildren();
  if (!payload || !payload.available) {
    const e = document.createElement('div'); e.className = 'empty';
    e.textContent = `AIBroker unavailable: ${text(payload && payload.error)}`;
    host.appendChild(e);
    return;
  }
  for (const u of (payload.usage || [])) {
    const row = document.createElement('div'); row.className = 'timeline-item';
    row.textContent = `${text(u.resource_id)} · executions ${text(u.executions)} · tokens ${u.token_source === 'reported' ? text(u.total_tokens) : 'unknown'}`;
    host.appendChild(row);
  }
  if (!(payload.usage || []).length) {
    const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'No usage records yet.'; host.appendChild(e);
  }
}

function renderWatchdogDiagnostics(payload) {
  const statusBox = $('watchdogStatus');
  const projectsBox = $('watchdogProjects');
  if (statusBox) statusBox.replaceChildren();
  if (projectsBox) projectsBox.replaceChildren();

  if (!payload || payload.available === false) {
    const errorMsg = (payload && payload.error) || 'Watchdog diagnostics unavailable';
    if (statusBox) {
      const e = document.createElement('div'); e.className = 'empty'; e.textContent = text(errorMsg); statusBox.appendChild(e);
    }
    if (projectsBox) {
      const e = document.createElement('div'); e.className = 'empty'; e.textContent = text(errorMsg); projectsBox.appendChild(e);
    }
    return;
  }

  const data = payload.data || payload;
  if (statusBox) {
    if (data.state !== undefined && data.state !== null) {
      kv(statusBox, 'State', data.state);
    }
    kv(statusBox, 'Degraded', data.degraded !== undefined ? (data.degraded ? 'yes' : 'no') : 'unavailable');
    if (data.degraded_reason) {
      kv(statusBox, 'Degraded reason', data.degraded_reason);
    }
    const autoRec = data.auto_recovery !== undefined && data.auto_recovery !== null
      ? (data.auto_recovery ? 'enabled' : 'disabled')
      : 'unavailable';
    kv(statusBox, 'Auto recovery', autoRec);
    if (data.updated_at || data.observed_at) {
      kv(statusBox, 'Observed', ago(data.updated_at || data.observed_at));
    }
  }

  if (projectsBox) {
    const projectEntries = Object.entries(data.projects || {}).map(([id, info]) => ({
      id,
      ...info,
    }));
    projectEntries.sort(compareSeverityThenIdThenTime);

    if (!projectEntries.length) {
      const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'No project diagnostics recorded.'; projectsBox.appendChild(e);
    } else {
      for (const p of projectEntries) {
        const row = document.createElement('div');
        row.className = `timeline-item ${stateTone(p.state || p.diagnostic_code)}`;
        const main = document.createElement('div');
        main.textContent = `${text(p.id)} · ${text(p.state || 'ok')}${p.diagnostic_code ? ` · ${p.diagnostic_code}` : ''}`;
        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = `${p.reason ? `${p.reason} · ` : ''}${ago(p.updated_at || p.last_attempt_at)}`;
        row.append(main, meta);
        projectsBox.appendChild(row);
      }
    }
  }
}

let controlCsrf = null;
let activePairingId = null;

function resetControlSession() {
  controlCsrf = null;
}

async function ensureControlSession(options = {}) {
  const force = Boolean(options && options.force);
  if (force) {
    controlCsrf = null;
  }
  if (controlCsrf) return controlCsrf;
  const response = await fetch('/api/v1/control/browser-sessions', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Sec-Fetch-Site': 'same-origin' }
  });
  if (!response.ok) throw new Error(`control session: HTTP ${response.status}`);
  const payload = await response.json();
  controlCsrf = payload.data.csrf_token;
  return controlCsrf;
}

function updateOpsSessionUI(connected, message) {
  if (typeof document === 'undefined') return;
  const stateEl = (typeof document.getElementById === 'function') ? document.getElementById('opsSessionState') : null;
  if (stateEl) {
    stateEl.textContent = message || (connected ? 'Connected (active)' : 'Disconnected');
    if (stateEl.classList && typeof stateEl.classList.add === 'function') {
      if (connected) {
        stateEl.classList.remove('bad', 'unknown');
        stateEl.classList.add('ok');
      } else {
        stateEl.classList.remove('ok');
        stateEl.classList.add('bad');
      }
    }
  }
}

async function controlFetch(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const headers = Object.assign({}, options.headers || {});
  let body = options.body;

  const csrf = await ensureControlSession();
  headers['X-DevOrch-CSRF'] = csrf;
  headers['Sec-Fetch-Site'] = 'same-origin';
  if (!headers['X-DevOrch-Request-ID']) {
    headers['X-DevOrch-Request-ID'] = `req-ui-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  }
  if (body !== undefined && body !== null && typeof body === 'object' && !(body instanceof FormData) && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }

  const fetchOptions = {
    ...options,
    method,
    headers,
    body,
    credentials: 'same-origin',
  };

  const response = await fetch(path, fetchOptions);

  if (response.status === 401 || response.status === 403) {
    resetControlSession();
    try {
      const newCsrf = await ensureControlSession({ force: true });
      headers['X-DevOrch-CSRF'] = newCsrf;
      const retryResponse = await fetch(path, {
        ...fetchOptions,
        headers,
      });
      if (retryResponse.status === 401 || retryResponse.status === 403) {
        updateOpsSessionUI(false, 'Session expired / unauthorized. Please reconnect.');
      } else if (retryResponse.ok) {
        updateOpsSessionUI(true, 'Connected (active)');
      }
      return retryResponse;
    } catch (err) {
      updateOpsSessionUI(false, 'Session expired. Please reconnect.');
      throw err;
    }
  }

  if (response.ok) {
    updateOpsSessionUI(true, 'Connected (active)');
  }

  return response;
}

const OPERATION_KINDS = [
  'exec',
  'spawn',
  'poll',
  'cancel',
  'read',
  'stat',
  'stage_write',
  'write',
];

function buildTransportRequest(kind, form = {}) {
  if (!OPERATION_KINDS.includes(kind)) {
    throw new Error(`unknown operation kind: ${kind}`);
  }
  const f = form || {};
  const hostId = f.host_id || 'local';
  const projectId = f.project_id || '';
  const idemKey = f.idempotency_key || `ui-${kind.replace(/_/g, '-')}-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;

  if (kind === 'exec' || kind === 'spawn') {
    let parameters = {};
    if (f.parameters !== undefined && f.parameters !== null && f.parameters !== '') {
      if (typeof f.parameters === 'string') {
        try {
          parameters = JSON.parse(f.parameters);
        } catch (e) {
          throw new Error('parameters must be a JSON object');
        }
      } else if (typeof f.parameters === 'object' && !Array.isArray(f.parameters)) {
        parameters = f.parameters;
      } else {
        throw new Error('parameters must be a JSON object');
      }
      if (typeof parameters !== 'object' || parameters === null || Array.isArray(parameters)) {
        throw new Error('parameters must be a JSON object');
      }
    }
    const timeout = (f.timeout_seconds !== undefined && f.timeout_seconds !== null && f.timeout_seconds !== '')
      ? Number(f.timeout_seconds)
      : 300;
    const cwd = (f.expected_working_directory !== undefined && f.expected_working_directory !== null)
      ? String(f.expected_working_directory)
      : '.';
    const body = {
      project_id: projectId,
      host_id: hostId,
      command_ref: String(f.command_ref || ''),
      parameters,
      expected_working_directory: cwd,
      timeout_seconds: timeout,
      idempotency_key: idemKey,
    };
    return {
      method: 'POST',
      url: `/api/v1/control/transport/${kind}`,
      body,
    };
  }

  if (kind === 'poll') {
    const opId = String(f.operation_id || f.job_id || '');
    return {
      method: 'POST',
      url: '/api/v1/control/transport/poll',
      body: {
        project_id: projectId,
        host_id: hostId,
        operation_id: opId,
      },
    };
  }

  if (kind === 'cancel') {
    const opId = String(f.operation_id || f.job_id || '');
    const reason = String(f.reason || 'cancelled by user');
    return {
      method: 'POST',
      url: '/api/v1/control/transport/cancel',
      body: {
        project_id: projectId,
        host_id: hostId,
        operation_id: opId,
        reason,
      },
    };
  }

  if (kind === 'read') {
    const filePath = String(f.path || '');
    const offset = parseInt(f.offset_bytes || 0, 10) || 0;
    let maxBytes = parseInt(f.max_bytes || 65536, 10) || 65536;
    if (maxBytes > 10 * 1024 * 1024) maxBytes = 10 * 1024 * 1024;
    if (maxBytes < 1) maxBytes = 1;
    const params = new URLSearchParams({
      project_id: projectId,
      path: filePath,
      host_id: hostId,
      offset_bytes: String(offset),
      max_bytes: String(maxBytes),
    });
    return {
      method: 'GET',
      url: `/api/v1/control/transport/read?${params.toString()}`,
      body: null,
    };
  }

  if (kind === 'stat') {
    const filePath = String(f.path || '');
    const params = new URLSearchParams({
      project_id: projectId,
      path: filePath,
      host_id: hostId,
    });
    return {
      method: 'GET',
      url: `/api/v1/control/transport/stat?${params.toString()}`,
      body: null,
    };
  }

  if (kind === 'stage_write') {
    const b64 = String(f.content_base64 || '');
    const decodedSize = parseInt(f.decoded_size_bytes || 0, 10) || 0;
    if (b64.length > 16 * 1024 * 1024 || decodedSize > 16 * 1024 * 1024) {
      throw new Error('upload exceeds maximum size (16 MiB ceiling)');
    }
    return {
      method: 'POST',
      url: '/api/v1/control/transport/stage-write',
      body: {
        project_id: projectId,
        host_id: hostId,
        content_base64: b64,
        decoded_size_bytes: decodedSize,
        content_sha256: String(f.content_sha256 || ''),
      },
    };
  }

  if (kind === 'write') {
    const hasIfAbsent = Boolean(f.if_absent === true || f.if_absent === 'true');
    const hasExpectedSha = Boolean(f.expected_sha256 && String(f.expected_sha256).trim());
    if (hasIfAbsent && hasExpectedSha) {
      throw new Error('write requires exactly one precondition (if_absent or expected_sha256), not both');
    }
    if (!hasIfAbsent && !hasExpectedSha) {
      throw new Error('write requires exactly one precondition: if_absent=true or expected_sha256');
    }
    const body = {
      project_id: projectId,
      host_id: hostId,
      target_path: String(f.target_path || ''),
      content_ref: String(f.content_ref || ''),
      idempotency_key: idemKey,
    };
    if (f.content_sha256) body.content_sha256 = String(f.content_sha256);
    if (f.decoded_size_bytes !== undefined && f.decoded_size_bytes !== null) {
      body.decoded_size_bytes = Number(f.decoded_size_bytes);
    }
    if (hasIfAbsent) {
      body.if_absent = true;
    } else {
      body.expected_sha256 = String(f.expected_sha256).trim();
    }
    return {
      method: 'POST',
      url: '/api/v1/control/transport/write',
      body,
    };
  }

  throw new Error(`unsupported operation kind: ${kind}`);
}

function confirmOperationPrompt(kind, form = {}) {
  const f = form || {};
  if (kind === 'spawn') {
    return `Confirm spawn job: project=${f.project_id || ''}, host=${f.host_id || 'local'}, command=${f.command_ref || ''}`;
  }
  if (kind === 'cancel') {
    return `Confirm cancel job: project=${f.project_id || ''}, host=${f.host_id || 'local'}, operation_id=${f.operation_id || f.job_id || ''}`;
  }
  if (kind === 'write') {
    const isAbsent = Boolean(f.if_absent === true || f.if_absent === 'true');
    const prec = isAbsent ? 'if_absent=true' : `expected_sha256=${f.expected_sha256 || ''}`;
    return `Confirm write commit: project=${f.project_id || ''}, host=${f.host_id || 'local'}, target=${f.target_path || ''}, precondition=${prec}`;
  }
  return null;
}

function renderOperationResult(container, result, options = {}) {
  if (!container || typeof document === 'undefined') return;
  if (typeof container.replaceChildren === 'function') {
    container.replaceChildren();
  } else {
    while (container.firstChild) container.removeChild(container.firstChild);
  }
  if (!result) return;

  const card = document.createElement('div');
  card.className = 'event-card';

  const title = document.createElement('div');
  title.className = 'event-title';
  const op = options.operation || result.operation || 'Operation Result';
  title.textContent = op;
  card.appendChild(title);

  const grid = document.createElement('div');
  grid.className = 'kv-grid';

  const addKv = (key, val, mono = false) => {
    if (val === undefined || val === null || val === '') return;
    const item = document.createElement('div');
    item.className = 'kv';
    const k = document.createElement('div');
    k.className = 'k';
    k.textContent = key;
    const v = document.createElement('div');
    v.className = mono ? 'v mono' : 'v';
    v.textContent = String(val);
    item.appendChild(k);
    item.appendChild(v);
    grid.appendChild(item);
  };

  const reqId = result.request_id || options.request_id;
  if (reqId) addKv('Request ID', reqId, true);
  if (result.selected_transport) addKv('Transport', result.selected_transport);
  if (result.status) addKv('Status', result.status);
  if (result.exit_code !== undefined && result.exit_code !== null) addKv('Exit Code', result.exit_code, true);
  const jobId = result.job_id || result.operation_id;
  if (jobId) addKv('Job ID', jobId, true);
  if (result.content_ref) addKv('Content Ref', result.content_ref, true);
  if (result.content_sha256 || result.sha256) addKv('SHA-256', result.content_sha256 || result.sha256, true);
  const size = result.size_bytes ?? result.decoded_size_bytes;
  if (size !== undefined && size !== null) addKv('Size (Bytes)', size);
  if (result.path) addKv('Path', result.path, true);
  if (result.pre_digest) addKv('Pre Digest', result.pre_digest, true);
  if (result.post_digest) addKv('Post Digest', result.post_digest, true);
  if (result.generated_at || result.timestamp) addKv('Time', result.generated_at || result.timestamp);
  if (result.error || result.message) addKv('Error', result.error || result.message);

  card.appendChild(grid);

  const addTextPreview = (label, textContent) => {
    if (!textContent) return;
    const heading = document.createElement('div');
    heading.className = 'event-title';
    heading.textContent = label;
    card.appendChild(heading);

    const pre = document.createElement('pre');
    pre.className = 'event-detail mono';
    const maxLen = 4000;
    const clamped = textContent.length > maxLen ? textContent.slice(0, maxLen) + '\n... [truncated]' : textContent;
    pre.textContent = clamped;
    card.appendChild(pre);
  };

  addTextPreview('Stdout', result.stdout);
  addTextPreview('Stderr', result.stderr);

  if (result.content_base64) {
    const rawPreview = `[Binary content: ${size ?? result.content_base64.length} bytes, sha256: ${result.content_sha256 || 'unknown'}]`;
    addTextPreview('File Content', rawPreview);
  }

  container.appendChild(card);
}

function renderTransportEvidence(container, evidenceData) {
  if (!container || typeof document === 'undefined') return;
  if (typeof container.replaceChildren === 'function') {
    container.replaceChildren();
  } else {
    while (container.firstChild) container.removeChild(container.firstChild);
  }
  const items = (evidenceData && (evidenceData.operations || evidenceData.items)) || [];
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No transport operations recorded.';
    container.appendChild(empty);
    return;
  }
  for (const item of items) {
    const card = document.createElement('div');
    card.className = 'event-card';

    const header = document.createElement('div');
    header.className = 'event-title';
    header.textContent = `${item.operation || 'operation'} · ${item.status || 'unknown'}`;
    card.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'kv-grid';

    const addKv = (key, val, mono = false) => {
      if (val === undefined || val === null || val === '') return;
      const kv = document.createElement('div');
      kv.className = 'kv';
      const k = document.createElement('div');
      k.className = 'k';
      k.textContent = key;
      const v = document.createElement('div');
      v.className = mono ? 'v mono' : 'v';
      v.textContent = String(val);
      kv.appendChild(k);
      kv.appendChild(v);
      grid.appendChild(kv);
    };

    if (item.request_id) addKv('Request ID', item.request_id, true);
    if (item.selected_transport) addKv('Transport', item.selected_transport);
    if (item.project_id) addKv('Project', item.project_id);
    if (item.exit_code !== undefined && item.exit_code !== null) addKv('Exit Code', item.exit_code, true);
    if (item.error) addKv('Error', item.error);
    if (item.timestamp || item.generated_at) addKv('Time', item.timestamp || item.generated_at);

    card.appendChild(grid);
    container.appendChild(card);
  }
}

async function bufferToBase64AndSha256(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
  }
  const base64 = typeof btoa === 'function' ? btoa(binary) : Buffer.from(buffer).toString('base64');

  let sha256Hex = '';
  if (typeof crypto !== 'undefined' && crypto.subtle && typeof crypto.subtle.digest === 'function') {
    const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    sha256Hex = 'sha256:' + hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
  } else if (typeof require === 'function') {
    try {
      const nodeCrypto = require('crypto');
      sha256Hex = 'sha256:' + nodeCrypto.createHash('sha256').update(Buffer.from(buffer)).digest('hex');
    } catch (e) {}
  }
  return { base64, sha256Hex, size: bytes.length };
}

async function loadCommandCatalog(projectId, hostId) {
  try {
    const params = new URLSearchParams({
      project_id: projectId || '',
      host_id: hostId || 'local',
    });
    const res = await controlFetch(`/api/v1/control/transport/commands?${params.toString()}`);
    if (!res.ok) return;
    const envelope = await res.json();
    const data = envelope.data || {};
    const commands = data.commands || [];
    const datalist = (typeof document !== 'undefined') ? document.getElementById('opsCommandCatalog') : null;
    if (datalist && typeof datalist.replaceChildren === 'function') {
      datalist.replaceChildren();
      for (const cmd of commands) {
        if (cmd.selectable === false) continue;
        const opt = document.createElement('option');
        opt.value = cmd.command_ref;
        opt.textContent = `${cmd.command_ref} (${cmd.effect_class})`;
        datalist.appendChild(opt);
      }
    }
  } catch (e) {}
}

function initOperationsConsole() {
  if (typeof document === 'undefined') return;
  const $el = (id) => (typeof document.getElementById === 'function' ? document.getElementById(id) : null);

  const resultContainer = $el('opsResult');
  const evidenceContainer = $el('opsEvidence');
  const projectInput = $el('opsProject');
  const hostInput = $el('opsHost');

  let stagedContentRef = null;
  let stagedContentSha256 = null;
  let stagedDecodedSize = 0;

  const getForm = (extra = {}) => ({
    project_id: projectInput ? projectInput.value.trim() : '',
    host_id: hostInput ? hostInput.value.trim() : 'local',
    ...extra,
  });

  const displayResult = (data, operation, reqId) => {
    if (resultContainer) {
      renderOperationResult(resultContainer, data, { operation, request_id: reqId });
    }
  };

  const refreshEvidence = async () => {
    try {
      const res = await controlFetch('/api/v1/control/transport/operations?limit=50');
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        if (evidenceContainer) renderTransportEvidence(evidenceContainer, { items: [], error: err.message });
        return;
      }
      const data = await res.json();
      if (evidenceContainer) {
        renderTransportEvidence(evidenceContainer, data.data || data);
      }
    } catch (e) {
      if (evidenceContainer) renderTransportEvidence(evidenceContainer, { items: [] });
    }
  };

  const checkSessionAndStatus = async () => {
    try {
      const res = await controlFetch('/api/v1/control/external/status');
      if (res.ok) {
        updateOpsSessionUI(true, 'Connected (active)');
      } else {
        updateOpsSessionUI(false, 'Disconnected');
      }
    } catch (e) {
      updateOpsSessionUI(false, 'Disconnected');
    }
    const proj = projectInput ? projectInput.value.trim() : '';
    const host = hostInput ? hostInput.value.trim() : 'local';
    loadCommandCatalog(proj, host);
  };

  const reconnectBtn = $el('opsReconnect');
  if (reconnectBtn) {
    reconnectBtn.addEventListener('click', async () => {
      try {
        await ensureControlSession({ force: true });
        await checkSessionAndStatus();
      } catch (e) {
        updateOpsSessionUI(false, 'Reconnect failed');
      }
    });
  }

  const refreshEvidenceBtn = $el('opsRefreshEvidence');
  if (refreshEvidenceBtn) {
    refreshEvidenceBtn.addEventListener('click', () => refreshEvidence());
  }

  const execBtn = $el('opsExec');
  if (execBtn) {
    execBtn.addEventListener('click', async () => {
      try {
        const form = getForm({
          command_ref: $el('opsCommandRef') ? $el('opsCommandRef').value.trim() : '',
          parameters: $el('opsParameters') ? $el('opsParameters').value.trim() : '',
          expected_working_directory: $el('opsWorkingDirectory') ? $el('opsWorkingDirectory').value.trim() : '.',
          timeout_seconds: $el('opsTimeout') ? $el('opsTimeout').value : 300,
          idempotency_key: $el('opsIdempotencyKey') ? $el('opsIdempotencyKey').value.trim() : '',
        });
        const req = buildTransportRequest('exec', form);
        const res = await controlFetch(req.url, { method: req.method, body: req.body });
        const envelope = await res.json();
        displayResult(envelope.data || envelope, 'Exec', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        displayResult({ error: err.message }, 'Exec Failed');
      }
    });
  }

  const spawnBtn = $el('opsSpawn');
  if (spawnBtn) {
    spawnBtn.addEventListener('click', async () => {
      try {
        const form = getForm({
          command_ref: $el('opsCommandRef') ? $el('opsCommandRef').value.trim() : '',
          parameters: $el('opsParameters') ? $el('opsParameters').value.trim() : '',
          expected_working_directory: $el('opsWorkingDirectory') ? $el('opsWorkingDirectory').value.trim() : '.',
          timeout_seconds: $el('opsTimeout') ? $el('opsTimeout').value : 300,
          idempotency_key: $el('opsIdempotencyKey') ? $el('opsIdempotencyKey').value.trim() : '',
        });
        const prompt = confirmOperationPrompt('spawn', form);
        if (prompt && typeof window !== 'undefined' && typeof window.confirm === 'function') {
          if (!window.confirm(prompt)) return;
        }
        const req = buildTransportRequest('spawn', form);
        const res = await controlFetch(req.url, { method: req.method, body: req.body });
        const envelope = await res.json();
        const resData = envelope.data || envelope;
        displayResult(resData, 'Spawn', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
        if (resData.job_id || resData.operation_id) {
          const jobInput = $el('opsJobId');
          if (jobInput) jobInput.value = resData.job_id || resData.operation_id;
        }
      } catch (err) {
        displayResult({ error: err.message }, 'Spawn Failed');
      }
    });
  }

  const pollBtn = $el('opsPoll');
  if (pollBtn) {
    pollBtn.addEventListener('click', async () => {
      try {
        const form = getForm({
          operation_id: $el('opsJobId') ? $el('opsJobId').value.trim() : '',
        });
        const req = buildTransportRequest('poll', form);
        const res = await controlFetch(req.url, { method: req.method, body: req.body });
        const envelope = await res.json();
        displayResult(envelope.data || envelope, 'Poll', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        displayResult({ error: err.message }, 'Poll Failed');
      }
    });
  }

  const cancelBtn = $el('opsCancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', async () => {
      try {
        const form = getForm({
          operation_id: $el('opsJobId') ? $el('opsJobId').value.trim() : '',
        });
        const prompt = confirmOperationPrompt('cancel', form);
        if (prompt && typeof window !== 'undefined' && typeof window.confirm === 'function') {
          if (!window.confirm(prompt)) return;
        }
        const req = buildTransportRequest('cancel', form);
        const res = await controlFetch(req.url, { method: req.method, body: req.body });
        const envelope = await res.json();
        displayResult(envelope.data || envelope, 'Cancel', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        displayResult({ error: err.message }, 'Cancel Failed');
      }
    });
  }

  const readBtn = $el('opsRead');
  if (readBtn) {
    readBtn.addEventListener('click', async () => {
      try {
        const form = getForm({
          path: $el('opsPath') ? $el('opsPath').value.trim() : '',
          offset_bytes: $el('opsOffset') ? $el('opsOffset').value : 0,
          max_bytes: $el('opsMaxBytes') ? $el('opsMaxBytes').value : 65536,
        });
        const req = buildTransportRequest('read', form);
        const res = await controlFetch(req.url, { method: req.method });
        const envelope = await res.json();
        displayResult(envelope.data || envelope, 'Read File', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        displayResult({ error: err.message }, 'Read Failed');
      }
    });
  }

  const statBtn = $el('opsStat');
  if (statBtn) {
    statBtn.addEventListener('click', async () => {
      try {
        const form = getForm({
          path: $el('opsPath') ? $el('opsPath').value.trim() : '',
        });
        const req = buildTransportRequest('stat', form);
        const res = await controlFetch(req.url, { method: req.method });
        const envelope = await res.json();
        displayResult(envelope.data || envelope, 'Stat', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        displayResult({ error: err.message }, 'Stat Failed');
      }
    });
  }

  const stageBtn = $el('opsStageWrite');
  if (stageBtn) {
    stageBtn.addEventListener('click', async () => {
      try {
        const fileInput = $el('opsFile');
        const file = (fileInput && fileInput.files && fileInput.files[0]) ? fileInput.files[0] : null;
        if (!file) {
          throw new Error('Please select a file to stage');
        }
        if (file.size > 12 * 1024 * 1024) {
          throw new Error('upload exceeds maximum size (16 MiB ceiling)');
        }
        const buffer = await file.arrayBuffer();
        const { base64, sha256Hex, size } = await bufferToBase64AndSha256(buffer);
        const form = getForm({
          content_base64: base64,
          decoded_size_bytes: size,
          content_sha256: sha256Hex,
        });
        const req = buildTransportRequest('stage_write', form);
        const res = await controlFetch(req.url, { method: req.method, body: req.body });
        const envelope = await res.json();
        const data = envelope.data || envelope;
        if (data.content_ref) {
          stagedContentRef = data.content_ref;
          stagedContentSha256 = data.content_sha256;
          stagedDecodedSize = data.decoded_size_bytes;
        }
        displayResult(data, 'Stage Write Content', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        displayResult({ error: err.message }, 'Stage Write Failed');
      }
    });
  }

  const commitBtn = $el('opsCommitWrite');
  if (commitBtn) {
    commitBtn.addEventListener('click', async () => {
      try {
        if (!stagedContentRef) {
          throw new Error('No staged content. Stage a file first.');
        }
        const targetPath = $el('opsTargetPath') ? $el('opsTargetPath').value.trim() : '';
        const ifAbsentBox = $el('opsIfAbsent');
        const isAbsent = ifAbsentBox ? ifAbsentBox.checked : false;
        const expectedSha = $el('opsExpectedSha256') ? $el('opsExpectedSha256').value.trim() : '';
        const form = getForm({
          target_path: targetPath,
          content_ref: stagedContentRef,
          content_sha256: stagedContentSha256,
          decoded_size_bytes: stagedDecodedSize,
          if_absent: isAbsent,
          expected_sha256: expectedSha || undefined,
        });
        const prompt = confirmOperationPrompt('write', form);
        if (prompt && typeof window !== 'undefined' && typeof window.confirm === 'function') {
          if (!window.confirm(prompt)) return;
        }
        const req = buildTransportRequest('write', form);
        const res = await controlFetch(req.url, { method: req.method, body: req.body });
        const envelope = await res.json();
        stagedContentRef = null;
        stagedContentSha256 = null;
        stagedDecodedSize = 0;
        displayResult(envelope.data || envelope, 'Commit Write', res.headers ? res.headers.get('X-DevOrch-Request-ID') : null);
      } catch (err) {
        stagedContentRef = null;
        stagedContentSha256 = null;
        stagedDecodedSize = 0;
        displayResult({ error: err.message }, 'Commit Write Failed');
      }
    });
  }

  if (projectInput) {
    projectInput.addEventListener('change', () => {
      const proj = projectInput.value.trim();
      const host = hostInput ? hostInput.value.trim() : 'local';
      loadCommandCatalog(proj, host);
    });
  }
  if (hostInput) {
    hostInput.addEventListener('change', () => {
      const proj = projectInput ? projectInput.value.trim() : '';
      const host = hostInput.value.trim();
      loadCommandCatalog(proj, host);
    });
  }

  checkSessionAndStatus();
}

async function pollControlCommand(commandId, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const payload = await getJson(`/api/v1/control/commands/${encodeURIComponent(commandId)}`);
    if (payload.data && payload.data.state !== 'pending') return payload.data;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  return {command_id: commandId, state: 'pending', reason: 'daemon result is still pending'};
}

const CONFIRMED_ACTIONS = new Set(['stop', 'retry', 'rereview', 'reconcile', 'approve_owner_gate']);

async function sendControl(project, action, target = {}) {
  if (CONFIRMED_ACTIONS.has(action)) {
    const confirmPrompt = describeGuardedAction(project, { action }, target);
    if (!window.confirm(confirmPrompt)) return null;
  }
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

async function createAdapterPairing() {
  const csrf = await ensureControlSession();
  const response = await fetch('/api/v1/control/adapter-pairings', {
    method: 'POST', credentials: 'same-origin',
    headers: {'X-DevOrch-CSRF': csrf, 'Sec-Fetch-Site': 'same-origin'},
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `adapter pairing: HTTP ${response.status}`);
  activePairingId = payload.data.pairing_id;
  const pairingCodeEl = $('pairingCode');
  if (pairingCodeEl) pairingCodeEl.textContent = `${payload.data.pairing_id}:${payload.data.code}`;
  const revokeBtn = $('revokeAdapter');
  if (revokeBtn) revokeBtn.disabled = false;
  return payload.data;
}

async function revokeAdapterPairing() {
  if (!activePairingId) return null;
  const csrf = await ensureControlSession();
  const response = await fetch(`/api/v1/control/adapter-pairings/${encodeURIComponent(activePairingId)}/revoke`, {
    method: 'POST', credentials: 'same-origin',
    headers: {'X-DevOrch-CSRF': csrf, 'Sec-Fetch-Site': 'same-origin'},
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `pairing revoke: HTTP ${response.status}`);
  activePairingId = null;
  const pairingCodeEl = $('pairingCode');
  if (pairingCodeEl) pairingCodeEl.textContent = 'Pairing revoked.';
  const revokeBtn = $('revokeAdapter');
  if (revokeBtn) revokeBtn.disabled = true;
  return payload.data;
}

function renderControlSurface(payload) {
  const data = payload && payload.data || {};
  const projectsHost = $('controlProjects');
  if (projectsHost) projectsHost.replaceChildren();
  const statusEl = $('controlStatus');
  if (statusEl) statusEl.textContent = data.control_enabled ? 'guarded actions enabled' : 'observation only';

  const rawProjects = data.projects || [];
  const projects = rawProjects.slice().sort(compareSeverityThenIdThenTime);

  for (const project of projects) {
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
    kv(grid, 'Next action', project.next_title || project.next_status);
    kv(grid, 'Active roles', (project.active_roles || []).map(item => `${text(item.role)} / ${text(item.state)}`).join(', ') || UNKNOWN);
    kv(grid, 'Watchdog', project.watchdog && (project.watchdog.state || project.watchdog.diagnostic_code));
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
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = capability.action;
      const initialTarget = buildControlTarget(project, capability, targetSelect.value);
      const targetValid = initialTarget !== null;
      button.disabled = !data.control_enabled || !capability.available || !targetValid;
      button.title = capability.reason || (!targetValid ? 'Required target parameter missing' : '');

      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const target = buildControlTarget(project, capability, targetSelect.value);
          if (target === null) {
            if (statusEl) statusEl.textContent = `${capability.action}: missing required target`;
            return;
          }
          const result = await sendControl(project, capability.action, target);
          if (result) {
            button.textContent = `${capability.action}: ${result.state}`;
            await refresh();
          }
        } catch (error) {
          button.textContent = `${capability.action}: failed`;
          if (statusEl) statusEl.textContent = String(error.message || error);
        } finally {
          const currentTarget = buildControlTarget(project, capability, targetSelect.value);
          button.disabled = !data.control_enabled || !capability.available || currentTarget === null;
        }
      });
      controls.appendChild(button);
    }
    card.appendChild(controls);
    if (projectsHost) projectsHost.appendChild(card);
  }
  if (!(data.projects || []).length && projectsHost) {
    const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'No project control projections.'; projectsHost.appendChild(e);
  }
  const bindings = $('controlBindings');
  if (bindings) {
    bindings.replaceChildren();
    for (const item of (data.bindings || [])) {
      const row = document.createElement('div'); row.className = 'timeline-item';
      row.textContent = `${text(item.project_id)} · ${text(item.state)} · ${text(item.adapter)} / ${text(item.binding_id)}`;
      bindings.appendChild(row);
    }
    if (!(data.bindings || []).length) {
      const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'No runtime bindings.'; bindings.appendChild(e);
    }
  }
  renderTimeline('controlCommands', data.commands || [], item => `${text(item.action)} · ${text(item.state)}${item.reason ? ` · ${item.reason}` : ''}`);
}

function renderAccounting(payload) {
  const summary = $('accountingSummary'); if (summary) summary.replaceChildren();
  const bottleneck = $('accountingBottleneck'); if (bottleneck) bottleneck.replaceChildren();
  const provider = $('providerEvidence'); if (provider) provider.replaceChildren();
  const rdc = $('rdcEvidence'); if (rdc) rdc.replaceChildren();
  const hypotheses = $('hypothesisEvidence'); if (hypotheses) hypotheses.replaceChildren();
  const breakdown = $('scopeBreakdown'); if (breakdown) breakdown.replaceChildren();
  const gates = $('acceptanceGates'); if (gates) gates.replaceChildren();
  const warnings = $('evidenceWarnings'); if (warnings) warnings.replaceChildren();

  if (!payload || !payload.available) {
    for (const host of [summary, bottleneck, provider, rdc, hypotheses, breakdown, gates, warnings]) {
      if (host) {
        const item = document.createElement('div'); item.className = 'empty';
        item.textContent = `Execution evidence unavailable: ${text(payload && payload.error)}`;
        host.appendChild(item);
      }
    }
    return;
  }
  const status = payload.data_status || {};
  const accounting = payload.accounting || {};
  const phases = accounting.duration_by_phase || {};

  if (summary) {
    kv(summary, 'Accounting evidence', status.accounting);
    kv(summary, 'EDR', status.accounting === 'measured' ? ratio(accounting.edr) : 'unavailable');
    kv(summary, 'Accepted productive', status.accounting === 'measured' ? seconds(accounting.accepted_productive_seconds) : 'unavailable');
    kv(summary, 'Rejected work', status.accounting === 'measured' ? seconds(accounting.rejected_attempt_seconds) : 'unavailable');
    kv(summary, 'Owner wait', status.accounting === 'measured' ? seconds(accounting.owner_wait_seconds) : 'unavailable');
    kv(summary, 'Retry wall time', status.accounting === 'measured' ? seconds(accounting.retry_wall_time_seconds) : 'unavailable');
    kv(summary, 'Idle', status.accounting === 'measured' ? seconds(phases.idle) : 'unavailable');
    kv(summary, 'Plan-review churn', status.accounting === 'measured' ? accounting.plan_review_churn : 'unavailable');
    kv(summary, 'Inference', payload.inference);
  }

  if (bottleneck) {
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
  }

  if (provider) {
    const providerData = payload.provider || {};
    kv(provider, 'Evidence', status.provider);
    kv(provider, 'Results', status.provider === 'measured' ? providerData.result_count : 'unavailable');
    kv(provider, 'Context hits', status.provider === 'measured' ? providerData.context_hits : 'unavailable');
    kv(provider, 'Context switches', status.provider === 'measured' ? providerData.context_switches : 'unavailable');
    kv(provider, 'Resource switches', status.provider === 'measured' ? providerData.resource_switches : 'unavailable');
    kv(provider, 'Failover latency', status.provider === 'measured' ? seconds(providerData.failover_latency_seconds) : 'unavailable');
    kv(provider, 'Quota observations', status.provider === 'measured' ? providerData.quota_observation_count : 'unavailable');
    kv(provider, 'Rate limits', status.provider === 'measured' ? providerData.rate_limit_observation_count : 'unavailable');
  }

  if (rdc) {
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
  }

  if (hypotheses) {
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
  }

  if (breakdown) {
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
  }

  if (gates) {
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
  }

  if (warnings) {
    for (const warning of (payload.warnings || [])) {
      const row = document.createElement('div'); row.className = 'timeline-item'; row.textContent = text(warning); warnings.appendChild(row);
    }
    if (!(payload.warnings || []).length) {
      const item = document.createElement('div'); item.className = 'empty'; item.textContent = 'No evidence warnings.'; warnings.appendChild(item);
    }
  }
}

async function refresh() {
  const endpoints = [
    { key: 'monitor', url: '/api/monitor' },
    { key: 'summary', url: '/api/summary' },
    { key: 'events', url: '/api/events?limit=30' },
    { key: 'runs', url: '/api/runs?limit=20' },
    { key: 'orchestration', url: '/api/orchestration' },
    { key: 'brokerResources', url: '/api/broker/resources' },
    { key: 'brokerExecutions', url: '/api/broker/executions' },
    { key: 'brokerUsage', url: '/api/broker/usage' },
    { key: 'accounting', url: '/api/accounting' },
    { key: 'control', url: '/api/v1/control/overview' },
    { key: 'watchdog', url: '/api/watchdog' },
  ];

  const results = await Promise.allSettled(endpoints.map(e => getJson(e.url)));
  const data = {};
  endpoints.forEach((e, idx) => {
    const res = results[idx];
    if (res.status === 'fulfilled') {
      data[e.key] = res.value;
    } else {
      data[e.key] = { available: false, error: String(res.reason && res.reason.message || res.reason) };
    }
  });

  renderDaemonBadge(data.control, data.monitor);

  if (data.monitor && data.monitor.available !== false) {
    renderMonitor(data.monitor);
  } else {
    const badge = $('monitorBadge');
    if (badge) { badge.className = 'badge bad'; badge.textContent = 'Monitor Disconnected'; }
    const box = $('monitorDetails');
    if (box) {
      box.replaceChildren();
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = `Monitor unavailable: ${data.monitor ? data.monitor.error : 'error'}`;
      box.appendChild(e);
    }
  }

  const watchdogData = data.watchdog && data.watchdog.available !== false
    ? data.watchdog
    : (data.control && data.control.data && data.control.data.watchdog
        ? { available: true, data: data.control.data.watchdog }
        : data.watchdog);
  renderWatchdogBadge(watchdogData);

  if (data.summary && data.summary.available !== false) {
    renderOverview(data.summary);
  } else {
    const box = $('overviewDetails');
    if (box) {
      box.replaceChildren();
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = `Overview unavailable: ${data.summary ? data.summary.error : 'error'}`;
      box.appendChild(e);
    }
  }

  const monitorFailed = !data.monitor || data.monitor.available === false;
  const freshnessBadge = $('freshnessBadge');
  const freshnessState = computeFreshnessState(
    data.monitor && data.monitor.available !== false ? data.monitor : null,
    data.summary && data.summary.available !== false ? data.summary : null,
    monitorFailed
  );
  if (freshnessBadge) {
    freshnessBadge.className = `badge ${freshnessState === 'Live' ? 'ok' : freshnessState === 'Stale' ? 'warn' : 'bad'}`;
    freshnessBadge.textContent = freshnessState;
  }

  const kpis = computeKPIs({
    summary: data.summary && data.summary.available !== false ? data.summary : null,
    control: data.control && data.control.available !== false ? data.control : null,
    brokerResources: data.brokerResources,
    brokerExecutions: data.brokerExecutions,
    watchdog: watchdogData,
    monitor: data.monitor && data.monitor.available !== false ? data.monitor : null,
  });
  if ($('kpiProjects')) $('kpiProjects').textContent = kpis.projects;
  if ($('kpiRoles')) $('kpiRoles').textContent = kpis.roles;
  if ($('kpiIncidents')) $('kpiIncidents').textContent = kpis.incidents;
  if ($('kpiExecutions')) $('kpiExecutions').textContent = kpis.executions;
  if ($('kpiResources')) $('kpiResources').textContent = kpis.resources;

  const events = (data.events && data.events.available !== false && Array.isArray(data.events.items)) ? data.events.items : [];
  if (data.summary && data.summary.available !== false) {
    renderProjects(data.summary, events);
  } else {
    const host = $('projects');
    if (host) {
      host.replaceChildren();
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = `Projects unavailable: ${data.summary ? data.summary.error : 'error'}`;
      host.appendChild(e);
    }
  }

  renderBrokerResources(data.brokerResources);
  renderBrokerExecutions(data.brokerExecutions);
  renderBrokerUsage(data.brokerUsage);

  renderWatchdogDiagnostics(watchdogData);

  renderAccounting(data.accounting);

  if (data.control && data.control.available !== false) {
    renderControlSurface(data.control);
  } else {
    const statusEl = $('controlStatus');
    if (statusEl) statusEl.textContent = `control unavailable: ${data.control ? data.control.error : 'error'}`;
    const pHost = $('controlProjects');
    if (pHost) {
      pHost.replaceChildren();
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = `Control unavailable: ${data.control ? data.control.error : 'error'}`;
      pHost.appendChild(e);
    }
  }

  if (data.orchestration && data.orchestration.available !== false) {
    renderOrchestration(data.orchestration);
  } else {
    const host = $('orchestration');
    if (host) {
      host.replaceChildren();
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = `Orchestration unavailable: ${data.orchestration ? data.orchestration.error : 'error'}`;
      host.appendChild(e);
    }
  }

  const runs = (data.runs && data.runs.available !== false && Array.isArray(data.runs.items)) ? data.runs.items : [];
  renderTimeline('events', events, e => `${text(e.from_state)} → ${text(e.to_state)} · ${text(e.task_id)}`);
  renderTimeline('runs', runs, r => `${text(r.task_id)} · ${text(r.result)} · ${seconds(r.duration_seconds)}`);

  const lastRefreshEl = $('lastRefresh');
  if (lastRefreshEl) lastRefreshEl.textContent = `refreshed ${new Date().toLocaleTimeString()}`;
}

const VIEW_IDS = new Set([
  'overview',
  'projects-section',
  'resources-section',
  'accounting-section',
  'logs-section',
  'system-section',
  'operations-section',
]);

const LEGACY_HASH_ALIASES = {
  'controls': 'projects-section',
  'orchestration-section': 'projects-section',
  'runs-section': 'logs-section',
  'watchdog-section': 'system-section',
};

function isKnownViewHash(rawHash) {
  if (!rawHash) return false;
  const cleaned = String(rawHash).replace(/^#/, '').trim();
  if (!cleaned) return false;
  return Boolean(LEGACY_HASH_ALIASES[cleaned] || VIEW_IDS.has(cleaned));
}

function resolveViewId(rawHash) {
  if (!rawHash) return 'overview';
  const cleaned = String(rawHash).replace(/^#/, '').trim();
  if (!cleaned) return 'overview';
  if (LEGACY_HASH_ALIASES[cleaned]) return LEGACY_HASH_ALIASES[cleaned];
  if (VIEW_IDS.has(cleaned)) return cleaned;
  return 'overview';
}

function activateView(targetInput, options = {}) {
  const opts = typeof options === 'boolean' ? { focusHeading: options } : (options || {});
  const shouldFocusHeading = Boolean(opts.focusHeading);
  const targetViewId = resolveViewId(targetInput);
  if (typeof document !== 'undefined') {
    const sections = (typeof document.querySelectorAll === 'function')
      ? document.querySelectorAll('.view-section')
      : [];
    for (const section of sections) {
      const active = section.id === targetViewId;
      if (active) {
        if (typeof section.removeAttribute === 'function') section.removeAttribute('hidden');
        else section.hidden = false;
        if (section.classList && typeof section.classList.add === 'function') section.classList.add('is-active');
      } else {
        if (typeof section.setAttribute === 'function') section.setAttribute('hidden', '');
        else section.hidden = true;
        if (section.classList && typeof section.classList.remove === 'function') section.classList.remove('is-active');
      }
    }

    const links = (typeof document.querySelectorAll === 'function')
      ? document.querySelectorAll('nav.sidebar a, .sidebar-nav a')
      : [];
    for (const link of links) {
      const href = (typeof link.getAttribute === 'function') ? (link.getAttribute('href') || '') : (link.href || '');
      const resolved = resolveViewId(href);
      const active = resolved === targetViewId;
      if (active) {
        if (typeof link.setAttribute === 'function') link.setAttribute('aria-current', 'page');
        if (link.classList && typeof link.classList.add === 'function') link.classList.add('is-active');
      } else {
        if (typeof link.removeAttribute === 'function') link.removeAttribute('aria-current');
        if (link.classList && typeof link.classList.remove === 'function') link.classList.remove('is-active');
      }
    }

    if (shouldFocusHeading) {
      const heading = (typeof document.getElementById === 'function' ? document.getElementById(`${targetViewId}-heading`) : null) ||
        (typeof document.querySelector === 'function' ? document.querySelector(`#${targetViewId} h2`) : null);
      if (heading && typeof heading.focus === 'function') {
        if (typeof heading.hasAttribute === 'function' && !heading.hasAttribute('tabindex') && typeof heading.setAttribute === 'function') {
          heading.setAttribute('tabindex', '-1');
        }
        heading.focus();
      }
    }
  }
  return targetViewId;
}

function initNavigation() {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  // Initial activation at load: do not move focus so tab order skip-link -> sidebar -> header -> main is preserved
  activateView(resolveViewId(window.location ? window.location.hash : ''), { focusHeading: false });

  // Skip-link activation: focus #mainContent directly without resetting active view
  const skipLink = (typeof document.querySelector === 'function')
    ? document.querySelector('.skip-link')
    : null;
  if (skipLink && typeof skipLink.addEventListener === 'function') {
    skipLink.addEventListener('click', (e) => {
      if (e && typeof e.preventDefault === 'function') e.preventDefault();
      const mainEl = (typeof document.getElementById === 'function')
        ? document.getElementById('mainContent')
        : (typeof document.querySelector === 'function' ? document.querySelector('#mainContent') : null);
      if (mainEl && typeof mainEl.focus === 'function') {
        if (typeof mainEl.hasAttribute === 'function' && !mainEl.hasAttribute('tabindex') && typeof mainEl.setAttribute === 'function') {
          mainEl.setAttribute('tabindex', '-1');
        }
        mainEl.focus();
      }
    });
  }

  // Hash change: user-initiated navigation via URL hash
  window.addEventListener('hashchange', () => {
    const rawHash = (window.location && window.location.hash) ? window.location.hash : '';
    const cleaned = String(rawHash).replace(/^#/, '').trim();
    // Non-view fragments (e.g. #mainContent or in-page anchors) must not switch view or steal focus
    if (cleaned && !isKnownViewHash(cleaned)) {
      return;
    }
    activateView(resolveViewId(rawHash), { focusHeading: true });
  });

  // Sidebar links: user-initiated navigation
  const sidebarLinks = (typeof document.querySelectorAll === 'function')
    ? document.querySelectorAll('nav.sidebar a, .sidebar-nav a')
    : [];
  for (const link of sidebarLinks) {
    if (typeof link.addEventListener === 'function') {
      link.addEventListener('click', () => {
        const href = (typeof link.getAttribute === 'function') ? link.getAttribute('href') : link.href;
        activateView(resolveViewId(href), { focusHeading: true });
      });
    }
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    renderAccounting,
    renderControlSurface,
    sendControl,
    createAdapterPairing,
    revokeAdapterPairing,
    buildControlTarget,
    describeGuardedAction,
    computeFreshnessState,
    computeIncidentCount,
    computeKPIs,
    severityRank,
    compareSeverityThenIdThenTime,
    renderWatchdogDiagnostics,
    renderBrokerResources,
    renderBrokerExecutions,
    renderProjects,
    renderOverview,
    renderMonitor,
    renderDaemonBadge,
    renderWatchdogBadge,
    resolveViewId,
    isKnownViewHash,
    activateView,
    initNavigation,
    VIEW_IDS,
    LEGACY_HASH_ALIASES,
    // P19 exports
    buildTransportRequest,
    OPERATION_KINDS,
    confirmOperationPrompt,
    renderOperationResult,
    renderTransportEvidence,
    controlFetch,
    ensureControlSession,
    resetControlSession,
    initOperationsConsole,
  };
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const pairBtn = $('pairAdapter');
  if (pairBtn) pairBtn.addEventListener('click', () => createAdapterPairing().catch(error => {
    const codeEl = $('pairingCode'); if (codeEl) codeEl.textContent = String(error.message || error);
  }));
  const revokeBtn = $('revokeAdapter');
  if (revokeBtn) revokeBtn.addEventListener('click', () => revokeAdapterPairing().catch(error => {
    const codeEl = $('pairingCode'); if (codeEl) codeEl.textContent = String(error.message || error);
  }));
  const refreshBtn = $('refreshBtn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refresh());
  initNavigation();
  initOperationsConsole();
  refresh();
  setInterval(refresh, 10000);
}
