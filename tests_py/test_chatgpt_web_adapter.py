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

    def test_userscript_contains_no_workflow_decision_logic(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("NEXT_STAGE", "NEXT_TASK", "CONTINUE_CURRENT_STAGE", "OWNER_GATE", "validate_websol_response"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
