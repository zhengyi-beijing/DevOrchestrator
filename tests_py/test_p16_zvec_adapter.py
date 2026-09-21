import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.zvec import ZvecAdapter


class TestP16ZvecAdapter(unittest.TestCase):
    def test_probe_nonexistent_executable_fails_closed(self):
        adapter = ZvecAdapter(executable_path="C:\\nonexistent\\zvec_binary.exe")
        res = adapter.probe()
        self.assertFalse(res.supported)
        self.assertIn("not found", res.error_message or "")
        self.assertIsNone(res.version)
        self.assertFalse(res.local_only_verified)

    def test_query_path_traversal_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td) / "repo"
            repo_root.mkdir()
            (repo_root / "valid.py").write_text("print('hello')", encoding="utf-8")

            index_dir = Path(td) / "index"
            index_dir.mkdir()

            # Create mock script that emits hits including path traversal
            mock_script = Path(td) / "mock_zvec.py"
            mock_script.write_text(
                'import sys, json\n'
                'if "--version" in sys.argv:\n'
                '    print("zvec 1.2.0")\n'
                '    sys.exit(0)\n'
                'if "query" in sys.argv:\n'
                '    hits = [\n'
                '        {"path": "valid.py", "start_line": 1, "end_line": 1, "score": 0.9},\n'
                '        {"path": "../../secret.txt", "start_line": 1, "end_line": 2, "score": 0.99},\n'
                '        {"path": "/etc/passwd", "start_line": 1, "end_line": 1, "score": 0.99},\n'
                '        {"path": "valid.py", "start_line": 10, "end_line": 2, "score": 0.5}\n'  # invalid span
                '    ]\n'
                '    print(json.dumps(hits))\n'
                '    sys.exit(0)\n'
                'sys.exit(0)\n',
                encoding="utf-8",
            )

            # Create batch wrapper on Windows
            wrapper_cmd = Path(td) / "zvec_wrapper.bat"
            wrapper_cmd.write_text(f'@"{sys.executable}" "{mock_script}" %*\r\n', encoding="utf-8")

            adapter = ZvecAdapter(executable_path=wrapper_cmd)
            probe = adapter.probe()
            self.assertTrue(probe.supported)
            self.assertEqual(probe.version, "zvec 1.2.0")

            hits = adapter.query(index_dir, repo_root, "test query")
            # Only valid.py:1-1 should be accepted; path traversals and inverted spans rejected!
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].path, "valid.py")
            self.assertEqual(hits[0].start_line, 1)
            self.assertEqual(hits[0].end_line, 1)

    def test_stats_on_empty_and_nonexistent_index(self):
        adapter = ZvecAdapter()
        stats = adapter.stats(Path("C:\\nonexistent_index_dir"))
        self.assertFalse(stats["exists"])
        self.assertEqual(stats["size_bytes"], 0)

    def test_verify_network_denial_program_match_and_mismatch(self):
        from unittest.mock import patch, MagicMock
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            exe_path = Path(f.name)
        try:
            adapter = ZvecAdapter(executable_path=exe_path)

            # 1. Program matches
            match_output = (
                f"Rule Name: Block-Zvec-Outbound\n"
                f"Enabled: Yes\n"
                f"Direction: Out\n"
                f"Action: Block\n"
                f"Program: {exe_path}\n"
            )
            mock_proc_ok = MagicMock(returncode=0, stdout=match_output)
            with patch("subprocess.run", return_value=mock_proc_ok):
                self.assertTrue(adapter.verify_network_denial("Block-Zvec-Outbound"))

            # 2. Program mismatches
            mismatch_output = (
                f"Rule Name: Block-Zvec-Outbound\n"
                f"Enabled: Yes\n"
                f"Direction: Out\n"
                f"Action: Block\n"
                f"Program: C:\\different\\path\\malicious.exe\n"
            )
            mock_proc_mismatch = MagicMock(returncode=0, stdout=mismatch_output)
            with patch("subprocess.run", return_value=mock_proc_mismatch):
                self.assertFalse(adapter.verify_network_denial("Block-Zvec-Outbound"))

            # 3. Direction: In with "out" substring in name
            in_output = (
                f"Rule Name: Block-Zvec-Outbound-Check\n"
                f"Enabled: Yes\n"
                f"Direction: In\n"
                f"Action: Block\n"
                f"Program: {exe_path}\n"
            )
            mock_proc_in = MagicMock(returncode=0, stdout=in_output)
            with patch("subprocess.run", return_value=mock_proc_in):
                self.assertFalse(adapter.verify_network_denial("Block-Zvec-Outbound"))

            # 4. Action: Allow with "block" substring in name
            allow_output = (
                f"Rule Name: Block-Zvec-Outbound\n"
                f"Enabled: Yes\n"
                f"Direction: Out\n"
                f"Action: Allow\n"
                f"Program: {exe_path}\n"
            )
            mock_proc_allow = MagicMock(returncode=0, stdout=allow_output)
            with patch("subprocess.run", return_value=mock_proc_allow):
                self.assertFalse(adapter.verify_network_denial("Block-Zvec-Outbound"))
        finally:
            exe_path.unlink(missing_ok=True)

    def test_probe_requires_local_probes_before_verifying_local_only(self):
        from unittest.mock import patch, MagicMock
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            exe_path = Path(f.name)
        try:
            adapter = ZvecAdapter(executable_path=exe_path)
            # When verify_network_denial returns True, but _verify_local_probes fails
            with patch.object(adapter, "verify_network_denial", return_value=True), \
                 patch.object(adapter, "_verify_local_probes", return_value=False), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="zvec 1.0.0")):
                probe = adapter.probe()
                self.assertTrue(probe.supported)
                self.assertFalse(probe.local_only_verified)

            # When both return True
            adapter2 = ZvecAdapter(executable_path=exe_path)
            with patch.object(adapter2, "verify_network_denial", return_value=True), \
                 patch.object(adapter2, "_verify_local_probes", return_value=True), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="zvec 1.0.0")):
                probe = adapter2.probe()
                self.assertTrue(probe.supported)
                self.assertTrue(probe.local_only_verified)
        finally:
            exe_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
