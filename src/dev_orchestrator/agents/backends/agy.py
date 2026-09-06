"""Real ``agy`` agent backend adapter.

Execution contract (frozen in ``docs/AGENT_BACKEND_ROUTER_DESIGN.md``):

- Default command prefix is ``agy`` resolved with ``shutil.which``; tests may
  inject an explicit command prefix (e.g. ``(python, fake_agy.py)``).  The
  explicit prefix makes the ambient daemon PATH irrelevant.
- ``probe()`` executes only ``<prefix> --version`` — it never sends a prompt.
- ``start()`` runs::

      <prefix> --mode <mode> --output-format <output_format>
               --print-timeout <timeout>s --add-dir <working_directory>
               [--model <model>] [--effort <effort>]
               --print=<workspace_instruction + user_prompt>

  with ``shell=False`` and an explicit argv list.  The working directory comes
  from the request and must exist.
- A short workspace instruction is prepended to the user prompt so agy tool
  shells operate in the correct repository directory; we never rely on process
  cwd alone because agy may use its own scratch directory by default.
- Each run's stdout/stderr is captured under DevOrchestrator-owned
  ``<runtime_root>/agent-runs/<run_id>/stdout.log`` / ``stderr.log``; the
  backend never writes files into the observed project.
- ``cancel()`` terminates only the recorded process belonging to that run.
- Terminality is finalized exactly once: when ``status()``/``collect()`` observe
  that the process has ended, the internal record (state, exit code, log
  handles) is settled; ``cancel()`` preserves that true terminal state and
  never relabels an already-completed run as CANCELLED.
- Unknown run ids fail closed with ``UnknownRunError``.
- ``--dangerously-skip-permissions`` is never added.

Capability identity: backend id ``agy``, provider/independence domain
``agy``, default invocation mode ``accept-edits``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional, Sequence

from dev_orchestrator.agents.base import AgentBackend, UnknownRunError
from dev_orchestrator.agents.models import (
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentRun,
    AgentRunState,
    BackendStatus,
    CapabilitySet,
    QuotaState,
)
from dev_orchestrator.config import DEFAULT_RUNTIME_ROOT
from dev_orchestrator.platform.process import hidden_subprocess_kwargs

_DEFAULT_MODE = "accept-edits"
_DEFAULT_OUTPUT_FORMAT = "text"
_DEFAULT_PRINT_TIMEOUT = 300  # seconds; configurable per-instance
_RUNS_DIRNAME = "agent-runs"
_STDOUT_LOG = "stdout.log"
_STDERR_LOG = "stderr.log"
_PROBE_TIMEOUT_SECONDS = 15.0
_DEFAULT_TAGS = frozenset({"code", "repository", "text"})

_TERMINAL_STATES = (
    AgentRunState.COMPLETED,
    AgentRunState.FAILED,
    AgentRunState.CANCELLED,
)

# Workspace instruction template prepended to every user prompt so that agy
# tool shells operate inside the correct repository directory regardless of the
# process cwd that agy itself chooses.
_WORKSPACE_INSTRUCTION_TEMPLATE = (
    "[WORKSPACE] Repository directory: {working_directory}. "
    "All terminal commands MUST operate inside that directory. "
    "Do not use any other working directory.\n\n"
)


def _resolve_command_prefix(command_prefix: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Resolve the configured prefix; the default is ``agy`` from ``PATH``.

    When ``agy`` cannot be discovered the literal ``("agy",)`` prefix is kept
    so that ``probe()`` can report a precise unavailable reason instead of
    failing at construction time.
    """
    if command_prefix is not None:
        resolved = tuple(str(part) for part in command_prefix)
        if not resolved:
            raise ValueError("command_prefix must not be empty")
        return resolved
    discovered = shutil.which("agy")
    if discovered:
        return (discovered,)
    return ("agy",)


@dataclass
class _RunState:
    """Internal, mutable per-run tracking (never exposed to callers)."""

    run_id: str
    request: AgentRequest
    run_dir: Path
    process: Optional[subprocess.Popen] = None
    state: AgentRunState = AgentRunState.RUNNING
    exit_code: Optional[int] = None
    stdout_handle: Optional[BinaryIO] = None
    stderr_handle: Optional[BinaryIO] = None
    settled: bool = False


