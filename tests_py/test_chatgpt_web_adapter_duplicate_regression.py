import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "browser" / "chatgpt-web-adapter.user.js"


class ChatGptWebAdapterDuplicateRegressionTests(unittest.TestCase):
    def test_reclaimed_request_requires_dom_evidence_not_storage_mark(self):
        script = SCRIPT.as_posix()
        code = r'''
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => store.has(k) ? store.get(k) : null,
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k)
};
globalThis.__messages = [];
globalThis.document = {querySelectorAll: () => globalThis.__messages};
globalThis.window = {location:{href:'https://chatgpt.com/c/conv-A'}, document:globalThis.document};
globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
require('__SCRIPT__');
const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
console.log(a.requestAlreadySubmitted('conv-A','req-1'));
a.markRequestSubmitted('conv-A','req-1');
console.log(a.requestAlreadySubmitted('conv-A','req-1'));
console.log(a.requestAlreadySubmitted('conv-A','req-2'));
store.clear();
globalThis.__messages = [{innerText:'[DEVORCH_WEB_SOL_REQUEST req-1] already sent'}];
console.log(a.requestAlreadySubmitted('conv-A','req-1'));
'''.replace('__SCRIPT__', script)
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["false", "false", "false", "true"])

    def test_claim_path_checks_duplicate_before_inserting_prompt(self):
        source = SCRIPT.read_text(encoding="utf-8")
        duplicate_check = "requestAlreadySubmitted(bindingId, claim.request_id)"
        insert_call = "insertAndSubmit(claim.prompt)"
        confirm_call = "confirmSubmission(bindingId, claim"
        direct_mark = "markRequestSubmitted(bindingId, claim.request_id)"
        self.assertIn(duplicate_check, source)
        self.assertIn(confirm_call, source)
        self.assertLess(source.index(duplicate_check), source.index(insert_call))
        self.assertLess(source.index(insert_call), source.index(confirm_call))
        self.assertNotIn(direct_mark, source)


if __name__ == "__main__":
    unittest.main()
