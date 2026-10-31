"""Offline coverage for session usage aggregation."""
from __future__ import annotations

import json
import os

import pytest

import session_usage


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def assistant(request, usage, **extra):
    return {"type": "assistant", "requestId": request,
            "message": {"model": "claude", "usage": usage}, **extra}


def test_session_aggregation(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {"timestamp": "2026-01-01T10:00:00Z"},
        assistant("one", {"input_tokens": 999}),
        assistant("one", {"input_tokens": 10, "cache_read_input_tokens": 5,
                          "cache_creation_input_tokens": 2, "output_tokens": 3}),
        {"timestamp": "2026-01-01T11:00:00Z"},
    ])
    with transcript.open("a") as stream:
        stream.write("{malformed\n")
    write_jsonl(tmp_path / "session/subagents/agent-one.jsonl", [
        assistant("two", {"input_tokens": 7}, effort="high"),
        {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 500}}},
    ])
    codex = tmp_path / "codex"
    def rollout(name, cwd, time):
        write_jsonl(codex / "sessions/2026" / name, [
            {"type": "turn_context", "timestamp": time,
             "payload": {"cwd": str(cwd), "model": "codex", "effort": "high"}},
            {"type": "event_msg", "timestamp": time, "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 30, "cached_input_tokens": 12,
                                      "output_tokens": 8, "reasoning_output_tokens": 4}}}},
        ])
    rollout("rollout-good.jsonl", tmp_path, "2026-01-01T10:30:00Z")
    rollout("rollout-other.jsonl", tmp_path.parent / "other", "2026-01-01T10:30:00Z")
    rollout("rollout-late.jsonl", tmp_path, "2099-01-01T12:00:00Z")
    rows = session_usage.collect(transcript, tmp_path.resolve(), codex)
    assert [(r["engine"], r["effort"], r["count"], r["total"]) for r in rows] == [
        ("Codex", "high", 1, 38), ("Claude", "default", 1, 20), ("Claude", "high", 1, 7)]
    assert rows[0]["fresh_input"] == 18
    assert rows[0]["cache_read"] == 12
    assert rows[1]["cache_write"] == 2


def test_cli_json_and_table(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [assistant("one", {"input_tokens": 1234})])
    session_usage.main(["--transcript", str(transcript), "--json"])
    assert json.loads(capsys.readouterr().out)["total"]["total"] == 1234
    session_usage.main(["--transcript", str(transcript)])
    output = capsys.readouterr().out
    assert "TOTAL" in output and "1,234" in output


def test_default_discovery_prefers_session_id(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project.with spaces"
    project.mkdir()
    directory = tmp_path / ".claude/projects" / "".join(
        char if char.isalnum() else "-" for char in str(project))
    selected = directory / "selected.jsonl"
    newer = directory / "newer.jsonl"
    write_jsonl(selected, [assistant("one", {"input_tokens": 7})])
    write_jsonl(newer, [assistant("two", {"input_tokens": 99})])
    os.utime(selected, (1, 1))
    monkeypatch.setattr(session_usage.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selected")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert session_usage.main(["--project-dir", str(project), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"]["total"] == 7
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert session_usage.main(["--project-dir", str(project), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"]["total"] == 99


def test_rollout_deltas_and_window(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [{"timestamp": "2026-01-01T11:00:00Z"}])
    write_jsonl(tmp_path / "session/subagents/agent-one.jsonl", [
        {"timestamp": "2026-01-01T10:00:00Z"}])
    codex = tmp_path / "codex"
    def context(cwd, model, effort):
        return {"type": "turn_context", "timestamp": "2026-01-01T09:00:00Z",
                "payload": {"cwd": cwd, "model": model, "effort": effort}}
    def event(time, amount):
        return {"type": "event_msg", "timestamp": time, "payload": {
            "type": "token_count", "info": {"total_token_usage": {"input_tokens": amount}}}}
    write_jsonl(codex / "sessions/rollout-one.jsonl", [
        context(str(tmp_path / "subdir"), "first", "low"),
        event("2026-01-01T09:30:00Z", 100),
        event("2026-01-01T10:30:00Z", 120),
        context(str(tmp_path), "second", "high"),
        event("2026-01-01T11:30:00Z", 150),
        context([str(tmp_path)], "ignored", "low"),
        event("2026-01-01T12:00:00Z", 200),
    ])
    old = codex / "sessions/rollout-old.jsonl"
    write_jsonl(old, [context(str(tmp_path), "old", "low"), event("2026-01-01T11:00:00Z", 900)])
    os.utime(old, (1, 1))
    rows = session_usage.collect(transcript, tmp_path.resolve(), codex)
    assert [(row["model"], row["effort"], row["count"], row["total"]) for row in rows] == [
        ("second", "high", 1, 30), ("first", "low", 1, 20)]


def test_selection_oserror(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_usage.Path, "home", lambda: tmp_path)
    directory = tmp_path / ".claude/projects" / "".join(
        char if char.isalnum() else "-" for char in str(tmp_path.resolve()))
    write_jsonl(directory / "session.jsonl", [])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    original = session_usage.Path.stat
    def stat(path, *args, **kwargs):
        if path.name == "session.jsonl":
            raise OSError("stat failed")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(session_usage.Path, "stat", stat)
    with pytest.raises(SystemExit) as error:
        session_usage.main(["--project-dir", str(tmp_path)])
    assert error.value.code == 1
    assert "session-usage: stat failed" in capsys.readouterr().err
