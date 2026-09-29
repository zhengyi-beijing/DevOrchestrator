from __future__ import annotations

import html.parser
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = str(ROOT / "web" / "app.js")
INDEX_HTML = ROOT / "web" / "index.html"


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


class P19WebConsoleUITests(unittest.TestCase):
    def test_required_dom_element_ids_exist_in_index_html(self):
        html_content = INDEX_HTML.read_text(encoding="utf-8")
        required_ids = [
            "opsSessionState",
            "opsReconnect",
            "opsProject",
            "opsHost",
            "opsCommandRef",
            "opsCommandCatalog",
            "opsParameters",
            "opsWorkingDirectory",
            "opsTimeout",
            "opsIdempotencyKey",
            "opsExec",
            "opsSpawn",
            "opsJobId",
            "opsPoll",
            "opsCancel",
            "opsPath",
            "opsOffset",
            "opsMaxBytes",
            "opsRead",
            "opsStat",
            "opsFile",
            "opsStageWrite",
            "opsTargetPath",
            "opsIfAbsent",
            "opsExpectedSha256",
            "opsCommitWrite",
            "opsResult",
            "opsEvidence",
            "opsRefreshEvidence",
        ]
        for dom_id in required_ids:
            self.assertIn(f'id="{dom_id}"', html_content, f"Required DOM id {dom_id!r} not found in index.html")

    def test_view_ids_membership_and_resolver(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ VIEW_IDS, resolveViewId, isKnownViewHash }} = app;

