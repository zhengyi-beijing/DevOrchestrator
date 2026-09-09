import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "browser" / "chatgpt-web-adapter.user.js"


class ConversationControlUserscriptTests(unittest.TestCase):
    def source(self):
        return SCRIPT.read_text(encoding="utf-8")

    def test_userscript_declares_control_plane_routes_and_heartbeat(self):
        source = self.source()
        self.assertIn('CONTROL_BASE = "http://127.0.0.1:8766"', source)
        for route in ("/v1/session/heartbeat", "/v1/projects", "/v1/bind", "/v1/rebind", "/v1/unbind"):
            self.assertIn(route, source)
        self.assertIn("tab_instance_id", source)
        self.assertIn("sessionStorage", source)
        self.assertIn("controlHeartbeat", source)
    def test_badge_is_interactive_and_panel_is_present(self):
        source = self.source()
        self.assertIn('PANEL_ELEMENT_ID = "devorch-control-panel"', source)
        self.assertIn("pointer-events:auto", source)
        self.assertIn('badge.addEventListener("click"', source)
        self.assertIn("renderControlPanel", source)

    def test_owner_actions_are_wired_only_to_explicit_ui_clicks(self):
        source = self.source()
        self.assertIn('/v1/owner-action', source)
        self.assertIn('approve_next_stage', source)
        self.assertIn('start_current_task', source)
        self.assertIn('Stop / Pause auto-starts', source)
        self.assertIn('approveButton.addEventListener("click"', source)
        self.assertIn('startButton.addEventListener("click"', source)
        self.assertIn('stopButton.addEventListener("click"', source)

    def test_pure_badge_and_binding_action_helpers(self):
        script = SCRIPT.as_posix()
        code = (
            "globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__=true;"
            "require(\"" + script + "\");"
            "const a=globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;"
            "const labels=[a.controlBadgeLabel({projectId:'labdemo',bindingState:'bound'}),"
            "a.controlBadgeLabel({projectId:null,bindingState:'unbound'}),"
            "a.controlBadgeLabel({projectId:'labdemo',bindingState:'stale'})];"
            "const actions=[a.chooseBindingAction({orchestration_ready:false,conversation_binding:null},'conv-A'),"
            "a.chooseBindingAction({orchestration_ready:true,conversation_binding:{binding_id:'conv-B'}},'conv-A')];"
            "const labelsOk=labels[0].indexOf('labdemo')!==-1&&labels[0].endsWith('BOUND')&&labels[1].endsWith('UNBOUND')&&labels[2].endsWith('STALE');console.log(String(labelsOk)+'|'+actions.join('|'));"
        )
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "true|bind|rebind",
        )

    def test_binding_mutations_are_only_wired_to_explicit_buttons(self):
        source = self.source()
        self.assertIn('bindButton.addEventListener("click"', source)
        self.assertIn('unbindButton.addEventListener("click"', source)
        self.assertEqual(source.count("controlMutation("), 3)


if __name__ == "__main__":
    unittest.main()
