"""Shared fixtures: every test runs against a throwaway project directory."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "orchestrator.config.json"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks" / "scripts"))


@pytest.fixture(autouse=True)
def usage_files(tmp_path_factory, monkeypatch):
    """Never read the real ~/.claude or ~/.codex usage; tests write these paths instead."""
    base = tmp_path_factory.mktemp("usage")
    monkeypatch.setenv("ORCHESTRATOR_CLAUDE_USAGE_FILE", str(base / "statusline-input.json"))
    monkeypatch.setenv("CODEX_HOME", str(base / "codex"))
    monkeypatch.setenv("ORCHESTRATOR_OPENROUTER_USAGE_FILE", str(base / "openrouter-key.json"))
    monkeypatch.setenv("ORCHESTRATOR_OPENROUTER_MODELS_FILE", str(base / "openrouter-models.json"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_BASE", raising=False)
    # Tests pin model ids and scores: run them on a frozen map, not the shipped (re-seeded) one.
    config = json.loads(TEMPLATE.read_text())
    config.update(json.loads((ROOT / "tests" / "capability_map.json").read_text()))
    (base / "default-config.json").write_text(json.dumps(config))
    monkeypatch.setenv("ORCHESTRATOR_DEFAULT_CONFIG", str(base / "default-config.json"))
    return base


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


class FakeOpenRouter:
    """Local stand-in for the OpenRouter API; edit ``key_data``, ``models`` and ``chat_*`` per test.

    ``/chat/completions`` answers ``chat_status`` with ``chat_body`` (a dict, or raw text)
    after ``chat_delay`` seconds and records each request in ``chat_requests``.
    """

    def __init__(self):
        self.key_data = {"usage_daily": 0, "usage_weekly": 0, "usage_monthly": 0,
                         "limit": None, "limit_remaining": None, "limit_reset": None}
        self.models = []
        self.chat_status = 200
        self.chat_body = self.answer('{"route": "standard", "reason": "fake"}')
        self.chat_delay = 0
        self.chat_requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.chat_requests.append({"path": self.path, "headers": dict(self.headers),
                                            "body": raw.decode()})
                if self.path != "/chat/completions":
                    return self._send(404, {})
                time.sleep(outer.chat_delay)
                self._send(outer.chat_status, outer.chat_body)

            def do_GET(self):
                if self.path == "/key":
                    if not self.headers.get("Authorization", "").startswith("Bearer "):
                        return self._send(401, {"error": "unauthorized"})
                    return self._send(200, {"data": outer.key_data})
                if self.path == "/models":
                    return self._send(200, {"data": outer.models})
                self._send(404, {})

            def _send(self, code, body):
                raw = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def answer(content: str) -> dict:
        """A chat completion whose message content is ``content``."""
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.fixture
def fake_openrouter(monkeypatch):
    fake = FakeOpenRouter()
    monkeypatch.setenv("OPENROUTER_API_BASE", fake.base)
    yield fake
    fake.server.shutdown()
    fake.server.server_close()
