"""Offline coverage for instruction file synchronization."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_instructions.py"


def run(root, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), "--project-dir", str(root),
                             "--json", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_empty_template_and_idempotence(tmp_path):
    config = tmp_path / ".claude" / "orchestrator.config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"commands": {"test": "pytest"}}))
    before = snapshot(tmp_path)
    assert run(tmp_path)["directories"][0]["classification"] == "none"
    assert snapshot(tmp_path) == before
    run(tmp_path, "--apply")
    assert b"Test: pytest" in (tmp_path / "docs/AI_CONTEXT.md").read_bytes()
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"@docs/AI_CONTEXT.md\n"
    assert (tmp_path / "AGENTS.md").read_bytes() == b"Read and follow `docs/AI_CONTEXT.md`: it holds all project instructions for every agent.\n"
    before = snapshot(tmp_path)
    assert run(tmp_path, "--apply")["directories"][0]["classification"] == "both_linked"
    assert snapshot(tmp_path) == before


def test_scenarios_nested_and_bytes(tmp_path):
    content = "Κανόνες\r\n😀".encode()
    (tmp_path / "CLAUDE.md").write_bytes(content)
    (tmp_path / "CLAUDE.md.bak").write_bytes(b"old")
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_bytes(b"codex")
    same = tmp_path / "same"
    same.mkdir()
    (same / "CLAUDE.md").write_bytes(b"same\r\n")
    (same / "AGENTS.md").write_bytes(b"same\n\n")
    both = tmp_path / "both"
    both.mkdir()
    (both / "CLAUDE.md").write_bytes(b"claude")
    (both / "AGENTS.md").write_bytes(b"agents")
    plan = {e["directory"]: e for e in run(tmp_path)["directories"]}
    assert plan["."]["classification"] == "claude_only"
    assert plan["codex"]["classification"] == "codex_only"
    assert plan["both"]["needs_dedupe"] and not plan["same"]["needs_dedupe"]
    run(tmp_path, "--apply")
    assert (tmp_path / "docs/AI_CONTEXT.md").read_bytes() == content
    assert (tmp_path / "CLAUDE.md.bak").read_bytes() == b"old"
    assert (codex / "AI_CONTEXT.md").read_bytes() == b"codex"
    assert (same / "AI_CONTEXT.md").read_bytes() == b"same\r\n"
    assert (both / "AI_CONTEXT.md").read_bytes() == b"## From CLAUDE.md\n\nclaude\n\n## From AGENTS.md\n\nagents"
    before = snapshot(tmp_path)
    run(tmp_path, "--apply")
    assert snapshot(tmp_path) == before


def test_existing_common_and_pointer_detection(tmp_path):
    (tmp_path / "common.md").write_bytes(b"existing")
    (tmp_path / "CLAUDE.md").write_bytes(b"  @common.md  \n\n")
    (tmp_path / "AGENTS.md").write_bytes(b"new\r\ncontent")
    run(tmp_path, "--common-file", "common.md", "--apply")
    assert (tmp_path / "common.md").read_bytes() == b"existing\n\n## Moved from AGENTS.md\n\nnew\r\ncontent"
    before = snapshot(tmp_path)
    run(tmp_path, "--common-file", "common.md", "--apply")
    assert snapshot(tmp_path) == before


def test_symlink_is_single_source(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"original")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    run(tmp_path, "--apply")
    assert not (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "docs/AI_CONTEXT.md").read_bytes() == b"original"


def test_gitignore_and_template_not_scanned(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("templates/\n")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("ignored")
    (tmp_path / "AI_CONTEXT.md.template").write_text("template")
    assert len(run(tmp_path)["directories"]) == 1


def failure(root, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), '--project-dir', str(root),
                             '--apply', *args], capture_output=True, text=True)
    assert result.returncode == 2
    return result.stderr


def test_refuse_unsafe_paths_and_hardlinks(tmp_path):
    import os

    for kind in ('common', 'parent', 'instruction', 'hardlink'):
        root = tmp_path / kind
        root.mkdir()
        (root / 'AGENTS.md').write_text('rules')
        if kind == 'common':
            (root / 'common').symlink_to(root / 'AGENTS.md')
        elif kind == 'parent':
            (root / 'parent').symlink_to(tmp_path, target_is_directory=True)
        elif kind == 'instruction':
            (root / 'CLAUDE.md').symlink_to(root / 'AGENTS.md')
            (root / 'AGENTS.md').unlink()
            (root / 'AGENTS.md').write_text('rules')
            (root / 'CLAUDE.md').unlink()
            (root / 'CLAUDE.md').symlink_to('../outside')
        else:
            os.link(root / 'AGENTS.md', root / 'CLAUDE.md')
        args = ('--common-file', 'common') if kind == 'common' else (('--common-file', 'parent/common') if kind == 'parent' else ())
        before = snapshot(root)
        assert any(word in failure(root, *args) for word in ('symlink', 'hardlink'))
        assert snapshot(root) == before


def test_dangling_and_invalid_config(tmp_path):
    (tmp_path / 'CLAUDE.md').write_text('@docs/AI_CONTEXT.md\n')
    assert 'dangling' in failure(tmp_path)
    config = tmp_path / '.claude/orchestrator.config.json'
    config.parent.mkdir()
    config.write_text('{')
    assert 'invalid JSON' in failure(tmp_path)


def test_imports_spaces_and_claude_scope(tmp_path):
    claude = tmp_path / '.claude'
    claude.mkdir()
    (claude / 'CLAUDE.md').write_text('@rules.md\n`@inline.md`\n```\n@fenced.md\n```\n')
    run(tmp_path, '--common-file', 'shared dir/context.md', '--apply')
    common = (tmp_path / 'shared dir/context.md').read_text()
    assert '@../.claude/rules.md' in common
    assert '`@inline.md`' in common and '@fenced.md' in common
    assert (tmp_path / 'CLAUDE.md').read_text() == '@shared\\ dir/context.md\n'
    assert (claude / 'CLAUDE.md').read_text() == '@../shared\\ dir/context.md\n'
    assert not (claude / 'AGENTS.md').exists()
    before = snapshot(tmp_path)
    run(tmp_path, '--common-file', 'shared dir/context.md', '--apply')
    assert snapshot(tmp_path) == before


def test_existing_common_dedupe_and_private_backup(tmp_path):
    (tmp_path / 'common').write_text('existing')
    source = tmp_path / 'AGENTS.md'
    source.write_text('new')
    source.chmod(0o600)
    assert run(tmp_path, '--common-file', 'common')['directories'][0]['needs_dedupe']
    run(tmp_path, '--common-file', 'common', '--apply')
    assert (tmp_path / 'common').stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob('*.bak'))
    assert not (tmp_path / '.claude/orchestrator/instructions-sync.journal.json').exists()


def test_ignored_root_and_non_ascii_directory(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('CLAUDE.md\nκρυφό/\n')
    (tmp_path / 'CLAUDE.md').write_text('important')
    ignored = tmp_path / 'κρυφό'
    ignored.mkdir()
    (ignored / 'AGENTS.md').write_text('ignored')
    run(tmp_path, '--apply')
    assert (tmp_path / 'docs/AI_CONTEXT.md').read_text() == 'important'
    assert (ignored / 'AGENTS.md').read_text() == 'ignored'
    before = snapshot(tmp_path)
    run(tmp_path, '--apply')
    assert snapshot(tmp_path) == before


def test_replace_failure_rolls_back_and_retry(tmp_path, monkeypatch):
    import importlib.util
    import os

    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location('sync_instructions', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    (tmp_path / 'CLAUDE.md').write_text('original')
    (tmp_path / 'AGENTS.md').write_text('other')
    _, inventory = module.scan(tmp_path, Path('docs/AI_CONTEXT.md'))
    real_replace = os.replace
    count = [0]
    def fail_once(src, dst):
        count[0] += 1
        if count[0] == 2:
            raise OSError('injected failure')
        real_replace(src, dst)
    monkeypatch.setattr(module.os, 'replace', fail_once)
    import pytest
    with pytest.raises(OSError, match='injected'):
        module.apply(tmp_path, inventory)
    monkeypatch.setattr(module.os, 'replace', real_replace)
    run(tmp_path, '--apply')
    after = snapshot(tmp_path)
    run(tmp_path, '--apply')
    assert snapshot(tmp_path) == after


def test_interrupted_recovery_and_corrupt_backup(tmp_path, monkeypatch):
    import importlib.util
    import os
    import pytest

    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location('sync_recovery', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    for stop in (0, 1, 2):
        root = tmp_path / str(stop)
        root.mkdir()
        (root / 'CLAUDE.md').write_text('original')
        (root / 'AGENTS.md').write_text('other')
        (root / 'foo.bak').write_text('unrelated')
        _, inventory = module.scan(root, Path('docs/AI_CONTEXT.md'))
        original = os.replace
        count = [0]
        def interrupt(src, dst):
            if Path(dst).name != 'instructions-sync.journal.json':
                if count[0] == stop:
                    raise RuntimeError('interrupted')
                count[0] += 1
            original(src, dst)
        monkeypatch.setattr(module.os, 'replace', interrupt)
        with pytest.raises(RuntimeError):
            module.apply(root, inventory)
        monkeypatch.setattr(module.os, 'replace', original)
        before = snapshot(root)
        assert run(root)['pending_recovery']
        assert snapshot(root) == before
        if stop == 1:
            journal = json.loads((root / module.JOURNAL).read_text())
            backup = root / next(e['backup'] for e in journal['entries'] if e['backup'])
            saved = backup.read_bytes()
            backup.write_bytes(b'corrupt')
            damaged = snapshot(root)
            assert 'sha256 mismatch' in failure(root)
            assert snapshot(root) == damaged
            backup.write_bytes(saved)
        run(root, '--apply')
        assert (root / 'docs/AI_CONTEXT.md').read_bytes() == b'## From CLAUDE.md\n\noriginal\n\n## From AGENTS.md\n\nother'
        assert (root / 'foo.bak').read_text() == 'unrelated'
        assert not (root / module.JOURNAL).exists()
        assert not any(p.is_file() for p in (root / module.BACKUPS).rglob('*'))


def test_tampered_journal_refused_without_changes(tmp_path):
    journal = tmp_path / '.claude/orchestrator/instructions-sync.journal.json'
    journal.parent.mkdir(parents=True)
    victim = tmp_path / 'important.txt'
    victim.write_text('keep')
    journal.write_text(json.dumps({'state': 'ready', 'backup_dir': '.claude/orchestrator/instructions-sync-backups/test',
                                   'entries': [{'path': 'important.txt', 'existed': False, 'backup': None}]}))
    before = snapshot(tmp_path)
    assert 'invalid journal target' in failure(tmp_path)
    assert snapshot(tmp_path) == before


def test_common_sections_and_owner_permissions(tmp_path):
    common = tmp_path / 'common'
    common.write_text('## Moved from CLAUDE.md\n\nlonger text\n\n## Moved from AGENTS.md\n\nidentical\n')
    common.chmod(0o400)
    (tmp_path / 'CLAUDE.md').write_text('text')
    (tmp_path / 'AGENTS.md').write_text('identical\r\n')
    run(tmp_path, '--common-file', 'common', '--apply')
    assert common.read_text().count('identical') == 1
    assert '## Moved from CLAUDE.md\n\ntext' in common.read_text()
    assert common.stat().st_mode & 0o600 == 0o600
