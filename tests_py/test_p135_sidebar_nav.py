import html.parser
import json
import re
import subprocess
import unittest
from pathlib import Path

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


class P135SidebarNavigationTests(unittest.TestCase):
    def test_sidebar_nav_exists_and_section_nav_removed(self):
        html_content = INDEX_HTML.read_text(encoding="utf-8")
        # Sidebar nav exists
        self.assertRegex(
            html_content,
            r'<nav[^>]*class=["\'][^"\']*\bsidebar\b[^"\']*["\'][^>]*aria-label=["\']Primary["\']',
            "Persistent <nav class=\"sidebar\" aria-label=\"Primary\"> must exist",
        )
        # Top-tab section-nav markup must be completely removed
        self.assertNotIn("section-nav", html_content, "Old section-nav top-tab markup must be deleted")

    def test_sidebar_has_exactly_six_links_resolving_to_views_and_one_to_one_mapping(self):
        html_content = INDEX_HTML.read_text(encoding="utf-8")

        # Extract links in sidebar
        sidebar_match = re.search(r'<nav[^>]*class=["\'][^"\']*\bsidebar\b[^"\']*["\'][^>]*>(.*?)</nav>', html_content, re.DOTALL)
        self.assertIsNotNone(sidebar_match, "Sidebar nav block not found")
        sidebar_html = sidebar_match.group(1)

        links = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', sidebar_html, re.DOTALL)
        self.assertEqual(len(links), 6, f"Sidebar nav must have exactly 6 links, found {len(links)}: {links}")

        hrefs = [href for href, _ in links]
        expected_hrefs = [
            "#overview",
            "#projects-section",
            "#resources-section",
            "#accounting-section",
            "#logs-section",
            "#system-section",
        ]
        self.assertEqual(hrefs, expected_hrefs, f"Sidebar links must be in exact order: {expected_hrefs}")

        # Extract all view-sections in main
        section_ids = re.findall(r'<section\s+[^>]*id=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*\bview-section\b[^"\']*', html_content)
        expected_view_ids = [
            "overview",
            "projects-section",
            "resources-section",
            "accounting-section",
            "logs-section",
            "system-section",
        ]
        self.assertEqual(sorted(section_ids), sorted(expected_view_ids))

        # Check each href resolves to its section
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ resolveViewId }} = app;
const hrefs = {json.dumps(hrefs)};
const resolved = hrefs.map(h => resolveViewId(h));
console.log(JSON.stringify(resolved));
"""
        resolved_ids = run_node(script)
        self.assertEqual(resolved_ids, expected_view_ids)

    def test_host_to_view_mapping_and_persistent_chrome(self):
        html_content = INDEX_HTML.read_text(encoding="utf-8")

        # Persistent chrome must be outside every view-section
        persistent_ids = [
            "daemonBadge", "monitorBadge", "watchdogBadge", "freshnessBadge", "lastRefresh", "refreshBtn",
            "kpiProjects", "kpiRoles", "kpiIncidents", "kpiExecutions", "kpiResources",
        ]
        for dom_id in persistent_ids:
            self.assertIn(f'id="{dom_id}"', html_content)

        # Parse sections and their descendants
        class SectionParser(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.current_section = None
                self.section_hosts = {}

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                tag_id = attrs_dict.get("id")
                classes = attrs_dict.get("class", "").split()
                if tag == "section" and "view-section" in classes:
                    self.current_section = tag_id
                    self.section_hosts[self.current_section] = []
                elif self.current_section and tag_id:
                    self.section_hosts[self.current_section].append(tag_id)

            def handle_endtag(self, tag):
                pass

        parser = SectionParser()
        parser.feed(html_content)
        hosts = parser.section_hosts

        # Authoritative host-to-view mapping
        # 1. overview: monitorDetails, overviewDetails
        self.assertIn("monitorDetails", hosts.get("overview", []))
        self.assertIn("overviewDetails", hosts.get("overview", []))

        # 2. projects-section: projects, orchestration, controlStatus, controlProjects
        proj_hosts = hosts.get("projects-section", [])
        self.assertIn("projects", proj_hosts)
        self.assertIn("orchestration", proj_hosts)
        self.assertIn("controlStatus", proj_hosts)
        self.assertIn("controlProjects", proj_hosts)

        # 3. resources-section: brokerResources, brokerExecutions
        res_hosts = hosts.get("resources-section", [])
        self.assertIn("brokerResources", res_hosts)
        self.assertIn("brokerExecutions", res_hosts)

        # 4. accounting-section: brokerUsage, accountingSummary, accountingBottleneck,
        # providerEvidence, rdcEvidence, hypothesisEvidence, acceptanceGates, scopeBreakdown, evidenceWarnings
        acct_hosts = hosts.get("accounting-section", [])
        for hid in [
            "brokerUsage", "accountingSummary", "accountingBottleneck",
            "providerEvidence", "rdcEvidence", "hypothesisEvidence",
            "acceptanceGates", "scopeBreakdown", "evidenceWarnings",
        ]:
            self.assertIn(hid, acct_hosts, f"{hid} must live inside accounting-section")

        # 5. logs-section: events, runs
        log_hosts = hosts.get("logs-section", [])
        self.assertIn("events", log_hosts)
        self.assertIn("runs", log_hosts)

        # 6. system-section: watchdogStatus, watchdogProjects, pairAdapter, revokeAdapter, pairingCode, controlBindings, controlCommands
        sys_hosts = hosts.get("system-section", [])
        for hid in [
            "watchdogStatus", "watchdogProjects", "pairAdapter", "revokeAdapter",
            "pairingCode", "controlBindings", "controlCommands",
        ]:
            self.assertIn(hid, sys_hosts, f"{hid} must live inside system-section")

        # Subregions survive for legacy aliases
        self.assertIn("orchestration-section", proj_hosts)
        self.assertIn("controls", proj_hosts)
        self.assertIn("runs-section", log_hosts)
        self.assertIn("watchdog-section", sys_hosts)

    def test_resolver_behavior_for_canonical_aliases_and_fallbacks(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ resolveViewId }} = app;

const testCases = [
  // 6 canonical view IDs (with and without hash)
  {{ input: '#overview', expected: 'overview' }},
  {{ input: 'overview', expected: 'overview' }},
  {{ input: '#projects-section', expected: 'projects-section' }},
  {{ input: 'projects-section', expected: 'projects-section' }},
  {{ input: '#resources-section', expected: 'resources-section' }},
  {{ input: 'resources-section', expected: 'resources-section' }},
  {{ input: '#accounting-section', expected: 'accounting-section' }},
  {{ input: 'accounting-section', expected: 'accounting-section' }},
  {{ input: '#logs-section', expected: 'logs-section' }},
  {{ input: 'logs-section', expected: 'logs-section' }},
  {{ input: '#system-section', expected: 'system-section' }},
  {{ input: 'system-section', expected: 'system-section' }},

  // 4 legacy aliases (with and without hash)
  {{ input: '#controls', expected: 'projects-section' }},
  {{ input: 'controls', expected: 'projects-section' }},
  {{ input: '#orchestration-section', expected: 'projects-section' }},
  {{ input: 'orchestration-section', expected: 'projects-section' }},
  {{ input: '#runs-section', expected: 'logs-section' }},
  {{ input: 'runs-section', expected: 'logs-section' }},
  {{ input: '#watchdog-section', expected: 'system-section' }},
  {{ input: 'watchdog-section', expected: 'system-section' }},

  // Unknown / empty / whitespace / null / undefined fallback to overview
  {{ input: '', expected: 'overview' }},
  {{ input: '#', expected: 'overview' }},
  {{ input: '   ', expected: 'overview' }},
  {{ input: null, expected: 'overview' }},
  {{ input: undefined, expected: 'overview' }},
  {{ input: '#not-a-real-view', expected: 'overview' }},
  {{ input: 'random-text', expected: 'overview' }},
];

const results = testCases.map(tc => ({{
  input: String(tc.input),
  actual: resolveViewId(tc.input),
  expected: tc.expected,
  ok: resolveViewId(tc.input) === tc.expected,
}}));

console.log(JSON.stringify(results));
"""
        results = run_node(script)
        for res in results:
            self.assertTrue(
                res["ok"],
                f"resolveViewId failed for input: {res['input']!r} -> got {res['actual']!r}, expected {res['expected']!r}",
            )

    def test_activator_toggles_sections_and_aria_current_with_zero_fetch(self):
        script = f"""
class Element {{
  constructor(tag = 'div', id = '') {{
    this.tag = tag;
    this.id = id;
    this.children = [];
    this.className = '';
    this.classList = {{
      _set: new Set(),
      add: (c) => this.classList._set.add(c),
      remove: (c) => this.classList._set.delete(c),
      contains: (c) => this.classList._set.has(c),
    }};
    this.attributes = new Map();
    this.focused = false;
  }}
  setAttribute(k, v) {{ this.attributes.set(k, String(v)); }}
  getAttribute(k) {{ return this.attributes.get(k) || null; }}
  removeAttribute(k) {{ this.attributes.delete(k); }}
  hasAttribute(k) {{ return this.attributes.has(k); }}
  focus() {{ this.focused = true; }}
}}

const viewIds = ['overview', 'projects-section', 'resources-section', 'accounting-section', 'logs-section', 'system-section'];
const sections = viewIds.map(id => new Element('section', id));
const headings = viewIds.map(id => new Element('h2', id + '-heading'));
const links = viewIds.map(id => {{
  const el = new Element('a');
  el.setAttribute('href', '#' + id);
  return el;
}});

const elementsById = new Map();
viewIds.forEach((id, idx) => {{
  elementsById.set(id, sections[idx]);
  elementsById.set(id + '-heading', headings[idx]);
}});

global.document = {{
  querySelectorAll: (selector) => {{
    if (selector.includes('.view-section')) return sections;
    if (selector.includes('sidebar a') || selector.includes('.sidebar-nav a')) return links;
    return [];
  }},
  getElementById: (id) => elementsById.get(id) || null,
  querySelector: () => null,
}};

let fetchCalls = 0;
global.fetch = async () => {{
  fetchCalls++;
  throw new Error('fetch should not be called by activator');
}};

const app = require({json.dumps(APP_JS)});
const {{ activateView }} = app;

// Initial state: activate projects-section
const ret1 = activateView('#projects-section');
const check1 = {{
  ret: ret1,
  projectsHidden: sections[1].hasAttribute('hidden'),
  projectsActive: sections[1].classList.contains('is-active'),
  overviewHidden: sections[0].hasAttribute('hidden'),
  overviewActive: sections[0].classList.contains('is-active'),
  link0Aria: links[0].getAttribute('aria-current'),
  link1Aria: links[1].getAttribute('aria-current'),
  heading1Focused: headings[1].focused,
}};

// Switch to system-section via legacy alias #watchdog-section
headings[1].focused = false;
const ret2 = activateView('#watchdog-section');
const check2 = {{
  ret: ret2,
  systemHidden: sections[5].hasAttribute('hidden'),
  systemActive: sections[5].classList.contains('is-active'),
  projectsHidden: sections[1].hasAttribute('hidden'),
  projectsActive: sections[1].classList.contains('is-active'),
  link1Aria: links[1].getAttribute('aria-current'),
  link5Aria: links[5].getAttribute('aria-current'),
  heading5Focused: headings[5].focused,
}};

console.log(JSON.stringify({{
  fetchCalls,
  check1,
  check2,
}}));
"""
        out = run_node(script)
        self.assertEqual(out["fetchCalls"], 0, "activateView must never call fetch")

        # Check 1: projects-section active
        self.assertEqual(out["check1"]["ret"], "projects-section")
        self.assertFalse(out["check1"]["projectsHidden"])
        self.assertTrue(out["check1"]["projectsActive"])
        self.assertTrue(out["check1"]["overviewHidden"])
        self.assertFalse(out["check1"]["overviewActive"])
        self.assertIsNone(out["check1"]["link0Aria"])
        self.assertEqual(out["check1"]["link1Aria"], "page")
        self.assertTrue(out["check1"]["heading1Focused"])

        # Check 2: legacy alias watchdog-section activates system-section
        self.assertEqual(out["check2"]["ret"], "system-section")
        self.assertFalse(out["check2"]["systemHidden"])
        self.assertTrue(out["check2"]["systemActive"])
        self.assertTrue(out["check2"]["projectsHidden"])
        self.assertFalse(out["check2"]["projectsActive"])
        self.assertIsNone(out["check2"]["link1Aria"])
        self.assertEqual(out["check2"]["link5Aria"], "page")
        self.assertTrue(out["check2"]["heading5Focused"])

    def test_refresh_populates_hosts_inside_inactive_views(self):
        script = f"""
class Element {{
  constructor(tag = 'div', id = '') {{
    this.tag = tag; this.id = id; this.children = [];
    this.textContent = ''; this.className = '';
    this.attributes = new Map();
  }}
  setAttribute(k, v) {{ this.attributes.set(k, String(v)); }}
  getAttribute(k) {{ return this.attributes.get(k) || null; }}
  removeAttribute(k) {{ this.attributes.delete(k); }}
  hasAttribute(k) {{ return this.attributes.has(k); }}
  replaceChildren(...items) {{ this.children = items; }}
  append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); return item; }}
}}

const elements = new Map();
global.document = {{
  getElementById: (id) => {{
    if (!elements.has(id)) elements.set(id, new Element('div', id));
    return elements.get(id);
  }},
  createElement: (tag) => new Element(tag),
}};

const app = require({json.dumps(APP_JS)});
const {{
  renderProjects,
  renderBrokerResources,
  renderAccounting,
  renderControlSurface,
}} = app;

// Simulate that overview is active, but other sections are hidden
// We now render into hosts that reside in hidden sections:
// 1. projects (in hidden projects-section)
renderProjects({{
  projects: [
    {{ id: 'p1', name: 'Proj 1', lifecycle_state: 'EXECUTING', telemetry: {{ health: 'OK' }} }}
  ]
}}, []);

// 2. brokerResources (in hidden resources-section)
renderBrokerResources({{
  available: true,
  resources: [
    {{ resource_id: 'r1', provider: 'test', model: 'm1', enabled: true, state: {{ available: true }} }}
  ]
}});

// 3. accountingSummary (in hidden accounting-section)
renderAccounting({{
  available: true,
  data_status: {{ accounting: 'measured', provider: 'measured', rdc: 'measured' }},
  accounting: {{ edr: 0.85, accepted_productive_seconds: 100, rejected_attempt_seconds: 10 }},
  dominant_bottleneck: null,
  hypotheses: {{}},
  time_breakdown: [],
  acceptance: {{ gates: [] }},
  warnings: [],
}});

// 4. controlProjects (in hidden projects-section controls region)
renderControlSurface({{
  data: {{
    control_enabled: true,
    projects: [
      {{ project_id: 'p1', lifecycle_state: 'EXECUTING', controls: [] }}
    ],
    bindings: [],
    commands: [],
    sessions: [],
  }}
}});

const result = {{
  projectsChildCount: elements.get('projects').children.length,
  brokerChildCount: elements.get('brokerResources').children.length,
  accountingSummaryChildCount: elements.get('accountingSummary').children.length,
  controlProjectsChildCount: elements.get('controlProjects').children.length,
}};

console.log(JSON.stringify(result));
"""
        out = run_node(script)
        self.assertGreater(out["projectsChildCount"], 0, "projects host in inactive view must still be populated")
        self.assertGreater(out["brokerChildCount"], 0, "brokerResources host in inactive view must still be populated")
        self.assertGreater(out["accountingSummaryChildCount"], 0, "accountingSummary host in inactive view must still be populated")
        self.assertGreater(out["controlProjectsChildCount"], 0, "controlProjects host in inactive view must still be populated")

    def test_guarded_controls_regions_present_and_separated(self):
        html_content = INDEX_HTML.read_text(encoding="utf-8")

        # 1. In projects-section, controls-region is present and visually separated
        proj_match = re.search(r'<section\s+[^>]*id=["\']projects-section["\'][^>]*>(.*?)</section>', html_content, re.DOTALL)
        self.assertIsNotNone(proj_match, "projects-section not found")
        proj_html = proj_match.group(1)

        self.assertIn("controls-region", proj_html, "controls-region must be present in projects-section")
        self.assertRegex(
            proj_html,
            r'<div[^>]*class=["\'][^"\']*\bcontrols-region\b[^"\']*["\'][^>]*>[\s\S]*?<h3>Guarded Project Controls</h3>',
            "Guarded Project Controls heading must label controls-region in projects-section",
        )
        self.assertIn('id="controlProjects"', proj_html)
        self.assertIn('id="controlStatus"', proj_html)

        # 2. In system-section, controls-region is present with pairing/bindings
        sys_match = re.search(r'<section\s+[^>]*id=["\']system-section["\'][^>]*>(.*?)</section>', html_content, re.DOTALL)
        self.assertIsNotNone(sys_match, "system-section not found")
        sys_html = sys_match.group(1)

        self.assertIn("controls-region", sys_html, "controls-region must be present in system-section")
        self.assertRegex(
            sys_html,
            r'<div[^>]*class=["\'][^"\']*\bcontrols-region\b[^"\']*["\'][^>]*>[\s\S]*?<h3>Guarded System Controls</h3>',
            "Guarded System Controls heading must label controls-region in system-section",
        )
        self.assertIn('id="pairAdapter"', sys_html)
        self.assertIn('id="revokeAdapter"', sys_html)
        self.assertIn('id="pairingCode"', sys_html)
        self.assertIn('id="controlBindings"', sys_html)
        self.assertIn('id="controlCommands"', sys_html)

    def test_security_and_accessibility_requirements(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        css = STYLE_CSS.read_text(encoding="utf-8")
        js = Path(APP_JS).read_text(encoding="utf-8")

        # Skip link exists
        self.assertIn('class="skip-link"', html)
        self.assertIn('href="#mainContent"', html)

        # No inline scripts
        scripts = re.findall(r"<script(?![^>]*src=)[^>]*>", html, re.IGNORECASE)
        self.assertEqual(len(scripts), 0, "No inline scripts allowed under CSP")

        # No inline style tags or attributes
        self.assertNotIn("<style", html.lower(), "No inline style tags allowed")
        self.assertNotIn('style="', html.lower(), "No inline style attributes allowed")

        # CSS features
        self.assertIn(":focus-visible", css, "CSS must provide visible focus styling")
        self.assertIn("prefers-reduced-motion", css, "CSS must support reduced-motion")
        self.assertIn(".view-section[hidden]", css, "CSS must contain .view-section[hidden] rule")

        # OpenCode branding / wording exclusion rule
        for filename, content in [("index.html", html), ("style.css", css), ("app.js", js)]:
            self.assertNotIn(
                "opencode",
                content.lower(),
                f"OpenCode branding or strings must not exist in {filename}",
            )


if __name__ == "__main__":
    unittest.main()
