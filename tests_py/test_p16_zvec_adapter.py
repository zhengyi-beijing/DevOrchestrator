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


if __name__ == "__main__":
    unittest.main()
