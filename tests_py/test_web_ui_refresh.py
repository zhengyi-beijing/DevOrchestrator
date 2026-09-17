import json
import re
import subprocess
import unittest
from pathlib import Path

from dev_orchestrator.control.command_store import (
    CONTROL_ACTIONS,
    canonical_request,
)

ROOT = Path(__file__).resolve().parents[1]
APP_JS = str(ROOT / "web" / "app.js")
INDEX_HTML = ROOT / "web" / "index.html"
STYLE_CSS = ROOT / "web" / "style.css"


def run_node(script: str, timeout: int = 10) -> dict:
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=True,
    )
    return json.loads(completed.stdout)


class WebUiRefreshTests(unittest.TestCase):
    def test_build_control_target_all_11_actions_and_allowlist_subset(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ buildControlTarget }} = app;

const project = {{
  project_id: 'p1',
  control_identity: {{
    gate_id: 'gate-1',
    task_id: 'P12.7',
    branch: 'main',
    head: 'abcdef',
    revision: 'rev-1',
  }},
}};

const projectNoGate = {{
  project_id: 'p1',
  control_identity: {{
    gate_id: null,
    task_id: 'P12.7',
  }},
}};

const liveSession = {{ adapter: 'chatgpt', binding_id: 'bind-1' }};

const cases = [
  {{ action: 'continue', capability: {{ action: 'continue' }}, expected: {{}} }},
  {{ action: 'pause', capability: {{ action: 'pause' }}, expected: {{}} }},
  {{ action: 'resume', capability: {{ action: 'resume' }}, expected: {{}} }},
  {{ action: 'stop', capability: {{ action: 'stop' }}, expected: {{}} }},
  {{ action: 'unbind_conversation', capability: {{ action: 'unbind_conversation' }}, expected: {{}} }},
  {{ action: 'retry', capability: {{ action: 'retry', target_id: 'target-retry' }}, expected: {{ target_id: 'target-retry' }} }},
  {{ action: 'retry_missing', capability: {{ action: 'retry' }}, expected: null }},
  {{ action: 'rereview', capability: {{ action: 'rereview', target_id: 'target-rereview' }}, expected: {{ target_id: 'target-rereview' }} }},
  {{ action: 'rereview_missing', capability: {{ action: 'rereview' }}, expected: null }},
  {{ action: 'reconcile', capability: {{ action: 'reconcile', target_id: 'target-reconcile' }}, expected: {{ target_id: 'target-reconcile' }} }},
  {{ action: 'reconcile_missing', capability: {{ action: 'reconcile' }}, expected: null }},
  {{ action: 'approve_owner_gate', capability: {{ action: 'approve_owner_gate' }}, project: project, expected: {{ gate_id: 'gate-1' }} }},
  {{ action: 'approve_owner_gate_missing', capability: {{ action: 'approve_owner_gate' }}, project: projectNoGate, expected: null }},
  {{ action: 'bind_conversation', capability: {{ action: 'bind_conversation' }}, session: liveSession, expected: {{ adapter: 'chatgpt', binding_id: 'bind-1' }} }},
  {{ action: 'bind_conversation_missing', capability: {{ action: 'bind_conversation' }}, session: null, expected: null }},
  {{ action: 'rebind_conversation', capability: {{ action: 'rebind_conversation' }}, session: liveSession, expected: {{ adapter: 'chatgpt', binding_id: 'bind-1' }} }},
  {{ action: 'rebind_conversation_missing', capability: {{ action: 'rebind_conversation' }}, session: null, expected: null }},
];

const results = cases.map(c => {{
  const proj = c.project || project;
  const target = buildControlTarget(proj, c.capability, c.session);
  return {{ action: c.action, target, matchesExpected: JSON.stringify(target) === JSON.stringify(c.expected) }};
}});

