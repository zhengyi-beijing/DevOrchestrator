"""Tests for the ``agy`` agent backend.

Covers:
- probe: executes only ``--version``; reports UNKNOWN quota; captures label.
- argv / workspace instruction construction: ``--print``, ``--mode``,
  ``--output-format``, ``--print-timeout``, ``--add-dir`` are present; the
  workspace instruction is prepended to the prompt; optional ``--model`` /
  ``--effort`` appear only when configured.
- ``--dangerously-skip-permissions`` is never present.
- Successful run: COMPLETED, exit_code=0, stdout/stderr captured in
  DevOrchestrator-owned run dir.
- Nonzero exit: FAILED state + correct exit code; unknown run id raises
  ``UnknownRunError``.
- Cancel: marks the run CANCELLED.
"""

import asyncio
import json
import sys
import tempfile
import threading
import textwrap
import unittest
from pathlib import Path

from dev_orchestrator.agents.backends.agy import AgyBackend
from dev_orchestrator.agents.backends import AgyBackend as AgyBackendFromPkg
from dev_orchestrator.agents.base import UnknownRunError
from dev_orchestrator.agents.models import AgentRequest, AgentRole, AgentRunState, QuotaState


# ---------------------------------------------------------------------------
# Fake agy executable
# ---------------------------------------------------------------------------

FAKE_AGY = r'''
import sys, json, time

args = sys.argv[1:]

if "--version" in args:
    print("fake-agy 0.1.0")
    raise SystemExit(0)

# Dump invocation details so tests can inspect them
details = {"argv": args}
print(json.dumps(details))

# Real agy 1.1.26 requires the prompt attached as --print=<prompt>.
prompt_arg = next((arg for arg in args if arg.startswith("--print=")), "")
prompt = prompt_arg.split("=", 1)[1] if prompt_arg else ""

if "SLEEP" in prompt:
    time.sleep(30)
elif "FAIL7" in prompt:
    print("fake failure", file=sys.stderr)
    raise SystemExit(7)
else:
    # Extract just the user portion after the workspace prefix
    user_part = prompt.split("\n\n", 1)[-1] if "\n\n" in prompt else prompt
    print("OUT:" + user_part)
    print("ERR:" + user_part, file=sys.stderr)
'''


