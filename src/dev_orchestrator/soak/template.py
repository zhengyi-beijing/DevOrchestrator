"""P20.4 Tracked Pure-Software Soak Template and Project Generator."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Optional

from dev_orchestrator.soak.permit import SoakPermit, DEFAULT_DISPOSABLE_MARKER
from dev_orchestrator.storage.json_store import utc_now_iso, write_json

SOAK_TASK_SPECS: tuple[dict[str, Any], ...] = (
    {
        "task_id": "SOAK-T1",
        "kind": "feature",
        "title": "Implement bounded arithmetic operations",
        "description": "Add arithmetic operations add and multiply with boundary checks to math_utils.py.",
        "files": {
            "src/math_utils.py": (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "def multiply(a: int, b: int) -> int:\n"
                "    return a * b\n"
            ),
            "tests/test_math_utils.py": (
                "import unittest\n"
                "from src.math_utils import add, multiply\n\n"
                "class TestMathUtils(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
                "    def test_multiply(self):\n"
                "        self.assertEqual(multiply(4, 5), 20)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        },
    },
    {
        "task_id": "SOAK-T2",
        "kind": "edge_case",
        "title": "Fix division by zero and negative inputs",
        "description": "Add divide operation handling division by zero with ValueError to math_utils.py.",
        "files": {
            "src/math_utils.py": (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "def multiply(a: int, b: int) -> int:\n"
                "    return a * b\n\n"
                "def divide(a: int, b: int) -> float:\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            "tests/test_math_utils.py": (
                "import unittest\n"
                "from src.math_utils import add, multiply, divide\n\n"
                "class TestMathUtils(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
                "    def test_multiply(self):\n"
                "        self.assertEqual(multiply(4, 5), 20)\n"
                "    def test_divide_zero(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            divide(5, 0)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        },
    },
    {
        "task_id": "SOAK-T3",
        "kind": "refactor",
        "title": "Refactor arithmetic into Calculator class preserving behavior",
        "description": "Encapsulate operations in Calculator class in math_utils.py while keeping module functions as delegates.",
        "files": {
            "src/math_utils.py": (
                "class Calculator:\n"
                "    @staticmethod\n"
                "    def add(a: int, b: int) -> int:\n"
                "        return a + b\n"
                "    @staticmethod\n"
                "    def multiply(a: int, b: int) -> int:\n"
                "        return a * b\n"
                "    @staticmethod\n"
                "    def divide(a: int, b: int) -> float:\n"
                "        if b == 0:\n"
                "            raise ValueError('division by zero')\n"
                "        return a / b\n\n"
                "add = Calculator.add\n"
                "multiply = Calculator.multiply\n"
                "divide = Calculator.divide\n"
            ),
        },
    },
    {
        "task_id": "SOAK-T4",
        "kind": "parser",
        "title": "Implement structured JSON payload validator",
        "description": "Add validate_payload to parser.py validating required keys and types.",
        "files": {
            "src/parser.py": (
                "import json\n"
                "from typing import Any\n\n"
                "def validate_payload(raw: str) -> dict[str, Any]:\n"
                "    data = json.loads(raw)\n"
                "    if not isinstance(data, dict):\n"
                "        raise ValueError('payload must be an object')\n"
                "    if 'id' not in data or 'type' not in data:\n"
                "        raise ValueError('missing required fields')\n"
                "    return data\n"
            ),
            "tests/test_parser.py": (
                "import unittest\n"
                "from src.parser import validate_payload\n\n"
                "class TestParser(unittest.TestCase):\n"
                "    def test_valid(self):\n"
                "        res = validate_payload('{\"id\": \"1\", \"type\": \"item\"}')\n"
                "        self.assertEqual(res['id'], '1')\n"
                "    def test_invalid(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            validate_payload('{\"id\": \"1\"}')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        },
    },
    {
        "task_id": "SOAK-T5",
        "kind": "api",
        "title": "Implement in-memory item store API",
        "description": "Add ItemStore API with get, put and list methods in api.py with tests.",
        "files": {
            "src/api.py": (
                "from typing import Any\n\n"
                "class ItemStore:\n"
                "    def __init__(self) -> None:\n"
                "        self._items: dict[str, Any] = {}\n"
                "    def put(self, key: str, value: Any) -> None:\n"
                "        self._items[key] = value\n"
                "    def get(self, key: str) -> Any:\n"
                "        return self._items.get(key)\n"
                "    def list_all(self) -> list[str]:\n"
                "        return sorted(self._items.keys())\n"
            ),
            "tests/test_api.py": (
                "import unittest\n"
                "from src.api import ItemStore\n\n"
                "class TestApi(unittest.TestCase):\n"
                "    def test_store(self):\n"
                "        store = ItemStore()\n"
                "        store.put('a', 1)\n"
                "        self.assertEqual(store.get('a'), 1)\n"
                "        self.assertEqual(store.list_all(), ['a'])\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        },
    },
)


def generate_soak_project(
    permit: SoakPermit,
    template_root: Path | str | None = None,
) -> dict[str, Any]:
    """Generate the disposable soak git repository and isolated runtime."""
    repo_dir = Path(permit.disposable_repo_path)
    runtime_dir = Path(permit.disposable_runtime_root)
    repo_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # 1. Place marker file
    marker_file = repo_dir / permit.disposable_marker_name
    write_json(marker_file, {
        "run_id": permit.run_id,
        "project_id": permit.soak_project_id,
        "created_at": utc_now_iso(),
    }, indent=2)

    # 2. Initialize git repository if not already initialized
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "DevO Soak"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "soak@devorchestrator.local"], cwd=str(repo_dir), check=True)

    # 3. Write initial agent files
    agent_dir = repo_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "CURRENT.md").write_text("# Soak Project Current State\n", encoding="utf-8")
    (agent_dir / "HANDOFF.md").write_text("# Soak Handoff\n", encoding="utf-8")
    (agent_dir / "result.md").write_text("# Soak Result\n", encoding="utf-8")
    (agent_dir / "next.md").write_text(
        f"# {SOAK_TASK_SPECS[0]['task_id']} — {SOAK_TASK_SPECS[0]['title']}\n\n"
        f"Status: **READY_TO_RUN**\n\n"
        f"{SOAK_TASK_SPECS[0]['description']}\n",
        encoding="utf-8",
    )

    # 4. Write initial files for task 1
    t1_files = SOAK_TASK_SPECS[0]["files"]
    for rel_path, content in t1_files.items():
        fp = repo_dir / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    # 5. Initial Git commit
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: initial soak template"], cwd=str(repo_dir), check=True, capture_output=True)
    cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    initial_commit = cp.stdout.strip()

    # 6. Generate isolated control plane config
    ports = {
        "bridge_port": 19765,
        "daemon_port": 19770,
        "broker_port": 19775,
    }
    config_dir = repo_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    projects_config = {
        "projects": [
            {
                "id": permit.soak_project_id,
                "name": "P20 Unattended Soak Project",
                "repo_path": str(repo_dir),
                "active_branch": "main",
                "worker_timeout_seconds": 14400,
                "roles": {
                    "planner": {"enabled": True, "provider_independence": True},
                    "reviewer": {"enabled": True, "provider_independence": True},
                    "worker": {"enabled": True},
                },
            }
        ]
    }
    config_path = config_dir / "projects.json"
    write_json(config_path, projects_config, indent=2)

    exec_jobs_config = {
        "enabled": True,
        "runtime_root": str(runtime_dir),
        "projects": {
            permit.soak_project_id: {
                "repo_path": str(repo_dir),
                "file_roots": [str(repo_dir)],
                "commands": {
                    "pytest_all": {
                        "argv": ["python", "-m", "pytest", "-q"],
                        "cwd": ".",
                        "effect_class": "read_only",
                    }
                }
            }
        }
    }
    write_json(config_dir / "execution-jobs.json", exec_jobs_config, indent=2)

    return {
        "repo_path": str(repo_dir),
        "runtime_root": str(runtime_dir),
        "config_path": str(config_path),
        "marker_path": str(marker_file),
        "initial_commit": initial_commit,
        "ports": ports,
    }