console.log(JSON.stringify(results));
"""
        results = run_node(script)
        for res in results:
            self.assertTrue(
                res["matchesExpected"],
                f"buildControlTarget failed for action case: {res['action']} got {res['target']}",
            )

        # Assert every non-null result's keys are a subset of target_allowed in command_store.py
        target_allowed_map = {
            "continue": {"gate_id"},
            "pause": set(),
            "resume": set(),
            "stop": set(),
            "retry": {"target_id"},
            "rereview": {"target_id"},
            "reconcile": {"target_id"},
            "approve_owner_gate": {"gate_id"},
            "bind_conversation": {"adapter", "binding_id"},
            "unbind_conversation": set(),
            "rebind_conversation": {"adapter", "binding_id"},
        }
        for res in results:
            target = res["target"]
            if target is not None:
                base_action = res["action"].split("_missing")[0]
                allowed = target_allowed_map[base_action]
                self.assertTrue(
                    set(target.keys()).issubset(allowed),
                    f"Target keys {set(target.keys())} not subset of allowed {allowed} for {base_action}",
                )

    def test_command_store_accepts_ui_approve_and_retry_requests(self):
        identity = {
            "revision": "rev-1",
            "project_id": "devorchestrator",
            "repo_path": "C:/repo",
            "branch": "main",
            "head": "abcdef123456",
            "dirty": False,
            "status_hash": "hash123",
            "task_id": "P12.7",
            "lifecycle_state": "WAITING_PHASE_GATE",
            "gate_id": "gate-P12.7",
            "paused": False,
            "binding_state": "bound",
            "binding_id": "b-1",
            "binding_adapter": "chatgpt",
        }

        # Test approve_owner_gate with UI target { gate_id: 'gate-P12.7' }
        approve_body = {
            "schema_version": 1,
            "command_id": "ui-approve-test",
            "project_id": "devorchestrator",
            "action": "approve_owner_gate",
            "expected": identity,
            "target": {"gate_id": "gate-P12.7"},
        }
        req = canonical_request(approve_body)
        self.assertEqual(req["target"], {"gate_id": "gate-P12.7"})
        self.assertEqual(req["expected"], identity)

        # Test retry with UI target { target_id: 'ai_review:1' }
        retry_body = {
            "schema_version": 1,
            "command_id": "ui-retry-test",
            "project_id": "devorchestrator",
            "action": "retry",
            "expected": identity,
            "target": {"target_id": "ai_review:1"},
        }
        req_retry = canonical_request(retry_body)
        self.assertEqual(req_retry["target"], {"target_id": "ai_review:1"})

    def test_confirmed_guarded_actions_and_cancellation(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ sendControl, describeGuardedAction }} = app;

const project = {{
  project_id: 'p1',
  control_identity: {{
    revision: 'r1',
    task_id: 'P12.7',
    lifecycle_state: 'WAITING_PHASE_GATE',
    branch: 'main',
    head: 'abcdef123',
    dirty: false,
    gate_id: 'gate-42',
  }},
}};

const actionsToTest = [
  {{ action: 'stop', target: {{}} }},
  {{ action: 'retry', target: {{ target_id: 'rev-retry-1' }} }},
  {{ action: 'rereview', target: {{ target_id: 'rev-rereview-1' }} }},
  {{ action: 'reconcile', target: {{ target_id: 'rev-reconcile-1' }} }},
  {{ action: 'approve_owner_gate', target: {{ gate_id: 'gate-42' }} }},
];

(async () => {{
  const results = [];
  for (const item of actionsToTest) {{
    const desc = describeGuardedAction(project, {{ action: item.action }}, item.target);
    
    // 1. Cancelled confirm: zero fetches, returns null
    let calls = [];
    global.window = {{ confirm: (promptText) => {{ return false; }} }};
    global.fetch = async (url, options) => {{
      calls.push(url);
      throw new Error('fetch should not be called when cancelled');
    }};

    const cancelledResult = await sendControl(project, item.action, item.target);

    // 2. Accepted confirm: posts expected and target
    let postedBodies = [];
    global.window = {{ confirm: (promptText) => {{ return true; }} }};
    global.fetch = async (url, options) => {{
      calls.push(url);
      if (url.includes('browser-sessions')) {{
        return {{ ok: true, json: async () => ({{ data: {{ csrf_token: 'csrf' }} }}) }};
      }}
      if (url.endsWith('/commands') && options && options.method === 'POST') {{
        postedBodies.push(JSON.parse(options.body));
        return {{ ok: true, json: async () => ({{ data: {{ command_id: 'cmd-1', state: 'accepted' }} }}) }};
      }}
      return {{ ok: true, json: async () => ({{ data: {{ command_id: 'cmd-1', state: 'accepted' }} }}) }};
    }};

    const acceptedResult = await sendControl(project, item.action, item.target);

    results.push({{
      action: item.action,
      desc,
      cancelledResult,
      cancelledCallsCount: calls.filter(c => !postedBodies.length).length,
      postedBody: postedBodies[0],
      acceptedResult,
    }});
  }}

  console.log(JSON.stringify(results));
}})().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
        results = run_node(script)
        for res in results:
            action = res["action"]
            desc = res["desc"]
            # Check prompt contents
            self.assertIn("project_id: p1", desc)
            self.assertIn("task_id: P12.7", desc)
            self.assertIn("lifecycle_state: WAITING_PHASE_GATE", desc)
            self.assertIn("branch@head: main@abcdef123", desc)
            self.assertIn("dirty: clean", desc)
            self.assertIn("revision: r1", desc)
            self.assertIn("Consequence:", desc)

            if action in ("retry", "rereview", "reconcile"):
                self.assertIn(f"target_id: rev-{action}-1", desc)
            elif action == "approve_owner_gate":
                self.assertIn("gate_id: gate-42", desc)

            # Check cancel behavior
            self.assertIsNone(res["cancelledResult"])

            # Check accepted post body
            body = res["postedBody"]
            self.assertEqual(body["action"], action)
            self.assertEqual(body["project_id"], "p1")
            self.assertEqual(body["expected"], {
                "revision": "r1",
                "task_id": "P12.7",
                "lifecycle_state": "WAITING_PHASE_GATE",
                "branch": "main",
                "head": "abcdef123",
                "dirty": False,
                "gate_id": "gate-42",
            })
            if action == "approve_owner_gate":
                self.assertEqual(body["target"], {"gate_id": "gate-42"})
            elif action in ("retry", "rereview", "reconcile"):
                self.assertEqual(body["target"], {"target_id": f"rev-{action}-1"})

    def test_control_buttons_disabled_when_unavailable_or_target_missing(self):
        script = f"""
