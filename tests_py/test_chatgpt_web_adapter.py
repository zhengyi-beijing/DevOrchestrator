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


if __name__ == "__main__":
    unittest.main()
