"""Configuration loading and default path resolution.

``config/projects.json`` remains the public configuration source; entries are
returned verbatim so the monitor never rewrites the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "projects.json"
DEFAULT_RUNTIME_ROOT = REPO_ROOT / "runtime"
DEFAULT_WEB_ROOT = REPO_ROOT / "web"


def resolve_config_path(value: Optional[str]) -> Path:
    return Path(value).expanduser() if value else DEFAULT_CONFIG_PATH


def resolve_runtime_root(value: Optional[str]) -> Path:
    return Path(value).expanduser() if value else DEFAULT_RUNTIME_ROOT


def resolve_web_root(value: Optional[str]) -> Path:
    return Path(value).expanduser() if value else DEFAULT_WEB_ROOT


def load_projects_config(path: Path | str) -> dict[str, Any]:
    """Load and validate the projects configuration file."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8-sig") as handle:
        try:
            data = json.load(handle)
        except ValueError as exc:
            raise ValueError("invalid config JSON in {0}: {1}".format(config_path, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ValueError("config {0} must contain a \"projects\" list".format(config_path))
    for index, project in enumerate(data["projects"]):
        if not isinstance(project, dict) or not project.get("id"):
            raise ValueError("config {0} project #{1} is missing an id".format(config_path, index))
    return data