class AgyBackend(AgentBackend):
    """One real backend: launches the ``agy`` CLI in non-interactive ``--print`` mode."""

    def __init__(
        self,
        runtime_root: Optional[Path | str] = None,
        command_prefix: Optional[Sequence[str]] = None,
        mode: str = _DEFAULT_MODE,
        output_format: str = _DEFAULT_OUTPUT_FORMAT,
        print_timeout: int = _DEFAULT_PRINT_TIMEOUT,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        """Construct an AgyBackend.

        Parameters
        ----------
        runtime_root:
            Root directory for DevOrchestrator-owned run artefacts.  Defaults
            to ``DEFAULT_RUNTIME_ROOT``.
        command_prefix:
            Explicit executable/command prefix (e.g. ``["agy"]`` or a test
            shim).  When *None* ``agy`` is resolved from ``PATH``.  Providing
            an explicit prefix makes the ambient daemon PATH irrelevant.
        mode:
            Passed verbatim as ``--mode <mode>`` (default ``accept-edits``).
        output_format:
            Passed verbatim as ``--output-format <output_format>`` (default
            ``text``).
        print_timeout:
            Seconds passed as ``--print-timeout <print_timeout>`` (default
            300 s).
        model:
            Optional model name forwarded as ``--model <model>``; omitted when
            *None*.
        effort:
            Optional effort level forwarded as ``--effort <effort>``; omitted
            when *None*.
        """
        self._runtime_root = (
            Path(runtime_root) if runtime_root is not None else DEFAULT_RUNTIME_ROOT
        )
        self._command_prefix = _resolve_command_prefix(command_prefix)
        self._mode = mode
        self._output_format = output_format
        self._print_timeout = print_timeout
        self._model = model
        self._effort = effort
        self._runs: dict[str, _RunState] = {}

    # -- identity -----------------------------------------------------------

    @property
    def backend_id(self) -> str:
        return "agy"

    @property
    def runtime_root(self) -> Path:
        """DevOrchestrator-owned directory holding ``agent-runs/<run_id>``."""
        return self._runtime_root

    @property
    def command_prefix(self) -> tuple[str, ...]:
        """Executable prefix prepended to every backend argv."""
        return self._command_prefix

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            roles=frozenset(AgentRole),
            tags=_DEFAULT_TAGS,
            provider=self.backend_id,
            independence_domain=self.backend_id,
            cancellation_supported=True,
        )

    # -- probe (never sends a prompt) ---------------------------------------

    def probe(self) -> BackendStatus:
        """Run ``<prefix> --version`` only; quota is always reported UNKNOWN.

        A successful version probe reports the first output line as the
        version label; failures report an unavailable status with the
        diagnostic reason.
        """
        argv = [*self._command_prefix, "--version"]
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus(
                self.backend_id,
                False,
                "probe --version failed: {0}".format(exc),
                QuotaState.UNKNOWN,
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if not detail:
                detail = "exit code {0}".format(proc.returncode)
            return BackendStatus(
                self.backend_id,
                False,
                "probe --version failed: {0}".format(detail),
                QuotaState.UNKNOWN,
            )
        first_line = (proc.stdout or "").strip().splitlines()
        label = first_line[0].strip() if first_line else "agy"
        return BackendStatus(
            self.backend_id, True, "", QuotaState.UNKNOWN, model_label=label
        )

    # -- run lifecycle ------------------------------------------------------

    def _build_argv(self, request: AgentRequest) -> list[str]:
        """Build the full argv for one non-interactive agy invocation."""
        working_directory = str(Path(request.working_directory))
        workspace_prefix = _WORKSPACE_INSTRUCTION_TEMPLATE.format(
            working_directory=working_directory
        )
        full_prompt = workspace_prefix + request.prompt

        argv: list[str] = [
            *self._command_prefix,
            "--mode", self._mode,
            "--output-format", self._output_format,
            "--print-timeout", "{0}s".format(self._print_timeout),
            "--add-dir", working_directory,
        ]
        if self._model is not None:
            argv.extend(["--model", self._model])
        if self._effort is not None:
            argv.extend(["--effort", self._effort])
        argv.append("--print=" + full_prompt)
        return argv

    async def start(self, request: AgentRequest) -> AgentRun:
        """Start one non-interactive ``agy --print`` run for ``request``."""
        cwd = Path(request.working_directory)
        if not cwd.is_dir():
            raise ValueError(
                "AgyBackend working directory does not exist: {0}".format(cwd)
            )
        run_id = uuid.uuid4().hex
        run_dir = self._runtime_root / _RUNS_DIRNAME / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        record = _RunState(run_id=run_id, request=request, run_dir=run_dir)
        self._runs[run_id] = record

        stdout_path = run_dir / _STDOUT_LOG
        stderr_path = run_dir / _STDERR_LOG
        argv = self._build_argv(request)
        out_handle = stdout_path.open("wb")
        err_handle = stderr_path.open("wb")
        record.stdout_handle = out_handle
        record.stderr_handle = err_handle
        try:
            # Use a real Popen process handle instead of asyncio's subprocess
            # watcher. Managed Workers run inside a background thread with its
            # own event loop; on Windows a completed child can otherwise leave
            # ``Process.wait()`` pending even after the OS process has exited.
            # Popen.wait() is delegated to a worker thread in _settle(), so the
            # async backend contract remains non-blocking while terminal status
            # is anchored directly to the operating-system process handle.
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=out_handle,
                stderr=err_handle,
                shell=False,
                **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            record.state = AgentRunState.FAILED
            try:
                err_handle.write(
                    ("failed to start agy: {0}\n".format(exc)).encode("utf-8")
                )
            except OSError:
                pass
            self._close_handles(record)
            record.settled = True
            return self._to_run(record)
        record.process = process
        return self._to_run(record)

    async def status(self, run_id: str) -> AgentRun:
        """Current snapshot of an existing run (unknown ids fail closed).

        When the recorded process has already ended, the run record is
        finalized exactly once (state, exit code, log handles) so later calls
        — including ``cancel()`` — observe the true terminal state.
        """
        record = self._require_run(run_id)
        if (
            not record.settled
            and record.process is not None
            and record.process.poll() is not None
        ):
            await self._settle(record)
        return self._to_run(record)

    async def cancel(self, run_id: str) -> AgentRun:
        """Cancel only the recorded process of ``run_id``.

        A run whose process already ended keeps its true terminal state
        (``COMPLETED``/``FAILED``) and is never relabelled ``CANCELLED``; only
        a genuinely running process is killed and marked ``CANCELLED``.
        """
        record = self._require_run(run_id)
        if (
            not record.settled
            and record.process is not None
            and record.process.poll() is not None
        ):
            # Process exited on its own: finalize to the true terminal state.
            await self._settle(record)
        if record.state in _TERMINAL_STATES:
            return self._to_run(record)
        process = record.process
        if process is not None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await self._settle(record, forced=AgentRunState.CANCELLED)
        return self._to_run(record)

    async def collect(self, run_id: str) -> AgentResult:
        """Wait for completion, finalize state and return captured output."""
        record = self._require_run(run_id)
        if record.state not in _TERMINAL_STATES:
            await self._settle(record)
        return AgentResult(
            run_id=run_id,
            backend_id=self.backend_id,
            state=record.state,
            exit_code=record.exit_code,
            stdout=record.run_dir.joinpath(_STDOUT_LOG).read_text(
                encoding="utf-8", errors="replace"
            ),
            stderr=record.run_dir.joinpath(_STDERR_LOG).read_text(
                encoding="utf-8", errors="replace"
            ),
        )

    # -- helpers ------------------------------------------------------------

    def _require_run(self, run_id: str) -> _RunState:
        record = self._runs.get(run_id)
        if record is None:
            raise UnknownRunError("unknown run id: {0}".format(run_id))
        return record

    async def _settle(
        self, record: _RunState, forced: Optional[AgentRunState] = None
    ) -> None:
        """Finalize a run record exactly once (state, exit code, handles).

        Waits for a still-running recorded process, then derives the true
        terminal state from its exit code — or applies ``forced`` for a
        genuine cancellation — records the exit code and closes the
        stdout/stderr handles.  Idempotent under concurrent callers: the
        ``settled`` flag is re-checked after the (only) await so exactly one
        caller performs the finalization.
        """
        if record.settled:
            return
        process = record.process
        if process is not None and process.poll() is None:
            try:
                await asyncio.to_thread(process.wait)
            except ProcessLookupError:
                pass
        if record.settled:
            return
        return_code = process.poll() if process is not None else None
        if forced is not None:
            record.state = forced
        elif record.state not in _TERMINAL_STATES:
            if process is None or return_code != 0:
                record.state = AgentRunState.FAILED
            else:
                record.state = AgentRunState.COMPLETED
        if return_code is not None:
            record.exit_code = return_code
        self._close_handles(record)
        record.settled = True

    @staticmethod
    def _close_handles(record: _RunState) -> None:
        for handle in (record.stdout_handle, record.stderr_handle):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        record.stdout_handle = None
        record.stderr_handle = None

    def _to_run(self, record: _RunState) -> AgentRun:
        process = record.process
        exit_code = record.exit_code
        if exit_code is None and process is not None:
            exit_code = process.poll()
        return AgentRun(
            run_id=record.run_id,
            backend_id=self.backend_id,
            state=record.state,
            exit_code=exit_code,
            pid=process.pid if process is not None else None,
            project_id=record.request.project_id,
        )