class AgyBackendTests(unittest.IsolatedAsyncioTestCase):
    def make_request(self, cwd, prompt="HELLO", project_id="fixture"):
        return AgentRequest(
            project_id=project_id,
            role=AgentRole.WORKER,
            prompt=prompt,
            working_directory=cwd,
            required_capabilities=frozenset({"code"}),
        )

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_agy.py"
        self.fake.write_text(textwrap.dedent(FAKE_AGY), encoding="utf-8")
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "sentinel.txt").write_text("unchanged", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.backend = AgyBackend(
            runtime_root=self.runtime,
            command_prefix=(sys.executable, str(self.fake)),
            project="agy-fixture",
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    # -- package export -------------------------------------------------------

    def test_agy_backend_exported_from_backends_package(self):
        """AgyBackend must be importable directly from the backends package."""
        self.assertIs(AgyBackendFromPkg, AgyBackend)

    # -- probe ----------------------------------------------------------------

    async def test_probe_is_non_prompt_and_reports_unknown_quota(self):
        status = self.backend.probe()
        self.assertTrue(status.available, status.reason)
        self.assertEqual(status.backend_id, "agy")
        self.assertEqual(status.quota, QuotaState.UNKNOWN)
        self.assertIn("fake-agy", status.model_label)

    # -- argv / workspace instruction / prompt construction ------------------

    def test_argv_contains_required_flags(self):
        """_build_argv must include --print, --mode, --output-format,
        --print-timeout, and --add-dir."""
        request = self.make_request(self.work, "DO SOMETHING")
        argv = self.backend._build_argv(request)
        self.assertTrue(any(arg.startswith("--print=") for arg in argv))
        self.assertIn("--mode", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("--print-timeout", argv)
        self.assertIn("--add-dir", argv)

    def test_argv_default_mode_is_accept_edits(self):
        request = self.make_request(self.work, "x")
        argv = self.backend._build_argv(request)
        idx = argv.index("--mode")
        self.assertEqual(argv[idx + 1], "accept-edits")

    def test_argv_default_output_format_is_text(self):
        request = self.make_request(self.work, "x")
        argv = self.backend._build_argv(request)
        idx = argv.index("--output-format")
        self.assertEqual(argv[idx + 1], "text")

    def test_argv_add_dir_matches_working_directory(self):
        request = self.make_request(self.work, "x")
        argv = self.backend._build_argv(request)
        idx = argv.index("--add-dir")
        self.assertEqual(Path(argv[idx + 1]), Path(self.work))

    def test_argv_project_uses_provider_project_not_request_project_id(self):
        request = self.make_request(self.work, "x", project_id="LabDemo")
        argv = self.backend._build_argv(request)
        idx = argv.index("--project")
        self.assertEqual(argv[idx + 1], "agy-fixture")

    async def test_missing_provider_project_is_unavailable_and_start_has_no_artifacts(self):
        backend = AgyBackend(
            runtime_root=self.runtime,
            command_prefix=(sys.executable, str(self.fake)),
            project="   ",
        )
        status = backend.probe()
        self.assertFalse(status.available)
        self.assertIn("provider project not configured", status.reason)
        with self.assertRaisesRegex(ValueError, "non-empty provider project"):
            await backend.start(self.make_request(self.work, "x"))
        self.assertEqual(backend._runs, {})
        self.assertFalse((self.runtime / "agent-runs").exists())

    def test_argv_print_timeout_is_configurable(self):
        backend = AgyBackend(
            runtime_root=self.runtime,
            command_prefix=(sys.executable, str(self.fake)),
            project="agy-fixture",
            print_timeout=60,
        )
        request = self.make_request(self.work, "x")
        argv = backend._build_argv(request)
        idx = argv.index("--print-timeout")
        self.assertEqual(argv[idx + 1], "60s")

    def test_argv_model_and_effort_omitted_when_not_set(self):
        request = self.make_request(self.work, "x")
        argv = self.backend._build_argv(request)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_argv_model_and_effort_present_when_configured(self):
        backend = AgyBackend(
            runtime_root=self.runtime,
            command_prefix=(sys.executable, str(self.fake)),
            project="agy-fixture",
            model="gemini-2.5-pro",
            effort="high",
        )
        request = self.make_request(self.work, "x")
        argv = backend._build_argv(request)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-2.5-pro")
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_workspace_instruction_prepended_to_prompt(self):
        """The final argv element (the prompt) must contain the workspace
        instruction with the absolute working directory path."""
        request = self.make_request(self.work, "MY TASK")
        argv = self.backend._build_argv(request)
        print_arg = next(arg for arg in argv if arg.startswith("--print="))
        full_prompt = print_arg.split("=", 1)[1]
        self.assertIn("[WORKSPACE]", full_prompt)
        self.assertIn(str(self.work), full_prompt)
        self.assertIn("MY TASK", full_prompt)
        # Workspace instruction must come first
        self.assertLess(
            full_prompt.index("[WORKSPACE]"),
            full_prompt.index("MY TASK"),
        )

    def test_dangerous_skip_permissions_never_in_argv(self):
        """--dangerously-skip-permissions must never appear in any argv."""
        for prompt in ("HELLO", "SLEEP", "FAIL7"):
            request = self.make_request(self.work, prompt)
            argv = self.backend._build_argv(request)
            for arg in argv:
                self.assertNotIn("dangerously", arg.lower(), msg=f"Dangerous flag found in argv for prompt={prompt!r}")

    # -- successful collect ---------------------------------------------------

    async def test_start_status_collect_captures_output_without_project_writes(self):
        before = (self.work / "sentinel.txt").read_bytes()
        run = await self.backend.start(self.make_request(self.work, "HELLO"))
        self.assertEqual(run.backend_id, "agy")
        result = await self.backend.collect(run.run_id)
        self.assertEqual(result.state, AgentRunState.COMPLETED)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("OUT:HELLO", result.stdout)
        self.assertIn("ERR:HELLO", result.stderr)
        # The project directory must be untouched by the backend
        self.assertEqual((self.work / "sentinel.txt").read_bytes(), before)
        # Logs must live in DevOrchestrator's runtime directory
        self.assertTrue(
            (self.runtime / "agent-runs" / run.run_id / "stdout.log").is_file()
        )
        self.assertTrue(
            (self.runtime / "agent-runs" / run.run_id / "stderr.log").is_file()
        )

    async def test_two_backends_pass_distinct_provider_projects(self):
        first_backend = AgyBackend(
            runtime_root=self.runtime / "a",
            command_prefix=(sys.executable, str(self.fake)),
            project="agy-labdemo",
        )
        second_backend = AgyBackend(
            runtime_root=self.runtime / "b",
            command_prefix=(sys.executable, str(self.fake)),
            project="agy-xray",
        )
        first = await first_backend.start(
            self.make_request(self.work, "FIRST", project_id="labdemo")
        )
        second = await second_backend.start(
            self.make_request(self.work, "SECOND", project_id="xray-hw-platform")
        )
        first_result = await first_backend.collect(first.run_id)
        second_result = await second_backend.collect(second.run_id)
        first_argv = json.loads(first_result.stdout.splitlines()[0])["argv"]
        second_argv = json.loads(second_result.stdout.splitlines()[0])["argv"]
        self.assertEqual(first_argv[first_argv.index("--project") + 1], "agy-labdemo")
        self.assertEqual(second_argv[second_argv.index("--project") + 1], "agy-xray")
    def test_collect_completes_from_background_thread_event_loop(self):
        """Managed AGY runs must settle from the executor's worker thread.

        TransitionExecutor owns a daemon thread and calls asyncio.run() there.
        On Windows the backend must not depend on an asyncio subprocess watcher
        tied to that thread-local loop for terminal notification.
        """
        results = []
        errors = []

        def target():
            async def execute():
                run = await self.backend.start(
                    self.make_request(self.work, "THREAD-COLLECT")
                )
                results.append(await self.backend.collect(run.run_id))

            try:
                asyncio.run(execute())
            except BaseException as exc:  # captured for assertion in test thread
                errors.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

        self.assertFalse(thread.is_alive(), "background collect did not terminate")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, AgentRunState.COMPLETED)
        self.assertEqual(results[0].exit_code, 0)
        self.assertIn("OUT:THREAD-COLLECT", results[0].stdout)

    # -- nonzero exit / unknown run id ----------------------------------------

    async def test_nonzero_exit_is_failed_and_unknown_run_is_typed_error(self):
        run = await self.backend.start(self.make_request(self.work, "FAIL7"))
        result = await self.backend.collect(run.run_id)
        self.assertEqual(result.state, AgentRunState.FAILED)
        self.assertEqual(result.exit_code, 7)
        with self.assertRaises(UnknownRunError):
            await self.backend.status("missing-run-id")

    # -- cancel ---------------------------------------------------------------

    async def test_cancel_marks_only_that_run_cancelled(self):
        run = await self.backend.start(self.make_request(self.work, "SLEEP"))
        await asyncio.sleep(0.15)
        cancelled = await self.backend.cancel(run.run_id)
        self.assertEqual(cancelled.state, AgentRunState.CANCELLED)
        result = await self.backend.collect(run.run_id)
        self.assertEqual(result.state, AgentRunState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