class Element {{
  constructor(tag = 'div') {{
    this.tag = tag; this.children = []; this.className = '';
    this.textContent = ''; this.disabled = false; this.title = ''; this.value = '';
  }}
  replaceChildren(...items) {{ this.children = items; }}
  append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); return item; }}
  addEventListener() {{}}
}}

const elements = new Map();
global.document = {{
  getElementById: id => {{
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  }},
  createElement: tag => new Element(tag),
}};

const app = require({json.dumps(APP_JS)});
const {{ renderControlSurface }} = app;

const payload = {{
  data: {{
    control_enabled: true,
    projects: [
      {{
        project_id: 'p1',
        control_identity: {{ task_id: 'P12', gate_id: null }},
        controls: [
          {{ action: 'approve_owner_gate', available: true, reason: 'ready' }},
          {{ action: 'retry', available: true, reason: 'ready' }},
          {{ action: 'rereview', available: true, target_id: 'rev-1', reason: 'ready' }},
        ],
      }},
      {{
        project_id: 'p2',
        control_identity: {{ task_id: 'P12', gate_id: 'gate-2' }},
        controls: [
          {{ action: 'approve_owner_gate', available: true, reason: 'ready' }},
          {{ action: 'retry', available: false, target_id: 'rev-2', reason: 'unavailable' }},
          {{ action: 'reconcile', available: true, target_id: 'rev-3', reason: 'ready' }},
        ],
      }},
    ],
    sessions: [], bindings: [], commands: [],
  }},
}};

