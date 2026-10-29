"""Shared fixtures: every test runs against a throwaway project directory."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks" / "scripts"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Empty project directory used as CLAUDE_PROJECT_DIR."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    return tmp_path


@pytest.fixture
def write_config(project):
    def write(data):
        path = project / ".claude" / "orchestrator.config.json"
        path.write_text(json.dumps(data) if isinstance(data, dict) else data)
        return path
    return write
