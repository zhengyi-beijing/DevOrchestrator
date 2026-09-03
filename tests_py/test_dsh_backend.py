import asyncio
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from dev_orchestrator.agents.backends.dsh import DshBackend
from dev_orchestrator.agents.base import UnknownRunError
from dev_orchestrator.agents.models import AgentRequest, AgentRole, AgentRunState, QuotaState


FAKE_DSH = r'''
import sys, time
if "--version" in sys.argv:
    print("fake-dsh 1.0")
    raise SystemExit(0)
prompt = sys.argv[-1]
if prompt == "SLEEP":
    time.sleep(30)
elif prompt == "FAIL7":
    print("fake failure", file=sys.stderr)
    raise SystemExit(7)
else:
    print("OUT:" + prompt)
    print("ERR:" + prompt, file=sys.stderr)
'''


class DshBackendTests(unittest.IsolatedAsyncioTestCase):
    def make_request(self, cwd, prompt="HELLO"):
        return AgentRequest(
            project_id="fixture", role=AgentRole.WORKER, prompt=prompt,
            working_directory=cwd, required_capabilities=frozenset({"code"}),
        )
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_dsh.py"
        self.fake.write_text(textwrap.dedent(FAKE_DSH), encoding="utf-8")
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "sentinel.txt").write_text("unchanged", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.backend = DshBackend(
            runtime_root=self.runtime,
            command_prefix=(sys.executable, str(self.fake)),
        )
    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_probe_is_non_prompt_and_reports_unknown_quota(self):
        status = self.backend.probe()
        self.assertTrue(status.available, status.reason)
        self.assertEqual(status.quota, QuotaState.UNKNOWN)
        self.assertIn("fake-dsh 1.0", status.model_label)

    async def test_start_status_collect_captures_output_without_project_writes(self):
        before = (self.work / "sentinel.txt").read_bytes()
        run = await self.backend.start(self.make_request(self.work, "HELLO"))
        self.assertEqual(run.backend_id, "dsh")
        result = await self.backend.collect(run.run_id)
        self.assertEqual(result.state, AgentRunState.COMPLETED)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("OUT:HELLO", result.stdout)
        self.assertIn("ERR:HELLO", result.stderr)
        self.assertEqual((self.work / "sentinel.txt").read_bytes(), before)
        self.assertTrue((self.runtime / "agent-runs" / run.run_id / "stdout.log").is_file())

    async def test_nonzero_exit_is_failed_and_unknown_run_is_typed_error(self):
        run = await self.backend.start(self.make_request(self.work, "FAIL7"))
        result = await self.backend.collect(run.run_id)
        self.assertEqual(result.state, AgentRunState.FAILED)
        self.assertEqual(result.exit_code, 7)
        with self.assertRaises(UnknownRunError):
            await self.backend.status("missing-run")

    async def test_cancel_marks_only_that_run_cancelled(self):
        run = await self.backend.start(self.make_request(self.work, "SLEEP"))
        await asyncio.sleep(0.15)
        cancelled = await self.backend.cancel(run.run_id)
        self.assertEqual(cancelled.state, AgentRunState.CANCELLED)
        result = await self.backend.collect(run.run_id)
        self.assertEqual(result.state, AgentRunState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
