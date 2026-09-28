import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from dev_orchestrator.ai.execution_transport import SSHTransportConfig
from dev_orchestrator.control.reconcile import _git_distance
from dev_orchestrator.platform import process as process_platform
from dev_orchestrator.transport.ssh_channel import run_remote_helper_envelope


class HiddenSubprocessKwargsTests(unittest.TestCase):
    def assert_windows_hidden_kwargs(self, kwargs):
        expected_flag = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        self.assertEqual(kwargs["creationflags"], expected_flag)
        startupinfo = kwargs["startupinfo"]
        use_show_window = int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001))
        self.assertEqual(startupinfo.dwFlags & use_show_window, use_show_window)
        self.assertEqual(startupinfo.wShowWindow, int(getattr(subprocess, "SW_HIDE", 0)))

    def test_windows_uses_create_no_window_and_hidden_startup_info(self):
        with patch.object(process_platform.os, "name", "nt"):
            kwargs = process_platform.hidden_subprocess_kwargs()

        self.assert_windows_hidden_kwargs(kwargs)

    def test_non_windows_keeps_original_subprocess_behavior(self):
        with patch.object(process_platform.os, "name", "posix"):
            self.assertEqual(process_platform.hidden_subprocess_kwargs(), {})


class GitDistanceSubprocessTests(unittest.TestCase):
    def test_success_preserves_result_and_hides_window_on_windows(self):
        completed = subprocess.CompletedProcess([], 0, stdout="3\n", stderr="")
        with (
            patch.object(process_platform.os, "name", "nt"),
            patch("subprocess.run", return_value=completed) as run,
        ):
            result = _git_distance("C:/repo", "old", "new")

        self.assertEqual(result, 3)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["git", "rev-list", "--count", "old..new"])
        call_kwargs = run.call_args.kwargs
        self.assertEqual(call_kwargs["cwd"], "C:/repo")
        self.assertTrue(call_kwargs["capture_output"])
        self.assertTrue(call_kwargs["text"])
        self.assertEqual(call_kwargs["timeout"], 5)
        HiddenSubprocessKwargsTests().assert_windows_hidden_kwargs(call_kwargs)

    def test_non_windows_does_not_add_process_creation_kwargs(self):
        completed = subprocess.CompletedProcess([], 0, stdout="2\n", stderr="")
        with (
            patch.object(process_platform.os, "name", "posix"),
            patch("subprocess.run", return_value=completed) as run,
        ):
            result = _git_distance("/repo", "old", "new")

        self.assertEqual(result, 2)
        run.assert_called_once_with(
            ["git", "rev-list", "--count", "old..new"],
            cwd="/repo",
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_git_failure_still_returns_none(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="bad revision")
        with patch("subprocess.run", return_value=completed):
            self.assertIsNone(_git_distance("C:/repo", "old", "new"))

    def test_timeout_still_returns_none(self):
        timeout = subprocess.TimeoutExpired(["git", "rev-list"], 5)
        with patch("subprocess.run", side_effect=timeout):
            self.assertIsNone(_git_distance("C:/repo", "old", "new"))


class SSHChannelSubprocessTests(unittest.TestCase):
    def test_windows_ssh_transport_hides_console_window(self):
        cfg = SSHTransportConfig(peer="host.example", expected_host_identity="host.example")
        fake_subprocess = MagicMock()
        fake_subprocess.run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({
                "request_id": "request-1",
                "host_identity": "host.example",
            }).encode("utf-8"),
            stderr=b"",
        )

        with patch.object(process_platform.os, "name", "nt"):
            result = run_remote_helper_envelope(
                cfg,
                {"request_id": "request-1"},
                30,
                subprocess_module=fake_subprocess,
            )

        self.assertEqual(result["request_id"], "request-1")
        HiddenSubprocessKwargsTests().assert_windows_hidden_kwargs(
            fake_subprocess.run.call_args.kwargs
        )


if __name__ == "__main__":
    unittest.main()
