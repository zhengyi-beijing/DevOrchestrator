"""Tests for P19.3 ChatGPT Plus Browser Control Bridge."""

from __future__ import annotations

import json
import io
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from unittest.mock import patch

from dev_orchestrator.bridge.server import (
    _is_allowed_control_origin,
    _is_loopback_address,
    make_bridge_server,
)
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.browser_control import (
    DEVORCH_ACTION_RESULT_V1,
    DEVORCH_ACTION_V1,
    BrowserControlConflictError,
    BrowserControlConfirmationRequiredError,
    BrowserControlError,
    BrowserControlRequestStore,
    BrowserControlService,
    format_action_result,
    parse_action_envelope,
    to_web_bridge_request,
    validate_action_envelope,
)
from dev_orchestrator.control.security import (
    CapabilityVerdict,
    ControlSecurity,
    create_browser_control_capability,
    create_mcp_capability,
    list_browser_control_capabilities,
    revoke_browser_control_capability,
    validate_browser_control_capability,
)
from dev_orchestrator.cli import main as cli_main
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.storage.json_store import read_json, write_json
from dev_orchestrator.web.server import make_server
from tests_py.test_control_commands import write_config

ROOT = Path(__file__).resolve().parents[1]
USERSCRIPT_PATH = ROOT / "browser" / "chatgpt-web-adapter.user.js"


class ProtocolParserTests(unittest.TestCase):
    """Test DEVORCH_ACTION_V1 envelope parsing, validation, and result formatting."""

    def test_parse_bracket_envelope(self):
        text = """
Here is the requested operation:
[DEVORCH_ACTION_V1]
{
  "protocol": "DEVORCH_ACTION_V1",
  "request_id": "req-101",
  "action": "status",
  "project_id": "devorchestrator",
  "parameters": {}
}
[/DEVORCH_ACTION_V1]
Please confirm when done.
"""
        parsed = parse_action_envelope(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["protocol"], DEVORCH_ACTION_V1)
        self.assertEqual(parsed["request_id"], "req-101")
        self.assertEqual(parsed["action"], "status")
        self.assertEqual(parsed["project_id"], "devorchestrator")
        self.assertFalse(parsed["confirmed"])

    def test_parse_code_fence_envelope(self):
        text = """
```devorch_action
{
  "protocol": "DEVORCH_ACTION_V1",
  "request_id": "req-102",
  "action": "start_task",
  "project_id": "devorchestrator",
  "parameters": {"command_ref": "echo_hello"},
  "confirmed": true
}
```
"""
        parsed = parse_action_envelope(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["request_id"], "req-102")
        self.assertEqual(parsed["action"], "start_task")
        self.assertTrue(parsed["confirmed"])
        self.assertEqual(parsed["parameters"], {"command_ref": "echo_hello"})

    def test_parse_raw_json_envelope(self):
        raw = json.dumps({
            "protocol": "DEVORCH_ACTION_V1",
            "request_id": "req-103",
            "action": "job_status",
            "project_id": "test-proj",
            "parameters": {"job_id": "job-999"},
        })
        parsed = parse_action_envelope(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["request_id"], "req-103")
        self.assertEqual(parsed["action"], "job_status")

    def test_parse_invalid_or_missing_envelope(self):
        self.assertIsNone(parse_action_envelope(""))
        self.assertIsNone(parse_action_envelope("just some normal conversation text"))
        self.assertIsNone(parse_action_envelope("[DEVORCH_ACTION_V1] {bad json} [/DEVORCH_ACTION_V1]"))
        # Missing required request_id
        invalid = {"protocol": "DEVORCH_ACTION_V1", "action": "status"}
        with self.assertRaises(BrowserControlError):
            validate_action_envelope(invalid)

    def test_format_action_result(self):
        res = format_action_result("req-101", "success", {"state": "ok"})
        self.assertIn(f"[{DEVORCH_ACTION_RESULT_V1} req-101]", res)
        self.assertIn('"status": "success"', res)
        self.assertIn('"state": "ok"', res)
        self.assertIn(f"[/{DEVORCH_ACTION_RESULT_V1}]", res)

        err_res = format_action_result("req-102", "error", {"error": "Denied", "reason": "Hardware command"})
        self.assertIn('"status": "error"', err_res)
        self.assertIn('"error": "Denied"', err_res)
        self.assertIn('"reason": "Hardware command"', err_res)

    def test_read_only_browser_envelope_translates_to_bound_web_bridge_request(self):
        issued_at = datetime.now(timezone.utc).isoformat()
        translated = to_web_bridge_request({
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-bound-status",
            "action": "get_project_status",
            "project_id": "devorchestrator",
            "parameters": {},
            "binding": {"adapter": "chatgpt_web", "binding_id": "conv-1"},
            "issued_at": issued_at,
        })
        self.assertEqual(translated["schema_version"], 1)
        self.assertEqual(translated["operation"], "project_status")
        self.assertEqual(translated["adapter_request_id"], "req-bound-status")
        self.assertEqual(translated["binding"]["binding_id"], "conv-1")
        self.assertEqual(translated["issued_at"], issued_at)
        self.assertEqual(translated["payload"], {})

    def test_read_only_browser_envelope_rejects_unknown_fields_and_parameters(self):
        base = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-rejected",
            "action": "get_project_status",
            "project_id": "devorchestrator",
            "parameters": {},
            "binding": {"adapter": "chatgpt_web", "binding_id": "conv-1"},
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        for changes in (
            {"shell": "whoami"},
            {"parameters": {"path": "C:/Windows/win.ini"}},
            {"action": "start_task"},
            {"binding": {"adapter": "other", "binding_id": "conv-1"}},
        ):
            request = dict(base)
            request.update(changes)
            with self.assertRaises(BrowserControlError):
                to_web_bridge_request(request)


