import json
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.corpus import (
    CORPUS_FILES,
    build_manifest,
    generate_synthetic_corpus,
    verify_corpus_manifest,
)
from aibench.prompts import (
    COMMON_RETRIEVAL_INSTRUCTION,
    get_canonical_tasks,
    render_task_prompt,
)


class TestP16CorpusAndPrompts(unittest.TestCase):
    def test_synthetic_corpus_generation_and_verification(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "corpus"
            repo_path, head_sha, manifest = generate_synthetic_corpus(target)

            self.assertTrue(repo_path.is_dir())
            self.assertTrue((repo_path / "pyproject.toml").is_file())
            self.assertTrue((repo_path / "src" / "synth_app" / "core" / "auth.py").is_file())
            self.assertTrue((repo_path / "src" / "synth_app" / "core" / "cache.py").is_file())
            self.assertTrue((repo_path / "src" / "synth_app" / "services" / "billing.py").is_file())
            self.assertTrue((repo_path / "src" / "synth_app" / "repository" / "account_repo.py").is_file())
            self.assertTrue((repo_path / "src" / "synth_app" / "handlers" / "api.py").is_file())
            self.assertTrue((repo_path / "src" / "synth_app" / "compat" / "legacy_api.py").is_file())
            self.assertTrue((repo_path / "docs" / "architecture.md").is_file())
            self.assertTrue((repo_path / "docs" / "compatibility.md").is_file())
            self.assertTrue((repo_path / "manifest.json").is_file())

            self.assertIsNotNone(head_sha)
            self.assertIn("corpus_manifest_hash", manifest)
            self.assertEqual(len(manifest["files"]), len(CORPUS_FILES))

            # Verify manifest matches
            ok, errors = verify_corpus_manifest(repo_path)
            self.assertTrue(ok, f"Manifest verification failed: {errors}")

            # Tamper with a file and verify failure
            auth_file = repo_path / "src" / "synth_app" / "core" / "auth.py"
            auth_file.write_text("tampered content", encoding="utf-8")
            ok_tampered, errors_tampered = verify_corpus_manifest(repo_path)
            self.assertFalse(ok_tampered)
            self.assertTrue(any("hash mismatch" in e for e in errors_tampered))

    def test_canonical_tasks_and_immutable_prompts(self):
        tasks = get_canonical_tasks()
        self.assertEqual(len(tasks), 4)
        roles = {t.role for t in tasks}
        self.assertEqual(roles, {"planner", "reviewer", "worker", "debugger"})

        for task in tasks:
            prompt = render_task_prompt(task)
            self.assertIn(COMMON_RETRIEVAL_INSTRUCTION.strip(), prompt.prompt_text)
            self.assertIn("[EXPECTED_JSON_SCHEMA]", prompt.prompt_text)
            self.assertEqual(prompt.task_id, task.task_id)

            # Assert hashing reproducibility
            prompt2 = render_task_prompt(task)
            self.assertEqual(prompt.prompt_hash, prompt2.prompt_hash)
            self.assertEqual(prompt.prompt_bytes, prompt2.prompt_bytes)

    def test_no_ground_truth_leaked_into_prompt(self):
        tasks = get_canonical_tasks()
        for task in tasks:
            prompt = render_task_prompt(task)
            # Ground truth markers like prohibited findings or test solutions should never appear in prompt text
            for prohibited in task.ground_truth.get("prohibited_findings", []):
                self.assertNotIn(prohibited, prompt.prompt_text)


if __name__ == "__main__":
    unittest.main()