console.log(JSON.stringify({{
  hasOperations: VIEW_IDS.has('operations-section'),
  resolvedHash: resolveViewId('#operations-section'),
  resolvedClean: resolveViewId('operations-section'),
  isKnownHash: isKnownViewHash('#operations-section'),
  isKnownClean: isKnownViewHash('operations-section'),
}}));
"""
        res = run_node(script)
        self.assertTrue(res["hasOperations"])
        self.assertEqual(res["resolvedHash"], "operations-section")
        self.assertEqual(res["resolvedClean"], "operations-section")
        self.assertTrue(res["isKnownHash"])
        self.assertTrue(res["isKnownClean"])

    def test_build_transport_request_pure_builder_contracts(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ buildTransportRequest, OPERATION_KINDS }} = app;

const kinds = Array.from(OPERATION_KINDS);
const outputs = {{}};

// 1. Exec
outputs.exec = buildTransportRequest('exec', {{
  project_id: 'p1', host_id: 'local', command_ref: 'cmd_a',
  parameters: '{{"arg": "val"}}', expected_working_directory: '.',
  timeout_seconds: 60, idempotency_key: 'idem-1'
}});

// 2. Spawn
outputs.spawn = buildTransportRequest('spawn', {{
  project_id: 'p1', host_id: 'local', command_ref: 'cmd_b',
  parameters: {{ num: 42 }}, expected_working_directory: 'sub',
  timeout_seconds: 120, idempotency_key: 'idem-2'
}});

// 3. Poll
outputs.poll = buildTransportRequest('poll', {{
  project_id: 'p1', host_id: 'local', operation_id: 'job-123'
}});

// 4. Cancel
outputs.cancel = buildTransportRequest('cancel', {{
  project_id: 'p1', host_id: 'local', operation_id: 'job-123', reason: 'user cancelled'
}});

// 5. Read
outputs.read = buildTransportRequest('read', {{
  project_id: 'p1', host_id: 'local', path: 'dir/file.txt', offset_bytes: 10, max_bytes: 1024
}});

// 6. Stat
outputs.stat = buildTransportRequest('stat', {{
  project_id: 'p1', host_id: 'local', path: 'dir/file.txt'
}});

// 7. Stage Write
outputs.stage_write = buildTransportRequest('stage_write', {{
  project_id: 'p1', host_id: 'local', content_base64: 'aGVsbG8=',
  decoded_size_bytes: 5, content_sha256: 'sha256:abc'
}});

// 8. Write with if_absent
outputs.write_absent = buildTransportRequest('write', {{
  project_id: 'p1', host_id: 'local', target_path: 'dir/new.bin',
  content_ref: 'blob-1', if_absent: true, idempotency_key: 'idem-write-1'
}});

// 9. Write with expected_sha256
outputs.write_cas = buildTransportRequest('write', {{
  project_id: 'p1', host_id: 'local', target_path: 'dir/old.bin',
  content_ref: 'blob-2', expected_sha256: 'sha256:prev', idempotency_key: 'idem-write-2'
}});

// Precondition validation errors
let bothPreconditionsError = null;
try {{
  buildTransportRequest('write', {{
    target_path: 't', content_ref: 'c', if_absent: true, expected_sha256: 'sha256:x'
  }});
}} catch (e) {{
  bothPreconditionsError = e.message;
}}

let noPreconditionError = null;
try {{
  buildTransportRequest('write', {{
    target_path: 't', content_ref: 'c'
  }});
}} catch (e) {{
  noPreconditionError = e.message;
}}

let invalidParamsError = null;
try {{
  buildTransportRequest('exec', {{
    command_ref: 'c', parameters: '[1, 2, 3]'
  }});
}} catch (e) {{
  invalidParamsError = e.message;
}}

let oversizedUploadError = null;
try {{
  buildTransportRequest('stage_write', {{
    content_base64: 'x'.repeat(17 * 1024 * 1024)
  }});
}} catch (e) {{
  oversizedUploadError = e.message;
}}

console.log(JSON.stringify({{
  kinds,
  outputs,
  bothPreconditionsError,
  noPreconditionError,
  invalidParamsError,
  oversizedUploadError,
}}));
"""
        res = run_node(script)
        self.assertEqual(len(res["kinds"]), 8)
        self.assertIn("exec", res["kinds"])
        self.assertIn("write", res["kinds"])

        # Exec check
        self.assertEqual(res["outputs"]["exec"]["method"], "POST")
        self.assertEqual(res["outputs"]["exec"]["url"], "/api/v1/control/transport/exec")
        self.assertEqual(res["outputs"]["exec"]["body"]["parameters"], {"arg": "val"})

        # Spawn check
        self.assertEqual(res["outputs"]["spawn"]["method"], "POST")
        self.assertEqual(res["outputs"]["spawn"]["url"], "/api/v1/control/transport/spawn")
        self.assertEqual(res["outputs"]["spawn"]["body"]["command_ref"], "cmd_b")

        # Poll check
        self.assertEqual(res["outputs"]["poll"]["method"], "POST")
        self.assertEqual(res["outputs"]["poll"]["body"]["operation_id"], "job-123")

        # Cancel check
        self.assertEqual(res["outputs"]["cancel"]["method"], "POST")
        self.assertEqual(res["outputs"]["cancel"]["body"]["reason"], "user cancelled")

        # Read check
        self.assertEqual(res["outputs"]["read"]["method"], "GET")
        self.assertIn("offset_bytes=10", res["outputs"]["read"]["url"])
        self.assertIn("max_bytes=1024", res["outputs"]["read"]["url"])

        # Stat check
        self.assertEqual(res["outputs"]["stat"]["method"], "GET")
        self.assertIn("path=dir%2Ffile.txt", res["outputs"]["stat"]["url"])

        # Write checks
        self.assertTrue(res["outputs"]["write_absent"]["body"].get("if_absent"))
        self.assertEqual(res["outputs"]["write_cas"]["body"].get("expected_sha256"), "sha256:prev")

        # Validations
        self.assertIn("not both", res["bothPreconditionsError"])
        self.assertIn("exactly one precondition", res["noPreconditionError"])
        self.assertIn("JSON object", res["invalidParamsError"])
        self.assertIn("maximum size", res["oversizedUploadError"])

    def test_confirmation_prompt_gate(self):
        script = f"""
const app = require({json.dumps(APP_JS)});
const {{ confirmOperationPrompt }} = app;

console.log(JSON.stringify({{
  spawnPrompt: confirmOperationPrompt('spawn', {{ project_id: 'p1', host_id: 'local', command_ref: 'run_tests' }}),
  cancelPrompt: confirmOperationPrompt('cancel', {{ project_id: 'p1', host_id: 'local', operation_id: 'job-99' }}),
  writeAbsentPrompt: confirmOperationPrompt('write', {{ project_id: 'p1', host_id: 'local', target_path: 'f.txt', if_absent: true }}),
  writeCasPrompt: confirmOperationPrompt('write', {{ project_id: 'p1', host_id: 'local', target_path: 'f.txt', expected_sha256: 'sha256:123' }}),
  execPrompt: confirmOperationPrompt('exec', {{ command_ref: 'c' }}),
  readPrompt: confirmOperationPrompt('read', {{ path: 'p' }}),
  statPrompt: confirmOperationPrompt('stat', {{ path: 'p' }}),
  stageWritePrompt: confirmOperationPrompt('stage_write', {{}}),
}}));
"""
        res = run_node(script)
        self.assertIsNotNone(res["spawnPrompt"])
        self.assertIn("run_tests", res["spawnPrompt"])
        self.assertIsNotNone(res["cancelPrompt"])
        self.assertIn("job-99", res["cancelPrompt"])
        self.assertIsNotNone(res["writeAbsentPrompt"])
        self.assertIn("if_absent=true", res["writeAbsentPrompt"])
        self.assertIsNotNone(res["writeCasPrompt"])
        self.assertIn("sha256:123", res["writeCasPrompt"])

        self.assertIsNone(res["execPrompt"])
        self.assertIsNone(res["readPrompt"])
        self.assertIsNone(res["statPrompt"])
        self.assertIsNone(res["stageWritePrompt"])

    def test_safe_text_rendering_no_innerhtml_or_xss(self):
        script = f"""
class MockElement {{
  constructor(tag = 'div', className = '') {{
    this.tagName = tag.toUpperCase();
    this.className = className;
    this.children = [];
    this.textContent = '';
    this.attributes = new Map();
  }}
  appendChild(child) {{ this.children.push(child); return child; }}
  setAttribute(k, v) {{ this.attributes.set(k, String(v)); }}
  getAttribute(k) {{ return this.attributes.get(k) || null; }}
  replaceChildren(...items) {{ this.children = items; }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
  }}
  get firstChild() {{ return this.children[0] || null; }}
}}

global.document = {{
  createElement: (tag) => new MockElement(tag),
}};

const app = require({json.dumps(APP_JS)});
const {{ renderOperationResult, renderTransportEvidence }} = app;

const container1 = new MockElement('div', 'opsResult');
renderOperationResult(container1, {{
  status: 'ok',
  stdout: '<img src=x onerror=alert(1)>',
  stderr: '<script>alert(2)</script>',
  error: '<b onmouseover=evil()>error</b>',
  path: '<svg/onload=alert(3)>',
  request_id: 'req-<script>',
}});

const container2 = new MockElement('div', 'opsEvidence');
renderTransportEvidence(container2, {{
  operations: [
    {{ operation: 'exec', request_id: '<script>req</script>', error: '<img src=1>' }}
  ]
}});

function collectTextAndTags(el) {{
  const tags = [el.tagName];
  let text = el.textContent || '';
  for (const child of el.children) {{
    const sub = collectTextAndTags(child);
    tags.push(...sub.tags);
    text += ' ' + sub.text;
  }}
  return {{ tags, text }};
}}

const res1 = collectTextAndTags(container1);
const res2 = collectTextAndTags(container2);

console.log(JSON.stringify({{
  tags1: res1.tags,
  text1: res1.text,
  tags2: res2.tags,
  text2: res2.text,
}}));
"""
        res = run_node(script)
        # Verify no IMG, SCRIPT, SVG tags were created as DOM elements
        for tag in ["IMG", "SCRIPT", "SVG"]:
            self.assertNotIn(tag, res["tags1"])
            self.assertNotIn(tag, res["tags2"])
        # Verify the raw strings were preserved safely as text
        self.assertIn("<img src=x onerror=alert(1)>", res["text1"])
        self.assertIn("<script>alert(2)</script>", res["text1"])
        self.assertIn("<script>req</script>", res["text2"])

    def test_app_js_source_hygiene_no_token_no_persistent_storage(self):
        js = Path(APP_JS).read_text(encoding="utf-8")
        # No master bearer token or filesystem token reading
        self.assertNotIn("api-token", js)
        self.assertNotIn("API_TOKEN", js)
        # No localStorage, sessionStorage, indexedDB, or document.cookie
        self.assertNotIn("localStorage", js)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("indexedDB", js)
        self.assertNotIn("document.cookie", js)

    def test_control_fetch_reauthenticates_at_most_once_and_never_on_5xx(self):
        script = f"""
let fetchLog = [];
let sessionCalls = 0;

global.fetch = async (url, opts = {{}}) => {{
  fetchLog.push({{ url, method: opts.method || 'GET', headers: opts.headers || {{}} }});
  if (url === '/api/v1/control/browser-sessions') {{
    sessionCalls++;
    return {{
      ok: true,
      status: 201,
      json: async () => ({{ data: {{ csrf_token: 'csrf-' + sessionCalls }} }}),
    }};
  }}
  if (url === '/test-401') {{
    return {{
      ok: false,
      status: 401,
      json: async () => ({{ message: 'unauthorized' }}),
    }};
  }}
  if (url === '/test-500') {{
    return {{
      ok: false,
      status: 500,
      json: async () => ({{ message: 'server error' }}),
    }};
  }}
  return {{
    ok: true,
    status: 200,
    headers: new Map([['X-DevOrch-Request-ID', 'req-ok']]),
    json: async () => ({{ status: 'ok' }}),
  }};
}};

const app = require({json.dumps(APP_JS)});
const {{ controlFetch, resetControlSession }} = app;

(async () => {{
  // 1. First call on normal 200
  resetControlSession();
  sessionCalls = 0;
  fetchLog = [];
  const res1 = await controlFetch('/test-200');
  const normalSessionCalls = sessionCalls;
  const normalFetchCount = fetchLog.length;

  // 2. Call on 401: re-authenticates at most once, retries once, then returns 401
  sessionCalls = 0;
  fetchLog = [];
  const res2 = await controlFetch('/test-401');
  const reauthSessionCalls = sessionCalls;
  const reauthFetchCount = fetchLog.length;

  // 3. Call on 500: does NOT re-authenticate
  sessionCalls = 0;
  fetchLog = [];
  const res3 = await controlFetch('/test-500');
  const error500SessionCalls = sessionCalls;
  const error500FetchCount = fetchLog.length;

  console.log(JSON.stringify({{
    normalSessionCalls,
    normalFetchCount,
    reauthSessionCalls,
    reauthFetchCount,
    res2Status: res2.status,
    error500SessionCalls,
    error500FetchCount,
    res3Status: res3.status,
  }}));
}})();
"""
        res = run_node(script)
        # Normal call established session once, made 1 business fetch
        self.assertEqual(res["normalSessionCalls"], 1)
        self.assertEqual(res["normalFetchCount"], 2)  # session + endpoint

        # 401 call: 1 initial session + 1 retry session, 2 endpoint fetches (initial + 1 retry)
        self.assertEqual(res["reauthSessionCalls"], 1)  # force session call
        self.assertEqual(res["reauthFetchCount"], 3)   # initial fetch -> 401 -> session -> retry fetch
        self.assertEqual(res["res2Status"], 401)

        # 500 call: 0 session calls (reused cached session), 1 endpoint fetch, status 500
        self.assertEqual(res["error500SessionCalls"], 0)
        self.assertEqual(res["error500FetchCount"], 1)
        self.assertEqual(res["res3Status"], 500)


if __name__ == "__main__":
    unittest.main()