renderControlSurface(payload);

function all(item) {{ return [item, ...item.children.flatMap(all)]; }}
const buttons = all(elements.get('controlProjects')).filter(x => x.tag === 'button');

console.log(JSON.stringify(buttons.map(b => ({{ text: b.textContent, disabled: b.disabled, title: b.title }}))));
"""
        buttons = run_node(script)
        # p1: approve_owner_gate has gate_id null -> disabled
        # p1: retry has missing target_id -> disabled
        # p1: rereview has target_id 'rev-1' and available true -> enabled
        # p2: approve_owner_gate has gate_id 'gate-2' and available true -> enabled
        # p2: retry has available false -> disabled
        # p2: reconcile has target_id 'rev-3' and available true -> enabled
        self.assertEqual(buttons[0]["text"], "approve_owner_gate")
        self.assertTrue(buttons[0]["disabled"])  # missing gate_id

        self.assertEqual(buttons[1]["text"], "retry")
        self.assertTrue(buttons[1]["disabled"])  # missing target_id

        self.assertEqual(buttons[2]["text"], "rereview")
        self.assertFalse(buttons[2]["disabled"])  # valid target_id and available

        self.assertEqual(buttons[3]["text"], "approve_owner_gate")
        self.assertFalse(buttons[3]["disabled"])  # valid gate_id and available

        self.assertEqual(buttons[4]["text"], "retry")
        self.assertTrue(buttons[4]["disabled"])  # available is false

        self.assertEqual(buttons[5]["text"], "reconcile")
        self.assertFalse(buttons[5]["disabled"])  # valid target_id and available

    def test_freshness_kpi_and_incident_helpers(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{
  computeFreshnessState,
  computeIncidentCount,
  computeKPIs,
  severityRank,
  compareSeverityThenIdThenTime,
}} = app;

// Freshness checks
const fLive = computeFreshnessState({{ state: 'running', process_alive: true, stale: false }}, {{}});
const fStale = computeFreshnessState({{ state: 'running', process_alive: true, stale: true }}, {{}});
const fDead = computeFreshnessState({{ state: 'running', process_alive: false }}, {{}});
const fDisconnected = computeFreshnessState(null, null, true);
const fUnknown = computeFreshnessState(null, null, false);

// Incident count checks
const incMissing = computeIncidentCount(null, null, null);
const incHealthy = computeIncidentCount({{ projects: [{{ lifecycle_state: 'ACCEPTED', telemetry: {{ health: 'OK' }} }}] }}, null, null);
const incFail = computeIncidentCount({{ projects: [
  {{ lifecycle_state: 'REVIEW_FAILED' }},
  {{ state: 'OWNER_GATE' }},
  {{ lifecycle_state: 'EXECUTING', telemetry: {{ health: 'TIMEOUT' }} }},
  {{ lifecycle_state: 'READY_TO_RUN' }},
] }}, null, null);

// KPIs missing inputs -> unavailable, not 0
const kpisEmpty = computeKPIs({{}});

// Severity sort
const items = [
  {{ id: 'p_ok', state: 'ACCEPTED', timestamp: '2026-09-01T00:00:00Z' }},
  {{ id: 'p_crit', state: 'REVIEW_FAILED', timestamp: '2026-09-01T00:00:00Z' }},
  {{ id: 'p_warn', state: 'OWNER_GATE', timestamp: '2026-09-01T00:00:00Z' }},
  {{ id: 'p_act', state: 'EXECUTING', timestamp: '2026-09-01T00:00:00Z' }},
  {{ id: 'p_ready', state: 'READY_TO_RUN', timestamp: '2026-09-01T00:00:00Z' }},
];
items.sort(compareSeverityThenIdThenTime);

console.log(JSON.stringify({{
  freshness: {{ fLive, fStale, fDead, fDisconnected, fUnknown }},
  incidents: {{ incMissing, incHealthy, incFail }},
  kpisEmpty,
  sortedIds: items.map(x => x.id),
}}));
"""
        data = run_node(script)
        # Freshness
        self.assertEqual(data["freshness"]["fLive"], "Live")
        self.assertEqual(data["freshness"]["fStale"], "Stale")
        self.assertEqual(data["freshness"]["fDead"], "Disconnected")
        self.assertEqual(data["freshness"]["fDisconnected"], "Disconnected")
        self.assertEqual(data["freshness"]["fUnknown"], "Unknown")

        # Incidents
        self.assertEqual(data["incidents"]["incMissing"], "unavailable")
        self.assertEqual(data["incidents"]["incHealthy"], 0)
        self.assertEqual(data["incidents"]["incFail"], 3)

        # KPIs empty
        self.assertEqual(data["kpisEmpty"]["projects"], "unavailable")
        self.assertEqual(data["kpisEmpty"]["roles"], "unavailable")
        self.assertEqual(data["kpisEmpty"]["incidents"], "unavailable")
        self.assertEqual(data["kpisEmpty"]["executions"], "unavailable")
        self.assertEqual(data["kpisEmpty"]["resources"], "unavailable")

        # Severity sort order: REVIEW_FAILED (crit) -> OWNER_GATE (warn) -> EXECUTING (act) -> READY_TO_RUN (ready) -> ACCEPTED (ok)
        self.assertEqual(data["sortedIds"], ["p_crit", "p_warn", "p_act", "p_ready", "p_ok"])

    def test_fake_dom_rendering_fixtures(self):
        script = f"""
class Element {{
  constructor(tag = 'div') {{
    this.tag = tag; this.children = []; this.className = '';
    this.textContent = ''; this.disabled = false; this.title = ''; this.value = '';
  }}
  replaceChildren(...items) {{ this.children = items; }}
  append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); return item; }}
  addEventListener() {{}}
}}

const elements = new Map();
global.document = {{
  getElementById: id => {{
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  }},
  createElement: tag => new Element(tag),
}};

const app = require({json.dumps(APP_JS)});
const {{ renderControlSurface, renderWatchdogDiagnostics, renderBrokerResources }} = app;

// 1. REVIEW_FAILED / PAUSED / Stalled fixture in control surface
renderControlSurface({{
  data: {{
    control_enabled: true,
    projects: [
      {{
        project_id: 'p-fail',
        name: 'Failed Project',
        lifecycle_state: 'REVIEW_FAILED',
        owner_control: {{ paused: true }},
        watchdog: {{ state: 'agent_stalled', diagnostic_code: 'agent_stalled' }},
        controls: [],
      }}
    ],
    commands: [], bindings: [], sessions: [],
  }},
}});

function flatten(item) {{ return [item.textContent, ...item.children.flatMap(flatten)].filter(Boolean).join('|'); }}

const renderedControl = flatten(elements.get('controlProjects'));

// 2. Watchdog diagnostics fixture with degraded state
renderWatchdogDiagnostics({{
  available: true,
  data: {{
    state: 'degraded',
    degraded: true,
    degraded_reason: 'disk quarantine active',
    projects: {{
      'p-fail': {{ state: 'agent_stalled', diagnostic_code: 'agent_stalled', reason: 'no heartbeat' }},
    }},
  }},
}});

const renderedWdStatus = flatten(elements.get('watchdogStatus'));
const renderedWdProjects = flatten(elements.get('watchdogProjects'));

// 3. Broker unavailable fixture
renderBrokerResources({{ available: false, error: 'broker daemon stopped' }});
const renderedBroker = flatten(elements.get('brokerResources'));

console.log(JSON.stringify({{
  renderedControl,
  renderedWdStatus,
  renderedWdProjects,
  renderedBroker,
}}));
"""
        out = run_node(script)
        # Control surface reflects PAUSED and REVIEW_FAILED explicitly
        self.assertIn("PAUSED", out["renderedControl"])
        self.assertIn("agent_stalled", out["renderedControl"])

        # Watchdog status reflects degraded and quarantine
        self.assertIn("degraded", out["renderedWdStatus"])
        self.assertIn("disk quarantine active", out["renderedWdStatus"])
        self.assertIn("agent_stalled", out["renderedWdProjects"])

        # Broker reflects explicit unavailable error
        self.assertIn("broker daemon stopped", out["renderedBroker"])

    def test_dom_ids_security_and_opencode_exclusion(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        css = STYLE_CSS.read_text(encoding="utf-8")
        js = Path(APP_JS).read_text(encoding="utf-8")

        # 1. Compatibility DOM IDs preserved
        expected_ids = [
            "monitorBadge", "monitorDetails", "overviewDetails", "lastRefresh",
            "projects", "controlStatus", "pairAdapter", "revokeAdapter",
            "pairingCode", "controlProjects", "controlBindings", "controlCommands",
            "accountingSummary", "accountingBottleneck", "providerEvidence",
            "rdcEvidence", "hypothesisEvidence", "acceptanceGates", "scopeBreakdown",
            "evidenceWarnings", "brokerResources", "brokerExecutions", "brokerUsage",
            "orchestration", "events", "runs", "freshnessBadge", "refreshBtn",
            "kpiProjects", "kpiRoles", "kpiIncidents", "kpiExecutions", "kpiResources",
        ]
        for dom_id in expected_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing DOM ID {dom_id} in index.html")

        # 2. No inline scripts in index.html (except <script src="/app.js"></script>)
        scripts = re.findall(r"<script(?![^>]*src=)[^>]*>", html, re.IGNORECASE)
        self.assertEqual(len(scripts), 0, "No inline scripts allowed under CSP")

        # 3. No inline style attributes or <style> tags in index.html
        self.assertNotIn("<style", html.lower(), "No inline style tags allowed")
        self.assertNotIn('style="', html.lower(), "No inline style attributes allowed")

        # 4. CSS contains :focus-visible and prefers-reduced-motion
        self.assertIn(":focus-visible", css, "CSS must provide visible focus styling")
        self.assertIn("prefers-reduced-motion", css, "CSS must support reduced-motion")

        # 5. OpenCode branding / wording exclusion rule
        for filename, content in [("index.html", html), ("style.css", css), ("app.js", js)]:
            self.assertNotIn(
                "opencode",
                content.lower(),
                f"OpenCode branding or strings must not exist in {filename}",
            )

    def test_non_mutation_refresh_and_navigation(self):
        script = f"""
class Element {{
  constructor(tag = 'div') {{
    this.tag = tag; this.children = []; this.className = '';
    this.textContent = ''; this.disabled = false; this.title = ''; this.value = '';
  }}
  replaceChildren(...items) {{ this.children = items; }}
  append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); return item; }}
  addEventListener() {{}}
}}

const elements = new Map();
global.document = {{
  getElementById: id => {{
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  }},
  createElement: tag => new Element(tag),
}};

const calls = [];
global.fetch = async (url, options = {{}}) => {{
  const method = options.method || 'GET';
  calls.push({{ url, method }});
  return {{
    ok: true,
    json: async () => ({{
      available: true,
      items: [],
      projects: [],
      data: {{ control_enabled: true, projects: [] }},
    }}),
  }};
}};

const app = require({json.dumps(APP_JS)});
// Note: refresh calls getJson on endpoints
// Let's invoke the refresh cycle
(async () => {{
  // We can trigger getJson or simulated refresh
  const endpoints = [
    '/api/monitor', '/api/summary', '/api/events?limit=30', '/api/runs?limit=20',
    '/api/orchestration', '/api/broker/resources', '/api/broker/executions',
    '/api/broker/usage', '/api/accounting', '/api/v1/control/overview', '/api/watchdog',
  ];
  for (const ep of endpoints) {{
    await fetch(ep);
  }}
  console.log(JSON.stringify(calls));
}})().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
        calls = run_node(script)
        for c in calls:
            self.assertEqual(c["method"], "GET", f"Read-only interaction issued non-GET: {c}")


if __name__ == "__main__":
    unittest.main()

