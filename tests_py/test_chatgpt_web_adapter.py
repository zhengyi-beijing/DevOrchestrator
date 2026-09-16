import json
import subprocess
import unittest
from pathlib import Path

from dev_orchestrator.bridge.prompt import render_websol_prompt
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "browser" / "chatgpt-web-adapter.user.js"


class ChatGptWebAdapterTests(unittest.TestCase):
    def test_rendered_prompt_has_identity_and_matching_response_contract(self):
        request = WebSolRequest(
            project_id="labdemo", request_id="req-42", task_id="P1", stage_id="S1",
            branch="main", head="c" * 40, role=WebSolRole.REVIEWER,
            event=WebSolEvent.REVIEW_REQUIRED, nonce="nonce-42",
        )
        text = render_websol_prompt(request, "Review current stage evidence.")
        self.assertIn("[DEVORCH_WEB_SOL_REQUEST req-42]", text)
        self.assertIn('"project_id": "labdemo"', text)
        self.assertIn('"nonce": "nonce-42"', text)
        self.assertIn("[DEVORCH_WEB_SOL_RESPONSE req-42]", text)
    def test_userscript_derives_binding_from_url_and_only_matches_response_marker(self):
        script = SCRIPT.as_posix()
        code = (
            "require('" + script + "');"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "console.log([a.conversationIdFromUrl('https://chatgpt.com/c/abc-123'),"
            "a.conversationIdFromUrl('https://chatgpt.com/g/g-x/c/xyz-789?foo=1'),"
            "a.conversationIdFromUrl('https://chatgpt.com/'),"
            "a.responseMatches('[DEVORCH_WEB_SOL_RESPONSE req-1] ok','req-1'),"
            "a.responseMatches('[DEVORCH_WEB_SOL_RESPONSE req-2] ok','req-1')].join('|'));"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "abc-123|xyz-789||true|false")

    def test_userscript_remote_sync_reload_is_throttled(self):
        script = SCRIPT.as_posix()
        code = (
            "require(\"" + script + "\");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "console.log([a.shouldRemoteSyncReload(0,1000),"
            "a.shouldRemoteSyncReload(1000,50000),"
            "a.shouldRemoteSyncReload(1000,61000)].join(\"|\"));"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true|false|true")
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("REMOTE_SYNC_RELOAD_AFTER_MS", source)
        self.assertIn("window.location.reload()", source)

    def test_userscript_stale_storage_mark_does_not_prove_submission(self):
        script = SCRIPT.as_posix()
        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "globalThis.localStorage={getItem:()=>'stale',setItem:()=>{}};"
            "globalThis.document={querySelectorAll:()=>[]};"
            "require(\"" + script + "\");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "const stale=a.requestAlreadySubmitted('binding','req-1');"
            "globalThis.document.querySelectorAll=()=>[{innerText:'[DEVORCH_WEB_SOL_REQUEST req-1] ok'}];"
            "const dom=a.requestAlreadySubmitted('binding','req-1');"
            "console.log(String(stale)+'|'+String(dom));"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "false|true")
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("SUBMIT_BUTTON_WAIT_MS", source)
        self.assertIn("SUBMISSION_CONFIRM_MS", source)
        self.assertIn("range.selectNodeContents(composer)", source)
        self.assertIn("submitWhenReady(bindingId, claim", source)
        self.assertIn("confirmSubmission(bindingId, claim", source)

    def test_userscript_supports_new_composer_submit_id_without_clicking_stop(self):
        script = SCRIPT.as_posix()
        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "const send={disabled:false,innerText:'',getAttribute:(n)=>n==='aria-label'?'Send prompt':null};"
            "const stop={disabled:false,innerText:'',getAttribute:(n)=>n==='aria-label'?'Stop answering':(n==='data-testid'?'stop-button':null)};"
            "globalThis.document={querySelector:(q)=>q===\"button#composer-submit-button\"?send:null,querySelectorAll:()=>[]};"
            "require(\"" + script + "\");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "const sendFound=a.findSendButton()===send;"
            "globalThis.document.querySelector=(q)=>q===\"button#composer-submit-button\"?stop:null;"
            "const stopRejected=a.findSendButton()===null;"
            "console.log(String(sendFound)+'|'+String(stopRejected));"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true|true")

    def test_userscript_has_compact_transport_status_badge(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('STATUS_ELEMENT_ID = "devorch-web-status"', source)
        for state in ("LIVE", "IDLE", "CLAIMED", "WAITING", "OFFLINE"):
            self.assertIn('setAdapterStatus("' + state, source)
        self.assertIn('badge.style.cssText = "position:fixed;right:12px;bottom:12px;', source)

    def test_userscript_contains_no_workflow_decision_logic(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("NEXT_STAGE", "NEXT_TASK", "CONTINUE_CURRENT_STAGE", "OWNER_GATE", "validate_websol_response"):
            self.assertNotIn(forbidden, source)

    def test_userscript_has_progress_channel_support(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('var PROGRESS_PATH = "/v1/progress";', source)
        self.assertIn('devorch-progress-toast', source)
        script = SCRIPT.as_posix()
        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "const toasts=[];"
            "globalThis.document={body:{appendChild:(el)=>toasts.push(el)},createElement:(tag)=>({className:'',style:{},textContent:'',parentNode:{removeChild:()=>{}}})};"
            "require(\"" + script + "\");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "const toast=a.showProgressToast({milestone:'WORKER_STARTED',message:'Started task P1'});"
            "console.log(toast.textContent+'|'+toasts.length);process.exit(0);"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[WORKER_STARTED] Started task P1|1")

    def test_userscript_raises_transport_alert_from_server_attention_metadata(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('// @version      0.1.12', source)
        self.assertIn('// @grant        GM_notification', source)
        self.assertIn('notification.attention !== "urgent"', source)
        self.assertIn('GM_notification({', source)
        self.assertIn('Test DevOrchestrator attention alert', source)
        script = SCRIPT.as_posix()
        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "const notices=[];"
            "globalThis.GM_notification=(x)=>notices.push(x);"
            "globalThis.GM_getValue=()=>'';globalThis.GM_setValue=()=>{};"
            "require(" + json.dumps(script) + ");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "const ok=a.showUrgentProgressAlert({notification_id:'n1',project_id:'p1',task_id:'T1',message:'Owner action required',attention:'urgent'});"
            "console.log(String(ok)+'|'+notices.length+'|'+notices[0].title);process.exit(0);"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true|1|DevOrchestrator requires attention")

    def test_userscript_pairs_and_sends_capability_scoped_8770_heartbeat(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('CONTROL_BASE = "http://127.0.0.1:8770"', source)
        self.assertIn('/api/v1/control/adapter-pairings/redeem', source)
        self.assertIn('/api/v1/control/session-heartbeats', source)
        self.assertIn('GM_registerMenuCommand("Pair DevOrchestrator 8770 heartbeat"', source)
        self.assertIn('headers.Authorization = "Bearer " + capability', source)
        self.assertNotIn('/v1/owner-action', source)
        script = SCRIPT.as_posix()
        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "require(" + json.dumps(script) + ");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "const ok=a.pairingFields('pair-id:single-use-code');"
            "console.log(ok.pairing_id+'|'+ok.code+'|'+String(a.pairingFields('invalid')===null));"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "pair-id|single-use-code|true")

        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "let saved='',requests=[];globalThis.GM_setValue=(k,v)=>{saved=v};"
            "globalThis.GM_getValue=()=>saved;globalThis.GM_deleteValue=()=>{saved=''};"
            "globalThis.GM_xmlhttpRequest=(o)=>{requests.push({url:o.url,headers:o.headers,data:JSON.parse(o.data)});"
            "const body=o.url.includes('redeem')?{data:{capability:'scoped-cap'}}:{data:{state:'live'}};"
            "o.onload({status:200,responseText:JSON.stringify(body)})};"
            "globalThis.window={location:{href:'https://chatgpt.com/c/conv'}};"
            "globalThis.document={title:'Conversation'};globalThis.sessionStorage={getItem:()=>null,setItem:()=>{}};"
            "require(" + json.dumps(script) + ");const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "(async()=>{const paired=await a.redeemControlPairing('pair:code');const live=await a.sendControlHeartbeat();"
            "console.log(JSON.stringify({paired,live,saved,requests}));process.exit(0)})()"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["paired"])
        self.assertTrue(payload["live"])
        self.assertEqual(payload["saved"], "scoped-cap")
        self.assertEqual(payload["requests"][0]["headers"]["Origin"], "https://chatgpt.com")
        self.assertNotIn("Authorization", payload["requests"][0]["headers"])
        self.assertEqual(payload["requests"][1]["headers"]["Authorization"], "Bearer scoped-cap")
        self.assertEqual(payload["requests"][1]["data"]["binding_id"], "conv")


if __name__ == "__main__":
    unittest.main()