class UserscriptAdapterTests(unittest.TestCase):
    """Test pure functions exposed by chatgpt-web-adapter.user.js using Node.js."""

    def test_userscript_pure_browser_control_helpers(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        
        // 1. Action classifications
        const ro1 = a.isReadOnlyAction('get_project_status');
        const ro2 = a.isReadOnlyAction('status');
        const ro3 = a.isReadOnlyAction('start_task');
        const ef1 = a.isEffectfulAction('start_task');
        const ef2 = a.isEffectfulAction('cancel_job');
        const ef3 = a.isEffectfulAction('status');

        // 2. Envelope parsing
        const envText = '[DEVORCH_ACTION_V1]\\n{{"protocol":"DEVORCH_ACTION_V1","request_id":"node-1","action":"get_project_status","project_id":"devorchestrator","parameters":{{}}}}\\n[/DEVORCH_ACTION_V1]';
        const parsed = a.parseActionEnvelope(envText);
        const withShell = a.parseActionEnvelope('[DEVORCH_ACTION_V1]\\n{{"protocol":"DEVORCH_ACTION_V1","request_id":"node-shell","action":"get_project_status","project_id":"devorchestrator","parameters":{{}},"shell":"whoami"}}\\n[/DEVORCH_ACTION_V1]');
        const withPath = a.parseActionEnvelope('[DEVORCH_ACTION_V1]\\n{{"protocol":"DEVORCH_ACTION_V1","request_id":"node-path","action":"get_project_status","project_id":"devorchestrator","parameters":{{"path":"C:/Windows/win.ini"}}}}\\n[/DEVORCH_ACTION_V1]');
        
        // 3. Result formatting
        const formatted = a.formatActionResult('node-1', 'success', {{ alive: true }});
        
        console.log(JSON.stringify({{
            ro: [ro1, ro2, ro3],
            ef: [ef1, ef2, ef3],
            parsed_id: parsed ? parsed.request_id : null,
            parsed_action: parsed ? parsed.action : null,
            unsafe_rejected: withShell === null && withPath === null,
            has_head: formatted.indexOf('[DEVORCH_ACTION_RESULT_V1 node-1]') !== -1
        }}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual(data["ro"], [True, False, False])
        self.assertEqual(data["ef"], [False, False, False])
        self.assertEqual(data["parsed_id"], "node-1")
        self.assertEqual(data["parsed_action"], "get_project_status")
        self.assertTrue(data["unsafe_rejected"])
        self.assertTrue(data["has_head"])

    def test_roleless_body_fallback_selects_last_complete_bounded_read_only_action(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        let composerText = '';
        const composerNode = {{
          focus:() => undefined,
          get innerText() {{ return composerText; }},
          get textContent() {{ return composerText; }}
        }};
        globalThis.document = {{
          body: {{innerText:''}},
          querySelector: () => null,
          querySelectorAll: (selector) =>
            selector === "[contenteditable='true']" && composerText ? [composerNode] : []
        }};
        const block = (id, action='get_project_status') => '[DEVORCH_ACTION_V1]\\n' + JSON.stringify({{
          protocol:'DEVORCH_ACTION_V1', request_id:id, action,
          project_id:'devorchestrator', parameters:{{}}
        }}) + '\\n[/DEVORCH_ACTION_V1]';
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        document.body.innerText = block('req-old') + '\\nvisible page text\\n' + block('req-last');
        const last = a.findRolelessReadOnlyActionCandidate();
        document.body.innerText = '[DEVORCH_ACTION_V1] {{bad json}} [/DEVORCH_ACTION_V1]';
        const malformed = a.findRolelessReadOnlyActionCandidate();
        document.body.innerText = block('req-truncated').replace('[/DEVORCH_ACTION_V1]', ' '.repeat(32768) + '[/DEVORCH_ACTION_V1]');
        const truncated = a.findRolelessReadOnlyActionCandidate();
        document.body.innerText = block('req-effectful', 'start_task');
        const effectful = a.findRolelessReadOnlyActionCandidate();
        document.body.innerText = block('req-unknown', 'unknown_action');
        const unknown = a.findRolelessReadOnlyActionCandidate();
        document.body.innerText = block('req-composer');
        composerText = document.body.innerText;
        const composer = a.findRolelessReadOnlyActionCandidate();
        composerText = '';
        window.location.href = 'https://chatgpt.com/';
        const unbound = a.findRolelessReadOnlyActionCandidate();
        console.log(JSON.stringify({{
          last:last && last.request_id, malformed:malformed === null,
          truncated:truncated === null, effectful:effectful === null,
          unknown:unknown === null, composer:composer === null, unbound:unbound === null
        }}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual(data["last"], "req-last")
        self.assertTrue(data["malformed"])
        self.assertTrue(data["truncated"])
        self.assertTrue(data["effectful"])
        self.assertTrue(data["unknown"])
        self.assertTrue(data["composer"])
        self.assertTrue(data["unbound"])

    def test_role_aware_action_path_remains_preferred_over_roleless_body(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        const block = (id) => '[DEVORCH_ACTION_V1]\\n' + JSON.stringify({{
          protocol:'DEVORCH_ACTION_V1', request_id:id, action:'get_project_status',
          project_id:'devorchestrator', parameters:{{}}
        }}) + '\\n[/DEVORCH_ACTION_V1]';
        const assistants = [{{innerText:block('req-role-aware')}}];
        globalThis.document = {{
          body: {{innerText:block('req-roleless'), appendChild:() => undefined}},
          querySelector: () => null,
          querySelectorAll: (selector) => {{
            if (selector === "[data-message-author-role='assistant']") return assistants;
            if (selector === '[data-message-author-role]') return assistants;
            return [];
          }}
        }};
        globalThis.sessionStorage = {{getItem:() => null, setItem:() => undefined}};
        globalThis.GM_getValue = (key, fallback) => key === 'devorch:control:browser-control-capability' ? 'control-token' : fallback;
        const requests = [];
        globalThis.GM_xmlhttpRequest = (opts) => {{
          requests.push(JSON.parse(opts.data));
          opts.onload({{status:200,responseText:JSON.stringify({{data:{{ok:true}}}})}});
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        a.scanAndProcessBrowserActions();
        setTimeout(() => {{
          console.log(JSON.stringify({{requests, telemetry:a.collectActionScanTelemetry()}}));
          process.exit(0);
        }}, 0);
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual([item["request_id"] for item in data["requests"]], ["req-role-aware"])
        self.assertEqual(data["telemetry"]["roleless_fallback_candidate_count"], 0)
        self.assertEqual(data["telemetry"]["roleless_fallback_dispatch_count"], 0)
        self.assertIsNone(data["telemetry"]["last_roleless_fallback_request_id"])

    def test_roleless_fallback_dispatches_last_once_and_ignores_processed_or_unsafe_actions(self):
        script = USERSCRIPT_PATH.as_posix()
        case_script = r"""
        const block = (id, action='get_project_status') => '[DEVORCH_ACTION_V1]\n' + JSON.stringify({
          protocol:'DEVORCH_ACTION_V1', request_id:id, action,
          project_id:'devorchestrator', parameters:{}
        }) + '\n[/DEVORCH_ACTION_V1]';
        const requests = [];
        let modalCount = 0;
        globalThis.document = {
          body:{innerText:BODY_TEXT_FACTORY(), appendChild:() => { modalCount += 1; }},
          querySelector:() => null,
          querySelectorAll:() => [],
          getElementById:() => null,
          createElement:() => ({style:{}, appendChild:() => undefined})
        };
        globalThis.sessionStorage = {
          getItem:(key) => key.indexOf('devorch:control:processed-actions') === 0 ? PROCESSED : null,
          setItem:() => undefined
        };
        globalThis.GM_getValue = (key, fallback) => key === 'devorch:control:browser-control-capability' ? 'control-token' : fallback;
        globalThis.GM_xmlhttpRequest = (opts) => {
          requests.push(JSON.parse(opts.data));
          opts.onload({status:200,responseText:JSON.stringify({data:{ok:true}})});
        };
        require(SCRIPT_PATH);
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        a.scanAndProcessBrowserActions();
        setTimeout(() => {
          console.log(JSON.stringify({requests, modalCount, telemetry:a.collectActionScanTelemetry()}));
          process.exit(0);
        }, 0);
        """

        def run_case(body_expression: str, processed: list[str]) -> dict[str, Any]:
            code = (
                "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
                "globalThis.window={location:{href:'https://chatgpt.com/c/conv-live'}};"
                f"const SCRIPT_PATH={json.dumps(script)};"
                f"const BODY_TEXT_FACTORY=()=>{body_expression};"
                f"const PROCESSED={json.dumps(json.dumps(processed))};"
                + case_script
            )
            result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout.strip())

        dispatched = run_case("block('req-old') + '\\n' + block('req-last')", [])
        self.assertEqual([item["request_id"] for item in dispatched["requests"]], ["req-last"])
        self.assertEqual(dispatched["telemetry"]["roleless_fallback_candidate_count"], 1)
        self.assertEqual(dispatched["telemetry"]["roleless_fallback_dispatch_count"], 1)
        self.assertEqual(dispatched["telemetry"]["last_roleless_fallback_request_id"], "req-last")

        processed = run_case("block('req-old') + '\\n' + block('req-processed')", ["req-processed"])
        self.assertEqual(processed["requests"], [])
        self.assertEqual(processed["telemetry"]["roleless_fallback_candidate_count"], 1)
        self.assertEqual(processed["telemetry"]["roleless_fallback_dispatch_count"], 0)

        for unsafe_action in ("start_task", "unknown_action"):
            unsafe = run_case(f"block('req-unsafe', {json.dumps(unsafe_action)})", [])
            self.assertEqual(unsafe["requests"], [])
            self.assertEqual(unsafe["modalCount"], 0)
            self.assertEqual(unsafe["telemetry"]["roleless_fallback_dispatch_count"], 0)

    def test_userscript_injects_live_binding_and_rejects_supplied_mismatch(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        globalThis.GM_getValue = () => 'wb-token';
        const sent = [];
        globalThis.GM_xmlhttpRequest = (opts) => {{ sent.push(JSON.parse(opts.data)); opts.onload({{status:200,responseText:'{{}}'}}); }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        (async () => {{
          const base = {{protocol:'DEVORCH_ACTION_V1',request_id:'req-bind',action:'get_project_status',project_id:'devorchestrator',parameters:{{}}}};
          await a.dispatchBrowserControlAction(base, false);
          const mismatch = Object.assign({{}}, base, {{request_id:'req-wrong',binding:{{adapter:'chatgpt_web',binding_id:'other'}}}});
          const denied = await a.dispatchBrowserControlAction(mismatch, false);
          console.log(JSON.stringify({{sent, denied}}));
        }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual(len(data["sent"]), 1)
        self.assertEqual(data["sent"][0]["binding"], {"adapter": "chatgpt_web", "binding_id": "conv-live"})
        self.assertTrue(data["sent"][0]["issued_at"])
        self.assertEqual(data["denied"]["status"], 403)

    def test_userscript_heartbeats_bounded_metadata_only_action_scan_telemetry(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        const longId = 'req-' + 'x'.repeat(180);
        const primary = [
          {{innerText:'ordinary assistant response'}},
          {{innerText:'DEVORCH_ACTION_V1 malformed Bearer assistant-secret-token'}},
          {{innerText:'[DEVORCH_ACTION_V1]\\n' + JSON.stringify({{
            protocol:'DEVORCH_ACTION_V1', request_id:longId,
            action:'get_project_status', project_id:'devorchestrator', parameters:{{}}
          }}) + '\\n[/DEVORCH_ACTION_V1]'}}
        ];
        const shadowElement = (selectors, innerText, shadowRoot) => ({{
          innerText: innerText || '', shadowRoot: shadowRoot || null,
          matches: (selector) => selectors.indexOf(selector) !== -1
        }});
        const nestedShadow = {{
          innerText: 'nested DEVORCH_ACTION_V1 shadow secret',
          querySelectorAll: () => [
            shadowElement(["[data-message-author-role='assistant']", "[data-testid^='conversation-turn-']"], 'DEVORCH_ACTION_V1 nested secret'),
            shadowElement(['article', "[class*='conversation-turn']"], '')
          ]
        }};
        const topShadowHost = {{innerText: 'composed DEVORCH_ACTION_V1 text'}};
        const topShadow = {{
          host: topShadowHost,
          querySelectorAll: () => [
            shadowElement(["[data-message-author-role='assistant']"], 'ordinary shadow assistant'),
            shadowElement(["[data-testid^='conversation-turn-']"], ''),
            shadowElement(['article'], ''),
            shadowElement(["[class*='agent-turn']"], ''),
            shadowElement(["[class*='conversation-turn']"], ''),
            shadowElement([], 'DEVORCH_ACTION_V1 top shadow secret', nestedShadow)
          ]
        }};
        topShadowHost.shadowRoot = topShadow;
        const allElements = [{{}}, topShadowHost, {{shadowRoot:null}}, {{}}, {{}}];
        globalThis.document = {{
          title: 'Bound conversation',
          body: {{
            innerText:'page text with literal DEVORCH_ACTION_V1 marker',
            appendChild: () => undefined
          }},
          getElementById: () => null,
          createElement: () => ({{style:{{}}, setAttribute:() => undefined}}),
          querySelectorAll: (selector) => {{
            if (selector === "*") return allElements;
            if (selector === "[data-message-author-role='assistant']") return primary;
            if (selector === "[data-testid^='conversation-turn-'] [data-message-author-role='assistant']") return [primary[2]];
            if (selector === "[data-testid^='conversation-turn-']") return [{{}}, {{}}, {{}}, {{}}];
            if (selector === "main") return [{{}}];
            if (selector === "article") return [{{}}, {{}}];
            if (selector === "iframe") return [{{}}];
            if (selector === "[data-message-author-role]") return [{{}}, {{}}, {{}}];
            if (selector === "[data-testid]") return [{{}}, {{}}, {{}}, {{}}];
            if (selector === "[data-turn]") return [{{}}, {{}}];
            if (selector === "[data-turn-id]") return [{{}}];
            if (selector === "[class*='agent-turn']") return [{{}}, {{}}, {{}}];
            if (selector === "[class*='conversation-turn']") return [{{}}, {{}}];
            if (selector === "[role='article']") return [{{}}];
            if (selector === "#prompt-textarea") return [{{}}];
            if (selector === "[contenteditable='true']") return [{{}}, {{}}];
            if (selector === "textarea") return [{{}}];
            if (selector === "button[data-testid='send-button']") return [];
            if (selector === "button#composer-submit-button") return [{{}}];
            return [];
          }}
        }};
        globalThis.sessionStorage = {{getItem: () => 'tab-diagnostic', setItem: () => undefined}};
        globalThis.GM_getValue = (key, fallback) => key === 'devorch:control:heartbeat-capability'
          ? JSON.stringify({{token:'heartbeat-secret',pairing_id:'pair-1'}}) : fallback;
        globalThis.GM_setValue = () => undefined;
        const requests = [];
        globalThis.GM_xmlhttpRequest = (opts) => {{
          requests.push(JSON.parse(opts.data));
          opts.onload({{status:200,responseText:JSON.stringify({{data:{{session:{{active_tab_count:1}}}}}})}});
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const snapshot = a.collectActionScanTelemetry();
        a.sendControlHeartbeat().then((ok) => {{
          console.log(JSON.stringify({{ok, snapshot, heartbeat:requests[0]}}));
        }}).catch((error) => {{ console.error(error); process.exitCode = 1; }});
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertTrue(data["ok"])
        telemetry = data["heartbeat"]["action_scan"]
        self.assertEqual(telemetry, data["snapshot"])
        self.assertEqual(telemetry["primary_assistant_count"], 3)
        self.assertEqual(telemetry["assistant_turn_count"], 1)
        self.assertEqual(telemetry["conversation_turn_count"], 4)
        self.assertEqual(telemetry["total_element_count"], 5)
        self.assertEqual(telemetry["body_text_length"], 47)
        self.assertTrue(telemetry["body_has_action_marker"])
        self.assertEqual(telemetry["main_count"], 1)
        self.assertEqual(telemetry["article_count"], 2)
        self.assertEqual(telemetry["iframe_count"], 1)
        self.assertEqual(telemetry["data_message_author_role_count"], 3)
        self.assertEqual(telemetry["data_testid_count"], 4)
        self.assertEqual(telemetry["data_turn_count"], 2)
        self.assertEqual(telemetry["data_turn_id_count"], 1)
        self.assertEqual(telemetry["agent_turn_class_count"], 3)
        self.assertEqual(telemetry["conversation_turn_class_count"], 2)
        self.assertEqual(telemetry["role_article_count"], 1)
        self.assertEqual(telemetry["shadow_host_count"], 1)
        self.assertEqual(telemetry["open_shadow_root_count"], 2)
        self.assertEqual(telemetry["shadow_total_element_count"], 8)
        self.assertEqual(telemetry["shadow_primary_assistant_count"], 2)
        self.assertEqual(telemetry["shadow_conversation_turn_count"], 2)
        self.assertEqual(telemetry["shadow_article_count"], 2)
        self.assertEqual(telemetry["shadow_agent_turn_class_count"], 1)
        self.assertEqual(telemetry["shadow_conversation_turn_class_count"], 2)
        self.assertEqual(telemetry["shadow_marker_root_count"], 2)
        self.assertEqual(telemetry["shadow_marker_element_count"], 2)
        self.assertEqual(telemetry["marker_node_count"], 2)
        self.assertEqual(telemetry["parsed_action_count"], 1)
        self.assertEqual(len(telemetry["most_recent_request_id"]), 128)
        self.assertEqual(telemetry["roleless_fallback_candidate_count"], 0)
        self.assertEqual(telemetry["roleless_fallback_dispatch_count"], 0)
        self.assertIsNone(telemetry["last_roleless_fallback_request_id"])
        self.assertEqual(telemetry["prompt_textarea_count"], 1)
        self.assertEqual(telemetry["contenteditable_count"], 2)
        self.assertEqual(telemetry["textarea_count"], 1)
        self.assertEqual(telemetry["legacy_send_button_count"], 0)
        self.assertEqual(telemetry["composer_submit_button_count"], 1)
        self.assertEqual(telemetry["composer_kind"], "contenteditable")
        self.assertEqual(telemetry["composer_surface"], "document")
        self.assertEqual(telemetry["submit_candidate_path"], "send_testid")
        self.assertEqual(telemetry["submit_candidate_surface"], "document")
        self.assertEqual(telemetry["websol_submit_attempt_count"], 0)
        self.assertEqual(telemetry["websol_user_turn_confirm_count"], 0)
        self.assertEqual(telemetry["last_websol_submit_path"], "none")
        self.assertIsNone(telemetry["last_websol_submit_request_id"])
        self.assertEqual(telemetry["last_websol_user_turn_evidence"], "none")
        self.assertIsNone(telemetry["last_websol_user_turn_request_id"])
        self.assertEqual(telemetry["result_submit_attempt_count"], 0)
        self.assertEqual(telemetry["result_composer_found_count"], 0)
        self.assertEqual(telemetry["result_composer_prepared_count"], 0)
        self.assertEqual(telemetry["result_send_click_count"], 0)
        self.assertEqual(telemetry["result_confirm_success_count"], 0)
        self.assertIsNone(telemetry["last_result_submit_request_id"])
        self.assertEqual(telemetry["last_result_submit_stage"], "idle")
        self.assertFalse(telemetry["action_scan_active"])
        self.assertTrue(telemetry["scanned_at"].endswith("Z"))
        serialized = json.dumps(data["heartbeat"])
        self.assertNotIn("ordinary assistant response", serialized)
        self.assertNotIn("assistant-secret-token", serialized)
        self.assertNotIn("heartbeat-secret", serialized)
        self.assertNotIn("shadow secret", serialized)

    def test_userscript_open_shadow_traversal_enforces_root_depth_and_element_bounds(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        const emptyDocument = (hosts) => ({{
          body: {{innerText:''}},
          querySelectorAll: (selector) => selector === '*' ? hosts : []
        }});
        const oversizedRoots = Array.from({{length:40}}, () => ({{
          querySelectorAll: () => ({{length:10001}})
        }}));
        globalThis.document = emptyDocument(oversizedRoots.map((root) => ({{shadowRoot:root}})));
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const rootAndElementBound = a.collectActionScanTelemetry();

        let nested = {{querySelectorAll: () => []}};
        for (let depth = 0; depth < 7; depth += 1) {{
          const child = nested;
          nested = {{querySelectorAll: () => [{{shadowRoot:child, innerText:'', matches:() => false}}]}};
        }}
        globalThis.document = emptyDocument([{{shadowRoot:nested}}]);
        const depthBound = a.collectActionScanTelemetry();
        console.log(JSON.stringify({{rootAndElementBound, depthBound}}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual(data["rootAndElementBound"]["open_shadow_root_count"], 32)
        self.assertEqual(data["rootAndElementBound"]["shadow_total_element_count"], 320_000)
        self.assertEqual(data["depthBound"]["open_shadow_root_count"], 6)
        self.assertEqual(data["depthBound"]["shadow_total_element_count"], 6)

    def test_confirmation_modal_uses_text_nodes_and_delivery_is_confirmed(self):
        source = USERSCRIPT_PATH.read_text(encoding="utf-8")
        modal = source[
            source.index("function showEffectfulConfirmationModal"):
            source.index("function submitActionResult")
        ]
        self.assertNotIn(".innerHTML", modal)
        self.assertIn("content.textContent", modal)
        submit = source[
            source.index("function submitActionResult"):
            source.index("var actionScanActive")
        ]
        self.assertIn("isActionResultInConversation(requestId)", submit)
        self.assertIn("resolve(false)", submit)

    def test_composer_discovery_prefers_legacy_and_requires_one_usable_fallback(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        const legacy = {{focus:() => undefined}};
        const fallback = {{focus:() => undefined}};
        const other = {{focus:() => undefined}};
        let legacyComposer = legacy;
        let editables = [fallback];
        globalThis.document = {{
          querySelector:(selector) => selector === '#prompt-textarea' ? legacyComposer : null,
          querySelectorAll:(selector) => selector === "[contenteditable='true']" ? editables : []
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const legacyPreferred = a.findComposer() === legacy;
        legacyComposer = null;
        const uniqueFallback = a.findComposer() === fallback;
        editables = [fallback, other];
        const multipleRejected = a.findComposer() === null;
        editables = [{{}}];
        const unusableRejected = a.findComposer() === null;
        console.log(JSON.stringify({{legacyPreferred, uniqueFallback, multipleRejected, unusableRejected}}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertTrue(data["legacyPreferred"])
        self.assertTrue(data["uniqueFallback"])
        self.assertTrue(data["multipleRejected"])
        self.assertTrue(data["unusableRejected"])

    def test_roleless_result_confirmation_excludes_marker_still_in_composer(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        const marker = '[DEVORCH_ACTION_RESULT_V1 result-roleless]';
        const composer = {{focus:() => undefined, innerText:marker, textContent:marker}};
        globalThis.document = {{
          body:{{innerText:marker + ' result body'}},
          querySelector:() => null,
          querySelectorAll:(selector) => selector === "[contenteditable='true']" ? [composer] : []
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const draftRejected = !a.isActionResultInConversation('result-roleless');
        composer.innerText = '';
        composer.textContent = '';
        const clearedConfirmed = a.isActionResultInConversation('result-roleless');
        console.log(JSON.stringify({{draftRejected, clearedConfirmed}}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertTrue(data["draftRejected"])
        self.assertTrue(data["clearedConfirmed"])

    def test_role_aware_result_confirmation_remains_preferred(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        const marker = '[DEVORCH_ACTION_RESULT_V1 result-role-aware]';
        const composer = {{focus:() => undefined, innerText:marker, textContent:marker}};
        let roleAware = [{{innerText:marker + ' submitted'}}];
        globalThis.document = {{
          body:{{innerText:marker + ' body'}},
          querySelector:() => null,
          querySelectorAll:(selector) => {{
            if (selector === '[data-message-author-role]') return roleAware;
            if (selector === "[contenteditable='true']") return [composer];
            return [];
          }}
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const roleAwareConfirmed = a.isActionResultInConversation('result-role-aware');
        roleAware = [{{innerText:'different turn'}}];
        composer.innerText = '';
        composer.textContent = '';
        const bodyFallbackBlocked = !a.isActionResultInConversation('result-role-aware');
        console.log(JSON.stringify({{roleAwareConfirmed, bodyFallbackBlocked}}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertTrue(data["roleAwareConfirmed"])
        self.assertTrue(data["bodyFallbackBlocked"])

    def test_submit_action_result_reports_only_bounded_stage_counts(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        globalThis.setTimeout = (fn) => {{ queueMicrotask(fn); return 1; }};
        const longId = 'result-' + 'r'.repeat(180);
        const resultText = '[DEVORCH_ACTION_RESULT_V1 ' + longId + '] private-result-text';
        let clicked = false;
        const composer = {{focus:() => undefined, textContent:'', dispatchEvent:() => undefined}};
        const send = {{
          disabled:false,
          getAttribute:() => '',
          click:() => {{ clicked = true; }}
        }};
        globalThis.document = {{
          body:{{innerText:''}},
          execCommand:() => false,
          querySelector:(selector) => {{
            if (selector === "button[data-testid='send-button']") return send;
            return null;
          }},
          querySelectorAll:(selector) => {{
            if (selector === "[data-message-author-role='assistant']") return [];
            if (selector === '[data-message-author-role]') return clicked ? [{{innerText:resultText}}] : [];
            if (selector === '*') return [composer, send];
            if (selector === '#prompt-textarea') return [composer];
            if (selector === "[contenteditable='true']") return [composer];
            if (selector === "button[data-testid='send-button']") return [send];
            return [];
          }}
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        a.collectActionScanTelemetry();
        a.submitActionResult(resultText, longId).then((ok) => {{
          console.log(JSON.stringify({{ok, telemetry:a.collectActionScanTelemetry()}}));
        }}).catch((error) => {{ console.error(error); process.exitCode = 1; }});
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertTrue(data["ok"])
        telemetry = data["telemetry"]
        self.assertEqual(telemetry["result_submit_attempt_count"], 1)
        self.assertEqual(telemetry["result_composer_found_count"], 1)
        self.assertEqual(telemetry["result_composer_prepared_count"], 1)
        self.assertEqual(telemetry["result_send_click_count"], 1)
        self.assertEqual(telemetry["result_confirm_success_count"], 1)
        self.assertEqual(len(telemetry["last_result_submit_request_id"]), 128)
        self.assertEqual(telemetry["last_result_submit_stage"], "confirmed")
        serialized = json.dumps(telemetry)
        self.assertNotIn("private-result-text", serialized)
        self.assertNotIn("DEVORCH_ACTION_RESULT_V1", serialized)

    def test_action_retry_reuses_issued_at_and_preserves_first_success(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        const store = {{}};
        globalThis.sessionStorage = {{
          getItem:(key) => Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null,
          setItem:(key, value) => {{ store[key] = String(value); }}
        }};
        globalThis.GM_getValue = (key, fallback) =>
          key === 'devorch:control:browser-control-capability' ? 'wb-token' : fallback;
        const sent = [];
        globalThis.GM_xmlhttpRequest = (opts) => {{
          sent.push(JSON.parse(opts.data));
          opts.onload({{status:200,responseText:JSON.stringify({{data:{{ok:true}}}})}});
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const env = {{
          protocol:'DEVORCH_ACTION_V1',request_id:'req-stable',
          action:'get_project_status',project_id:'devorchestrator',parameters:{{}}
        }};
        (async () => {{
          await a.dispatchBrowserControlAction(env, false);
          await a.dispatchBrowserControlAction(env, false);
          const success = {{status:200,text:JSON.stringify({{data:{{project_id:'devorchestrator',git:{{head:'head-ok'}}}}}})}};
          const conflict = {{status:409,text:JSON.stringify({{error:'Conflict'}})}};
          const firstSubmitted = await a.handleActionResult(env, success);
          const firstPending = a.pendingActionResult('req-stable', 'conv-live');
          const secondSubmitted = await a.handleActionResult(env, conflict);
          const secondPending = a.pendingActionResult('req-stable', 'conv-live');
          console.log(JSON.stringify({{
            sent, firstSubmitted, secondSubmitted, firstPending, secondPending
          }}));
        }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual(len(data["sent"]), 2)
        self.assertEqual(data["sent"][0], data["sent"][1])
        self.assertTrue(data["sent"][0]["issued_at"])
        self.assertFalse(data["firstSubmitted"])
        self.assertFalse(data["secondSubmitted"])
        self.assertEqual(data["firstPending"], data["secondPending"])
        self.assertIn('"status": "success"', data["firstPending"])
        self.assertIn("head-ok", data["firstPending"])
        self.assertNotIn("Conflict", data["secondPending"])

    def test_local_composer_send_fallback_rejects_stop_and_ambiguity(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        let buttons = [];
        const container = {{querySelectorAll:(selector) => selector === 'button' ? buttons : [], parentElement:null}};
        const composer = {{focus:() => undefined, parentElement:container}};
        const makeButton = (label, className) => {{
          const state = {{clicks:0}};
          return {{
            disabled:false, className:className || '',
            innerText:'',
            getAttribute:(name) => {{
              if (name === 'aria-label') return label;
              if (name === 'class') return className || '';
              return '';
            }},
            click:() => {{ state.clicks += 1; }},
            state
          }};
        }};
        globalThis.document = {{
          querySelector:() => null,
          querySelectorAll:(selector) =>
            selector === "[contenteditable='true']" ? [composer] : []
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        const stop = makeButton('Stop', 'cursor bg-composer-primary');
        buttons = [stop];
        const stopClicked = a.clickSendButton();
        const stopClicks = stop.state.clicks;
        const send = makeButton('Send', 'cursor bg-composer-primary');
        buttons = [send];
        const sendClicked = a.clickSendButton();
        const sendClicks = send.state.clicks;
        const plain1 = makeButton('', 'cursor bg-composer-primary');
        const plain2 = makeButton('', 'cursor bg-composer-primary');
        buttons = [plain1, plain2];
        const ambiguousClicked = a.clickSendButton();
        console.log(JSON.stringify({{
          stopClicked, stopClicks, sendClicked, sendClicks, ambiguousClicked,
          ambiguousClicks:plain1.state.clicks + plain2.state.clicks
        }}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertFalse(data["stopClicked"])
        self.assertEqual(data["stopClicks"], 0)
        self.assertTrue(data["sendClicked"])
        self.assertEqual(data["sendClicks"], 1)
        self.assertFalse(data["ambiguousClicked"])
        self.assertEqual(data["ambiguousClicks"], 0)

    def test_userscript_automatically_claims_pending_bootstrap_into_private_storage(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        globalThis.window = {{ location: {{ href: 'https://chatgpt.com/c/conv-live' }} }};
        globalThis.document = {{ title: 'Bound conversation' }};
        globalThis.sessionStorage = {{
          getItem: () => 'tab-live', setItem: () => undefined
        }};
        const writes = [];
        globalThis.GM_getValue = (key, fallback) => key === 'devorch:control:heartbeat-capability'
          ? JSON.stringify({{token:'heartbeat-secret',pairing_id:'pair-1'}}) : fallback;
        globalThis.GM_setValue = (key, value) => writes.push([key, value]);
        const requests = [];
        globalThis.GM_xmlhttpRequest = (opts) => {{
          requests.push({{url:opts.url, data:opts.data, headers:opts.headers}});
          if (opts.url.endsWith('/session-heartbeats')) {{
            opts.onload({{status:200,responseText:JSON.stringify({{data:{{
              project_id:'devorchestrator', session:{{active_tab_count:1}},
              web_bridge_bootstrap:{{available:true,expires_in_seconds:60}}
            }}}})}});
          }} else {{
            opts.onload({{status:200,responseText:JSON.stringify({{data:{{token:'control-secret'}}}})}});
          }}
        }};
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        a.sendControlHeartbeat().then((ok) => {{
          console.log(JSON.stringify({{ok, requests, writes}}));
        }}).catch((error) => {{ console.error(error); process.exitCode = 1; }});
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["requests"]), 2)
        claim = data["requests"][1]
        self.assertEqual(claim["url"], "http://127.0.0.1:8750/api/v1/control/web-bridge/bootstrap/redeem")
        self.assertNotIn("?", claim["url"])
        self.assertEqual(json.loads(claim["data"]), {
            "project_id": "devorchestrator", "adapter": "chatgpt_web", "binding_id": "conv-live",
        })
        self.assertNotIn("heartbeat-secret", claim["data"])
        self.assertEqual(claim["headers"]["Authorization"], "Bearer heartbeat-secret")
        self.assertIn(["devorch:control:browser-control-capability", "control-secret"], data["writes"])
        self.assertNotIn("control-secret", json.dumps(data["requests"]))

    def test_manual_read_only_pairing_fallback_remains_available(self):
        source = USERSCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn('GM_registerMenuCommand("Pair DevOrchestrator read-only control"', source)
        self.assertIn("saveBrowserControlToken(raw.trim())", source)


class BrowserControlSecurityTests(unittest.TestCase):
    """Test capability minting, validation, scope isolation, and revocation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        (self.runtime_root / "control").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mint_and_validate_capability(self):
        cap = create_browser_control_capability(
            project_id="devorchestrator",
            allowed_actions=["status", "commands_catalog"],
            label="test-token",
            ttl_seconds=3600,
            runtime_root=self.runtime_root,
        )
        self.assertTrue(cap["token"].startswith("bc_"))
        self.assertTrue(cap["capability_id"].startswith("bc-"))
        token = cap["token"]

        # Valid action and project
        ok, reason, row = validate_browser_control_capability(
            f"Bearer {token}",
            project_id="devorchestrator",
            action="status",
            runtime_root=self.runtime_root,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "valid")
        self.assertIsNotNone(row)

        # Disallowed action
        ok, reason, _ = validate_browser_control_capability(
            f"Bearer {token}",
            project_id="devorchestrator",
            action="start_task",
            runtime_root=self.runtime_root,
        )
        self.assertFalse(ok)
        self.assertIn("action_not_allowed", reason)

        # Disallowed project
        ok, reason, _ = validate_browser_control_capability(
            f"Bearer {token}",
            project_id="other_project",
            action="status",
            runtime_root=self.runtime_root,
        )
        self.assertFalse(ok)
        self.assertIn("project_mismatch", reason)

    def test_revocation(self):
        cap = create_browser_control_capability(
            project_id="devorchestrator",
            label="revoke-me",
            runtime_root=self.runtime_root,
        )
        token = cap["token"]
        cap_id = cap["capability_id"]

        ok, _, _ = validate_browser_control_capability(f"Bearer {token}", runtime_root=self.runtime_root)
        self.assertTrue(ok)

        # Revoke
        rev = revoke_browser_control_capability(cap_id, runtime_root=self.runtime_root)
        self.assertTrue(rev.get("revoked"))

        # Now rejected
        ok, reason, _ = validate_browser_control_capability(f"Bearer {token}", runtime_root=self.runtime_root)
        self.assertFalse(ok)
        self.assertEqual(reason, "token_revoked")

    def test_scope_isolation_mcp_and_heartbeat_tokens_cannot_act_as_browser_control(self):
        # Mint MCP token
        mcp_cap = create_mcp_capability(
            label="mcp-only",
            runtime_root=self.runtime_root,
        )
        mcp_token = mcp_cap["token"]

        # MCP token must NOT validate as browser control capability
        ok, reason, _ = validate_browser_control_capability(
            f"Bearer {mcp_token}",
            project_id="devorchestrator",
            action="status",
            runtime_root=self.runtime_root,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_or_revoked_token")


class BrowserControlServiceTests(unittest.TestCase):
    """Test execution service, confirmation gating, idempotency, and isolation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        (self.runtime_root / "control").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "projects").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "bridge").mkdir(parents=True, exist_ok=True)

        write_json(self.runtime_root / "daemon.json", {
            "state": "running",
            "pid": 12345,
            "listen_address": "127.0.0.1",
            "port": 8770,
            "last_tick_at": "2026-09-30T00:00:00+00:00",
            "last_error": None,
        })
        write_json(self.runtime_root / "projects" / "devorchestrator.json", {
            "project_id": "devorchestrator",
            "name": "DevOrchestrator",
            "repo_path": str(self.runtime_root),
            "state": "READY_TO_RUN",
            "lifecycle_state": "COMPLETE",
        })

        import sys
        py = sys.executable
        # Setup test execution-jobs.json
        jobs_cfg = {
            "version": 1,
            "enabled": True,
            "runtime_root": str(self.runtime_root),
            "projects": {
                "devorchestrator": {
                    "project_id": "devorchestrator",
                    "repo_path": str(self.runtime_root),
                    "file_roots": [str(self.runtime_root)],
                    "commands": {
                        "echo_test": {
                            "command_ref": "echo_test",
                            "effect_class": "read_only",
                            "description": "Echo test command",
                            "argv": [py, "-c", "print('hello')"],
                            "timeout_seconds": 30,
                            "parameters": {"count": {"type": "integer"}},
                        },
                        "hardware_flash": {
                            "command_ref": "hardware_flash",
                            "effect_class": "hardware",
                            "description": "Flash hardware device",
                            "argv": ["echo", "flashing"],
                            "timeout_seconds": 60,
                        },
                    },
                }
            },
        }
        write_json(self.runtime_root / "execution-jobs.json", jobs_cfg)

        self.bridge_store = BrowserBridgeStore(self.runtime_root / "bridge", require_live_binding=False)
        self.service = BrowserControlService(
            runtime_root=self.runtime_root,
            bridge_store=self.bridge_store,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commands_catalog_marks_hardware_non_selectable(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-cat-1",
            "action": "commands_catalog",
            "project_id": "devorchestrator",
        }
        res = self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(res["status"], "success")
        cmds = res["data"]["commands"]
        by_ref = {c["command_ref"]: c for c in cmds}
        self.assertIn("echo_test", by_ref)
        self.assertIn("hardware_flash", by_ref)

        self.assertTrue(by_ref["echo_test"]["selectable"])
        self.assertFalse(by_ref["echo_test"]["is_hardware"])

        self.assertFalse(by_ref["hardware_flash"]["selectable"])
        self.assertTrue(by_ref["hardware_flash"]["is_hardware"])

    def test_read_file_path_traversal_fails_closed(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-rf-1",
            "action": "read_file",
            "project_id": "devorchestrator",
            "parameters": {"path": "../../../../../etc/passwd"},
        }
        with self.assertRaises(BrowserControlError) as ctx:
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertIn(ctx.exception.status_code, (400, 403, 404))

    def test_effectful_action_requires_confirmation(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-spawn-1",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test"},
            "confirmed": False,
        }
        with self.assertRaises(BrowserControlConfirmationRequiredError) as ctx:
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.action, "start_task")

    def test_confirmed_effectful_action_and_idempotency(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-spawn-2",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test", "parameters": {"count": 3}},
            "confirmed": True,
        }
        res1 = self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(res1["status"], "success")
        job_id = res1["data"].get("job_id") or res1["data"].get("operation_id")
        self.assertIsNotNone(job_id)

        # Exact replay returns cached response
        res2 = self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(res2["status"], "success")
        job_id_2 = res2["data"].get("job_id") or res2["data"].get("operation_id")
        self.assertEqual(job_id, job_id_2)

        # Mismatched payload with same request_id raises Conflict 409
        conflicting_req = dict(req)
        conflicting_req["parameters"] = {"command_ref": "echo_test", "parameters": {"count": 5}}
        with self.assertRaises(BrowserControlConflictError):
            self.service.execute_action(conflicting_req, host_id="test", capability_id="test-cap")

        # Polling also reaps the short-lived subprocess before the temporary
        # runtime is removed, avoiding a misleading ResourceWarning.
        for poll_index in range(20):
            status = self.service.execute_action({
                "protocol": DEVORCH_ACTION_V1,
                "request_id": f"req-spawn-2-status-{poll_index}",
                "action": "job_status",
                "project_id": "devorchestrator",
                "parameters": {"job_id": job_id},
            }, host_id="test", capability_id="test-cap")
            if status["data"].get("status") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)

    def test_concurrent_replay_dispatches_only_once(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-concurrent-1",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test"},
            "confirmed": True,
        }
        calls = 0
        calls_lock = threading.Lock()

        def fake_dispatch(*_args):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.15)
            return {"job_id": "job-single", "selected_transport": "local"}

        self.service._dispatch = fake_dispatch
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.service.execute_action(req, host_id="test", capability_id="test-cap"),
                range(2),
            ))

        self.assertEqual(calls, 1)
        self.assertEqual([row["data"]["job_id"] for row in results], ["job-single", "job-single"])

    def test_failed_or_corrupt_claim_fails_closed_on_replay(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-failed-1",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test"},
            "confirmed": True,
        }
        calls = 0

        def failing_dispatch(*_args):
            nonlocal calls
            calls += 1
            raise RuntimeError("ambiguous transport failure")

        self.service._dispatch = failing_dispatch
        with self.assertRaises(BrowserControlError):
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        with self.assertRaises(BrowserControlConflictError):
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(calls, 1)

        corrupt_id = "req-corrupt-1"
        self.service.store._file_path(corrupt_id).write_text("{not-json", encoding="utf-8")
        corrupt = dict(req, request_id=corrupt_id)
        with self.assertRaises(BrowserControlConflictError):
            self.service.execute_action(corrupt, host_id="test", capability_id="test-cap")

    def test_web_sol_channel_isolation(self):
        # Executing browser control action must NOT enqueue anything in BrowserBridgeStore
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-iso-1",
            "action": "status",
            "project_id": "devorchestrator",
        }
        self.service.execute_action(req, host_id="test", capability_id="test-cap")
        # Bridge queue must be completely empty
        claim = self.bridge_store.claim("chatgpt_web", "test-binding")
        self.assertIsNone(claim)


class WebBridgeBootstrapSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        self.security = ControlSecurity(self.runtime_root)
        pairing = self.security.create_pairing()
        redeemed = self.security.redeem_pairing(pairing["pairing_id"], pairing["code"])
        self.pairing_id = pairing["pairing_id"]
        self.heartbeat_token = redeemed["capability"]

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _grant(self, *, ttl=60):
        return self.security.create_web_bridge_bootstrap(
            "p1", "conv-1", "chatgpt_web", expires_in_seconds=ttl,
        )

    def test_bootstrap_is_exact_one_time_and_persists_only_hashes(self):
        metadata = self._grant()
        self.assertEqual(metadata, {
            "status": "pending", "project_id": "p1", "binding_id": "conv-1",
            "adapter": "chatgpt_web", "expires_in_seconds": 60,
        })
        self.assertTrue(self.security.web_bridge_bootstrap_status(
            "p1", "conv-1", "chatgpt_web",
        )["available"])

        for changes in (
            {"project_id": "p2"},
            {"binding_id": "conv-2"},
            {"adapter": "other"},
        ):
            identity = {"project_id": "p1", "binding_id": "conv-1", "adapter": "chatgpt_web"}
            identity.update(changes)
            with self.assertRaisesRegex(ValueError, "missing, expired, already used, or mismatched"):
                self.security.redeem_web_bridge_bootstrap(self.heartbeat_token, **identity)

        result = self.security.redeem_web_bridge_bootstrap(
            self.heartbeat_token, "p1", "conv-1", "chatgpt_web",
        )
        self.assertEqual(result["scope"], "web_bridge_control")
        ok, reason, _ = self.security.validate_web_bridge_capability(
            result["token"], "p1", "conv-1", "chatgpt_web",
        )
        self.assertTrue(ok, reason)
        for identity in (
            ("p2", "conv-1", "chatgpt_web"),
            ("p1", "conv-2", "chatgpt_web"),
            ("p1", "conv-1", "other"),
        ):
            self.assertFalse(self.security.validate_web_bridge_capability(result["token"], *identity)[0])

        with self.assertRaisesRegex(ValueError, "missing, expired, already used, or mismatched"):
            self.security.redeem_web_bridge_bootstrap(
                self.heartbeat_token, "p1", "conv-1", "chatgpt_web",
            )

        persisted = self.security.pairings_path.read_text(encoding="utf-8")
        self.assertNotIn(self.heartbeat_token, persisted)
        self.assertNotIn(result["token"], persisted)
        store = json.loads(persisted)
        grants = list(store["web_bridge_bootstrap_grants"].values())
        self.assertEqual(len(grants), 1)
        self.assertTrue(grants[0]["used"])
        self.assertNotIn("token", grants[0])
        self.assertNotIn("secret", grants[0])

    def test_bootstrap_ttl_expiry_restart_and_concurrent_replay_are_fail_closed(self):
        now = time.time()
        with patch("dev_orchestrator.control.security.time.time", return_value=now):
            metadata = self._grant(ttl=999)
        self.assertEqual(metadata["expires_in_seconds"], 60)
        restarted = ControlSecurity(self.runtime_root)
        self.assertTrue(restarted.web_bridge_bootstrap_status(
            "p1", "conv-1", "chatgpt_web",
        )["available"])
        with patch("dev_orchestrator.control.security.time.time", return_value=now + 61):
            self.assertFalse(restarted.web_bridge_bootstrap_status(
                "p1", "conv-1", "chatgpt_web",
            )["available"])
            with self.assertRaisesRegex(ValueError, "missing, expired, already used, or mismatched"):
                restarted.redeem_web_bridge_bootstrap(
                    self.heartbeat_token, "p1", "conv-1", "chatgpt_web",
                )

        restarted.create_web_bridge_bootstrap("p1", "conv-1", "chatgpt_web")
        def claim(_: int) -> bool:
            try:
                restarted.redeem_web_bridge_bootstrap(
                    self.heartbeat_token, "p1", "conv-1", "chatgpt_web",
                )
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim, range(2))), [False, True])

    def test_owner_cli_creates_exact_pending_grant_without_printing_secret(self):
        sessions = ConversationControlStore(self.runtime_root)
        sessions.heartbeat(
            "chatgpt_web", "conv-1", title="Conversation",
            url="https://chatgpt.com/c/conv-1", tab_instance_id="tab-1",
            capability_id=self.pairing_id,
        )
        sessions.bind("p1", "chatgpt_web", "conv-1")
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main([
                "create-web-bridge-bootstrap", "p1", "conv-1",
                "--adapter", "chatgpt_web", "--ttl", "60",
                "--runtime-root", str(self.runtime_root),
            ])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "pending")
        self.assertFalse({"token", "code", "secret", "grant_id"} & set(payload))

        # The owner command refuses a binding that is not the project's
        # authoritative current binding.
        self.assertEqual(cli_main([
            "create-web-bridge-bootstrap", "p1", "other",
            "--runtime-root", str(self.runtime_root),
        ]), 1)

    def test_capability_state_and_authorization_agree_after_expiry(self):
        cap = self.security.create_web_bridge_capability(
            "p1", "conv-1", adapter="chatgpt_web", expires_in_seconds=60,
        )
        self.assertEqual(self.security.capability_state(cap["capability_id"]), CapabilityVerdict.VALID)
        self.assertTrue(self.security.validate_web_bridge_capability(
            cap["token"], "p1", "conv-1", "chatgpt_web",
        )[0])

        future = time.time() + 61
        with patch("dev_orchestrator.control.security.time.time", return_value=future):
            self.assertEqual(self.security.capability_state(cap["capability_id"]), CapabilityVerdict.UNKNOWN)
            ok, reason, _ = self.security.validate_web_bridge_capability(
                cap["token"], "p1", "conv-1", "chatgpt_web",
            )
            self.assertFalse(ok)
            self.assertEqual(reason, "capability token expired")

        pending = self.security.create_pairing()
        pairing_expiry = read_json(self.security.pairings_path, {})["pairings"][pending["pairing_id"]]["expires_at_epoch"]
        with patch("dev_orchestrator.control.security.time.time", return_value=float(pairing_expiry) + 1):
            self.assertEqual(self.security.capability_state(pending["pairing_id"]), CapabilityVerdict.UNKNOWN)


class BridgeHTTPServerIntegrationTests(unittest.TestCase):
    """Verify the real 8765 adapter -> authoritative 8770 WebBridge path."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        (self.runtime_root / "control").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "bridge").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "projects").mkdir(parents=True, exist_ok=True)
        self.config = self.runtime_root / "projects.json"
        write_config(self.config, ROOT)
        project = {
            "project_id": "p1", "id": "p1", "name": "P1", "repo_path": str(ROOT),
            "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
            "git": {"branch": "main", "head": "head1", "dirty": False, "changed_entries": 0},
            "telemetry": {"task_id": "P19.3"},
            "readiness": {"state": "ready_to_run", "valid": True},
            "active_execution": None, "active_roles": [],
            "control_identity": {
                "project_id": "p1", "task_id": "P19.3", "lifecycle_state": "READY_TO_RUN",
                "paused": False, "binding_id": "conv-1", "binding_adapter": "chatgpt_web",
            },
        }
        write_json(self.runtime_root / "projects" / "p1.json", project)
        write_json(self.runtime_root / "summary.json", {
            "observed_at": datetime.now(timezone.utc).isoformat(), "projects": [project],
        })

        self.control_server = make_server(
            "127.0.0.1", 0, self.runtime_root, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.control_thread = threading.Thread(target=self.control_server.serve_forever, daemon=True)
        self.control_thread.start()
        self.control_port = self.control_server.server_address[1]

        self.security = ControlSecurity(self.runtime_root)
        heartbeat_pairing = self.security.create_pairing()
        heartbeat = self.security.redeem_pairing(
            heartbeat_pairing["pairing_id"], heartbeat_pairing["code"],
        )
        self.heartbeat_token = heartbeat["capability"]
        self.heartbeat_pairing_id = heartbeat["pairing_id"]

        self.conv_store = ConversationControlStore(self.runtime_root)
        self.conv_store.heartbeat(
            "chatgpt_web", "conv-1", title="P1 Conversation",
            url="https://chatgpt.com/c/conv-1", tab_instance_id="tab-1",
            capability_id=self.heartbeat_pairing_id,
        )
        self.conv_store.bind("p1", "chatgpt_web", "conv-1")

        self.bridge_store = BrowserBridgeStore(self.runtime_root / "bridge", require_live_binding=False)
        self.server = make_bridge_server(
            "127.0.0.1", 0, self.bridge_store,
            runtime_root=self.runtime_root, config_path=self.config,
            control_base_url=f"http://127.0.0.1:{self.control_port}",
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = self.server.server_address[1]
        self.cap = self.security.create_web_bridge_capability(
            "p1", "conv-1", adapter="chatgpt_web",
        )
        self.token = self.cap["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.control_server.shutdown()
        self.control_server.server_close()
        self.server_thread.join(timeout=2)
        self.control_thread.join(timeout=2)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        data = None
        if body is not None:
            if isinstance(body, dict):
                data = json.dumps(body).encode("utf-8")
                h.setdefault("Content-Type", "application/json")
            elif isinstance(body, (bytes, str)):
                data = body.encode("utf-8") if isinstance(body, str) else body
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        resp_body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, resp_headers, resp_body

    def _control_request(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        conn = HTTPConnection("127.0.0.1", self.control_port, timeout=5)
        request_headers = dict(headers)
        request_headers.setdefault("Content-Type", "application/json")
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=request_headers)
        response = conn.getresponse()
        response_body = response.read().decode("utf-8")
        conn.close()
        return response.status, response_body

    def _action(self, request_id="req-http-1", **overrides):
        value = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": request_id,
            "action": "get_project_status",
            "project_id": "p1",
            "parameters": {},
            "binding": {"adapter": "chatgpt_web", "binding_id": "conv-1"},
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        value.update(overrides)
        return value

    @property
    def auth_headers(self):
        return {"Authorization": f"Bearer {self.token}", "Origin": "https://chatgpt.com"}

    @property
    def heartbeat_headers(self):
        return {"Authorization": f"Bearer {self.heartbeat_token}", "Origin": "https://chatgpt.com"}

    def test_options_preflight_cors(self):
        status, headers, _ = self._request("OPTIONS", "/v1/control/action", headers={"Origin": "https://chatgpt.com"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("access-control-allow-origin"), "https://chatgpt.com")
        self.assertIn("POST", headers.get("access-control-allow-methods", ""))
        self.assertIn("Authorization", headers.get("access-control-allow-headers", ""))
        self.assertEqual(self._request("OPTIONS", "/v1/control/action", headers={"Origin": "https://attacker.site"})[0], 403)
        status, headers, _ = self._request("OPTIONS", "/v1/control/action", headers={"Origin": "https://chatgpt.com.attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_control_origin_and_peer_helpers_are_exact(self):
        self.assertTrue(_is_allowed_control_origin("https://chatgpt.com"))
        self.assertTrue(_is_allowed_control_origin("https://chatgpt.com:443"))
        self.assertTrue(_is_allowed_control_origin("http://127.0.0.1:8765"))
        self.assertTrue(_is_allowed_control_origin("http://[::1]:8765"))
        self.assertFalse(_is_allowed_control_origin("https://chatgpt.com.attacker.example"))
        self.assertFalse(_is_allowed_control_origin("http://localhost.attacker.example"))
        self.assertFalse(_is_allowed_control_origin("https://chatgpt.com/path"))
        self.assertTrue(_is_loopback_address("127.0.0.1"))
        self.assertTrue(_is_loopback_address("::1"))
        self.assertTrue(_is_loopback_address("::ffff:127.0.0.1"))
        self.assertFalse(_is_loopback_address("192.0.2.10"))

    def test_health_check(self):
        status, _, body = self._request("GET", "/v1/control/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("service"), "browser_control_bridge")

    def test_post_action_unauthorized(self):
        status, _, body = self._request("POST", "/v1/control/action", body=self._action())
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", body.lower())

    def test_post_action_read_only(self):
        status, headers, body = self._request(
            "POST", "/v1/control/action", body=self._action("req-http-2"), headers=self.auth_headers,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(headers.get("access-control-allow-origin"), "https://chatgpt.com")
        status_data = json.loads(body)["data"]
        self.assertEqual(status_data["project_id"], "p1")
        self.assertEqual(status_data["git"]["head"], "head1")
        self.assertEqual(status_data["current_task"], "P19.3")
        self.assertEqual(status_data["lifecycle_state"], "READY_TO_RUN")
        self.assertFalse(status_data["paused"])
        self.assertEqual(status_data["conversation"]["state"], "live")

    def test_heartbeat_persists_only_valid_action_scan_metadata_on_exact_tab(self):
        heartbeat_payload = {
            "adapter": "chatgpt_web",
            "binding_id": "conv-1",
            "title": "P1 Conversation",
            "url": "https://chatgpt.com/c/conv-1",
            "tab_instance_id": "tab-1",
            "action_scan": {
                "scanned_at": "2026-10-02T08:30:00.000Z",
                "primary_assistant_count": 7,
                "assistant_turn_count": 6,
                "conversation_turn_count": 12,
                "total_element_count": 200,
                "body_text_length": 4096,
                "body_has_action_marker": True,
                "main_count": 1,
                "article_count": 12,
                "iframe_count": 0,
                "data_message_author_role_count": 12,
                "data_testid_count": 30,
                "data_turn_count": 12,
                "data_turn_id_count": 12,
                "agent_turn_class_count": 6,
                "conversation_turn_class_count": 12,
                "role_article_count": 12,
                "shadow_host_count": 0,
                "open_shadow_root_count": 2,
                "shadow_total_element_count": 12000,
                "shadow_primary_assistant_count": 7,
                "shadow_conversation_turn_count": 12,
                "shadow_article_count": 12,
                "shadow_agent_turn_class_count": 6,
                "shadow_conversation_turn_class_count": 12,
                "shadow_marker_root_count": 1,
                "shadow_marker_element_count": 2,
                "marker_node_count": 2,
                "parsed_action_count": 1,
                "most_recent_request_id": "diag-request-1",
                "roleless_fallback_candidate_count": 3,
                "roleless_fallback_dispatch_count": 1,
                "last_roleless_fallback_request_id": "fallback-request-1",
                "prompt_textarea_count": 1,
                "contenteditable_count": 2,
                "textarea_count": 1,
                "legacy_send_button_count": 0,
                "composer_submit_button_count": 1,
                "composer_kind": "contenteditable",
                "composer_surface": "open_shadow_root",
                "submit_candidate_path": "send_testid",
                "submit_candidate_surface": "open_shadow_root",
                "websol_submit_attempt_count": 2,
                "websol_user_turn_confirm_count": 1,
                "last_websol_submit_path": "send_testid",
                "last_websol_submit_request_id": "probe:websol:conv-1:1:abc",
                "last_websol_user_turn_evidence": "roleless_surface",
                "last_websol_user_turn_request_id": "probe:websol:conv-1:1:abc",
                "result_submit_attempt_count": 4,
                "result_composer_found_count": 3,
                "result_composer_prepared_count": 2,
                "result_send_click_count": 1,
                "result_confirm_success_count": 1,
                "last_result_submit_request_id": "result-request-1",
                "last_result_submit_stage": "confirmed",
                "action_scan_active": False,
            },
        }
        status, body = self._control_request(
            "/api/v1/control/session-heartbeats", heartbeat_payload, self.heartbeat_headers,
        )
        self.assertEqual(status, 200, body)
        sessions = read_json(self.runtime_root / "conversation-sessions.json", {})
        tab = sessions["chatgpt_web\nconv-1"]["tabs"]["tab-1"]
        self.assertEqual(tab["action_scan"], heartbeat_payload["action_scan"])

        invalid = dict(heartbeat_payload)
        invalid["action_scan"] = {
            **heartbeat_payload["action_scan"],
            "most_recent_request_id": "x" * 129,
            "assistant_message": "must never persist",
        }
        status, body = self._control_request(
            "/api/v1/control/session-heartbeats", invalid, self.heartbeat_headers,
        )
        self.assertEqual(status, 200, body)
        sessions = read_json(self.runtime_root / "conversation-sessions.json", {})
        tab = sessions["chatgpt_web\nconv-1"]["tabs"]["tab-1"]
        self.assertEqual(tab["action_scan"], heartbeat_payload["action_scan"])
        self.assertNotIn("assistant_message", json.dumps(sessions))
        self.assertNotIn("must never persist", json.dumps(sessions))

        for field, invalid_value in (
            ("total_element_count", 10_001),
            ("body_text_length", 1_000_001),
            ("body_text_length", True),
            ("body_has_action_marker", "true"),
            ("shadow_host_count", -1),
            ("open_shadow_root_count", 33),
            ("shadow_total_element_count", 320_001),
            ("shadow_primary_assistant_count", 12_001),
            ("shadow_marker_root_count", 3),
            ("shadow_marker_element_count", 12_001),
            ("roleless_fallback_candidate_count", -1),
            ("roleless_fallback_dispatch_count", 10_001),
            ("prompt_textarea_count", 10_001),
            ("contenteditable_count", True),
            ("composer_kind", "message_text"),
            ("composer_surface", "closed_shadow_root"),
            ("submit_candidate_path", "javascript_eval"),
            ("submit_candidate_surface", "cross_origin_iframe"),
            ("websol_submit_attempt_count", 10_001),
            ("websol_user_turn_confirm_count", True),
            ("last_websol_submit_path", "direct_dom_mutation"),
            ("last_websol_submit_request_id", "x" * 129),
            ("last_websol_user_turn_evidence", "claim_only"),
            ("last_websol_user_turn_request_id", "x" * 129),
            ("result_submit_attempt_count", 10_001),
            ("result_composer_found_count", 5),
            ("result_confirm_success_count", 2),
            ("last_result_submit_request_id", "x" * 129),
            ("last_result_submit_stage", "message_text"),
        ):
            invalid = dict(heartbeat_payload)
            invalid["action_scan"] = {
                **heartbeat_payload["action_scan"],
                field: invalid_value,
            }
            status, body = self._control_request(
                "/api/v1/control/session-heartbeats", invalid, self.heartbeat_headers,
            )
            self.assertEqual(status, 200, body)
            sessions = read_json(self.runtime_root / "conversation-sessions.json", {})
            tab = sessions["chatgpt_web\nconv-1"]["tabs"]["tab-1"]
            self.assertEqual(tab["action_scan"], heartbeat_payload["action_scan"])

    def test_automatic_bootstrap_redeems_once_without_secret_leakage(self):
        security = ControlSecurity(self.runtime_root)
        canonical = read_json(security.pairings_path, {})
        canonical["capabilities"].pop(self.cap["capability_id"])
        write_json(security.pairings_path, canonical, indent=2)

        pairing = security.create_pairing()
        heartbeat = security.redeem_pairing(pairing["pairing_id"], pairing["code"])
        heartbeat_token = heartbeat["capability"]
        heartbeat_payload = {
            "adapter": "chatgpt_web",
            "binding_id": "conv-1",
            "title": "P1 Conversation",
            "url": "https://chatgpt.com/c/conv-1",
            "tab_instance_id": "tab-1",
        }
        heartbeat_status, heartbeat_body = self._control_request(
            "/api/v1/control/session-heartbeats",
            heartbeat_payload,
            {"Authorization": f"Bearer {heartbeat_token}", "Origin": "https://chatgpt.com"},
        )
        self.assertEqual(heartbeat_status, 200, heartbeat_body)
        heartbeat_data = json.loads(heartbeat_body)["data"]
        self.assertEqual(heartbeat_data["project_id"], "p1")
        self.assertEqual(heartbeat_data["web_bridge_bootstrap"]["available"], False)

        pending = security.create_web_bridge_bootstrap("p1", "conv-1", "chatgpt_web")
        self.assertEqual(pending["status"], "pending")
        heartbeat_status, heartbeat_body = self._control_request(
            "/api/v1/control/session-heartbeats", heartbeat_payload,
            {"Authorization": f"Bearer {heartbeat_token}", "Origin": "https://chatgpt.com"},
        )
        self.assertEqual(heartbeat_status, 200, heartbeat_body)
        heartbeat_data = json.loads(heartbeat_body)["data"]
        self.assertEqual(heartbeat_data["web_bridge_bootstrap"]["available"], True)
        self.assertNotIn("token", heartbeat_data["web_bridge_bootstrap"])
        self.assertNotIn("secret", heartbeat_data["web_bridge_bootstrap"])
        self.assertNotIn(heartbeat_token, heartbeat_body)

        bootstrap_headers = {
            "Authorization": f"Bearer {heartbeat_token}",
            "Origin": "https://chatgpt.com",
        }
        bootstrap_path = "/api/v1/control/web-bridge/bootstrap/redeem"
        other_pairing = security.create_pairing()
        other_heartbeat = security.redeem_pairing(
            other_pairing["pairing_id"], other_pairing["code"],
        )
        self.assertEqual(self._control_request(
            bootstrap_path,
            body={"project_id": "p1", "adapter": "chatgpt_web", "binding_id": "conv-1"},
            headers={
                "Authorization": f"Bearer {other_heartbeat['capability']}",
                "Origin": "https://chatgpt.com",
            },
        )[0], 403)
        wrong_status, _ = self._control_request(
            bootstrap_path,
            body={"project_id": "p2", "adapter": "chatgpt_web", "binding_id": "conv-1"},
            headers=bootstrap_headers,
        )
        self.assertEqual(wrong_status, 403)
        self.assertEqual(self._control_request(
            bootstrap_path,
            body={"project_id": "p1", "adapter": "chatgpt_web", "binding_id": "other"},
            headers=bootstrap_headers,
        )[0], 403)
        self.assertEqual(self._control_request(
            bootstrap_path,
            body={"project_id": "p1", "adapter": "other", "binding_id": "conv-1"},
            headers=bootstrap_headers,
        )[0], 403)
        self.assertEqual(self._control_request(
            bootstrap_path,
            body={"project_id": "p1", "adapter": "chatgpt_web", "binding_id": "conv-1"},
            headers={"Authorization": f"Bearer {heartbeat_token}", "Origin": "https://attacker.example"},
        )[0], 403)
        malformed_status, _ = self._control_request(
            bootstrap_path,
            body={"project_id": "p1", "adapter": "chatgpt_web", "binding_id": "conv-1", "url": "https://example.test"},
            headers=bootstrap_headers,
        )
        self.assertEqual(malformed_status, 400)

        request = {"project_id": "p1", "adapter": "chatgpt_web", "binding_id": "conv-1"}
        status, body = self._control_request(bootstrap_path, body=request, headers=bootstrap_headers)
        self.assertEqual(status, 200, body)
        control_token = json.loads(body)["data"]["token"]
        self.assertTrue(control_token)
        self.assertNotEqual(control_token, heartbeat_token)

        replay_status, replay_body = self._control_request(
            bootstrap_path, body=request, headers=bootstrap_headers,
        )
        self.assertEqual(replay_status, 409, replay_body)
        action_status, _, action_body = self._request(
            "POST", "/v1/control/action", body=self._action("req-bootstrap-action"),
            headers={"Authorization": f"Bearer {control_token}", "Origin": "https://chatgpt.com"},
        )
        self.assertEqual(action_status, 200, action_body)

        persisted = security.pairings_path.read_text(encoding="utf-8")
        self.assertNotIn(heartbeat_token, persisted)
        self.assertNotIn(control_token, persisted)

    def test_malformed_unsupported_shell_and_mutation_actions_rejected(self):
        for req in (
            {"protocol": DEVORCH_ACTION_V1},
            self._action("req-unsupported", action="get_resources"),
            self._action("req-shell", shell="powershell.exe -Command whoami"),
            self._action("req-file", action="read_file", parameters={"path": "C:/Windows/win.ini"}),
            self._action("req-mutate", action="start_task", parameters={"command_ref": "anything"}),
        ):
            self.assertEqual(self._request("POST", "/v1/control/action", body=req, headers=self.auth_headers)[0], 400)

    def test_wrong_conversation_cross_project_and_stale_session_rejected(self):
        wrong_binding = self._action("req-wrong-binding", binding={"adapter": "chatgpt_web", "binding_id": "other"})
        self.assertEqual(self._request("POST", "/v1/control/action", body=wrong_binding, headers=self.auth_headers)[0], 403)
        self.assertEqual(self._request("POST", "/v1/control/action", body=self._action("req-wrong-project", project_id="p2"), headers=self.auth_headers)[0], 403)

        sessions = self.conv_store._sessions()
        key = self.conv_store._key("chatgpt_web", "conv-1")
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        sessions[key]["last_seen_at"] = stale
        sessions[key]["tabs"]["tab-1"]["last_seen_at"] = stale
        write_json(self.conv_store.sessions_path, sessions, indent=2)
        status, _, body = self._request("POST", "/v1/control/action", body=self._action("req-stale"), headers=self.auth_headers)
        self.assertEqual(status, 403)
        self.assertIn("stale", body.lower())

    def test_idempotent_replay_and_reload_store_recovery(self):
        req = self._action("req-reload")
        first = self._request("POST", "/v1/control/action", body=req, headers=self.auth_headers)
        second = self._request("POST", "/v1/control/action", body=req, headers=self.auth_headers)
        self.assertEqual(first[0], 200, first[2])
        self.assertEqual(second[0], 200, second[2])
        self.assertEqual(json.loads(first[2]), json.loads(second[2]))
        self.assertTrue((self.runtime_root / "control" / "web_bridge_requests" / "req-reload.json").is_file())

        refreshed = json.loads(json.dumps(req))
        refreshed["issued_at"] = datetime.now(timezone.utc).isoformat()
        replay = self._request("POST", "/v1/control/action", body=refreshed, headers=self.auth_headers)
        self.assertEqual(replay[0], 200, replay[2])
        self.assertEqual(json.loads(first[2]), json.loads(replay[2]))

    def test_8765_bridge_contains_no_lifecycle_or_filesystem_dispatch(self):
        source = (ROOT / "src" / "dev_orchestrator" / "bridge" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("BrowserControlService", source)
        self.assertNotIn("spawn_job", source)
        self.assertNotIn("read_file(", source)
        self.assertIn("/api/v1/control/web-bridge/requests", source)
        self.assertNotIn("/api/v1/control/web-bridge/bootstrap/redeem", source)
        userscript = USERSCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn('GM_registerMenuCommand("Pair DevOrchestrator read-only control"', userscript)


if __name__ == "__main__":
    unittest.main()
