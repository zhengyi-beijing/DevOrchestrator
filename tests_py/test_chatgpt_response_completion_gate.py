import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "browser" / "chatgpt-web-adapter.user.js"


class ChatGptResponseCompletionGateTests(unittest.TestCase):
    def test_response_text_must_be_stable_before_transport_ack(self):
        script = SCRIPT.as_posix()
        code = r'''
globalThis.document = {querySelectorAll: () => []};
globalThis.window = {location:{href:'https://chatgpt.com/c/conv-A'}, document:globalThis.document};
globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
require('__SCRIPT__');
const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
let s = a.advanceResponseStability(null, '[DEVORCH_WEB_SOL_RESPONSE r]\n{"x":', 1000);
console.log(s.ready, s.since);
s = a.advanceResponseStability(s, '[DEVORCH_WEB_SOL_RESPONSE r]\n{"x":1', 1800);
console.log(s.ready, s.since);
s = a.advanceResponseStability(s, '[DEVORCH_WEB_SOL_RESPONSE r]\n{"x":1}', 2500);
console.log(s.ready, s.since);
s = a.advanceResponseStability(s, '[DEVORCH_WEB_SOL_RESPONSE r]\n{"x":1}', 4000);
console.log(s.ready, s.since);
'''.replace('__SCRIPT__', script)
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[:3], ["false 1000", "false 1800", "false 2500"])
        self.assertEqual(lines[3], "true 2500")

    def test_wait_loop_uses_stability_gate_before_respond(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("advanceResponseStability", source)
        self.assertIn("responseStability.ready", source)
        self.assertLess(source.index("responseStability.ready"), source.index("respond(bindingId, claim, responseText)"))


if __name__ == "__main__":
    unittest.main()