#!/usr/bin/env python3
"""Plan or synchronize project instruction files for Claude and Codex."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from engine_state import ConfigError, load_config

SKIP = {".git", "node_modules", ".venv", "venv", "build", "dist", "__pycache__"}
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "AI_CONTEXT.md.template"
AGENTS_POINTER = "Read and follow `{}`: it holds all project instructions for every agent.\n"


def pointers(directory, common):
    relative = Path(os.path.relpath(common, directory)).as_posix()
    return (('@' + relative.replace(' ', r'\ ') + '\n').encode(),
            AGENTS_POINTER.format(relative).encode())


def linked(content, pointer):
    return content.strip() == pointer.strip()


def normalized(content):
    return content.replace(b"\r\n", b"\n").strip()


def validate_path(root, path):
    if root not in (path, *path.parents):
        raise ValueError("path outside project: {}".format(path))
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("symlink in destination path: {}".format(part))
    if not str(path.resolve()).startswith(str(root) + os.sep) and path.resolve() != root:
        raise ValueError("path resolves outside project: {}".format(path))


def ignored_paths(root, paths):
    if not paths:
        return set()
    result = subprocess.run(["git", "check-ignore", "-z", "--stdin"], cwd=root,
                            input=b"\0".join(os.fsencode(p) for p in paths) + b"\0",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        raise ValueError("git check-ignore failed: " + os.fsdecode(result.stderr).strip())
    return set(os.fsdecode(p) for p in result.stdout.split(b"\0") if p)


def rebase_imports(data, source, common):
    # Preserve bytes outside import tokens, including code spans and fenced blocks.
    text = data.decode("utf-8", errors="surrogateescape")
    fence = None
    output = []
    pattern = re.compile(r'(?<![\w@])@((?:\\ |[^\s`])+)')
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            output.append(line)
            continue
        if fence:
            output.append(line)
            continue
        pieces = re.split(r'(`+[^`]*`+)', line)
        for index in range(0, len(pieces), 2):
            def replace(match):
                raw = match.group(1).replace(r'\ ', ' ')
                if raw.startswith(('/', '~')) or '://' in raw:
                    return match.group(0)
                target = source.parent / raw
                relative = os.path.relpath(target, common.parent).replace(os.sep, '/')
                return '@' + relative.replace(' ', r'\ ')
            pieces[index] = pattern.sub(replace, pieces[index])
        output.append(''.join(pieces))
    return ''.join(output).encode('utf-8', errors='surrogateescape')


def scan(root, common_file):
    entries, inventory, claude_only = [], [], []
    git = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).returncode == 0
    directories = []
    walked = []
    candidate_dirs = []
    for current, dirs, files in os.walk(root, followlinks=False):
        directory = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (directory / d).is_symlink())
        candidate_dirs.extend((directory / d).relative_to(root).as_posix() + '/' for d in dirs)
        walked.append((directory, list(dirs), files))
    ignored = ignored_paths(root, candidate_dirs) if git else set()
    for directory, dirs, files in walked:
        relative = directory.relative_to(root).as_posix() + '/'
        if any(relative.startswith(path) for path in ignored):
            continue
        dirs = [d for d in dirs if (directory / d).relative_to(root).as_posix() + '/' not in ignored]
        if 'CLAUDE.local.md' in files:
            claude_only.append((directory / 'CLAUDE.local.md').relative_to(root).as_posix())
        if directory.name == '.claude' and 'rules' in dirs:
            claude_only.append((directory / 'rules').relative_to(root).as_posix() + '/')
        if directory == root or directory.name != '.claude' and any(
                (directory / name).exists() or (directory / name).is_symlink()
                for name in ('CLAUDE.md', 'AGENTS.md')):
            directories.append(directory)
    seen = {}
    for directory in directories:
        common = root / common_file if directory == root else directory / 'AI_CONTEXT.md'
        paths = [directory / 'CLAUDE.md', directory / 'AGENTS.md']
        extra = root / '.claude' / 'CLAUDE.md'
        if directory == root and (extra.exists() or extra.is_symlink()):
            paths.append(extra)
        if common in paths:
            raise ValueError('common file cannot be an instruction pointer: {}'.format(common))
        validate_path(root, common)
        contents, ptrs = [], []
        for path in paths:
            validate_path(root, path.parent)
            if path.is_symlink():
                if path.name != 'CLAUDE.md' or path.parent != directory or not paths[1].is_file() or paths[1].is_symlink() or path.resolve() != paths[1]:
                    raise ValueError('unsupported instruction symlink: {}'.format(path))
            elif path.exists() and not path.is_file():
                raise ValueError('instruction is not a regular file: {}'.format(path))
            contents.append(path.read_bytes() if path.exists() else None)
            ptrs.append(pointers(path.parent, common)[0 if path.name == 'CLAUDE.md' else 1])
        if common.exists() and not common.is_file():
            raise ValueError('common file is not a regular file: {}'.format(common))
        for path in [common] + paths:
            if path.exists() and not path.is_symlink():
                info = path.stat()
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    raise ValueError('hardlinked files in plan: {} and {}'.format(seen[key], path))
                seen[key] = path
        flags = [c is not None and linked(c, p) for c, p in zip(contents, ptrs)]
        if any(flags) and not common.exists():
            raise ValueError('dangling instruction pointer to missing common file: {}'.format(common))
        present = [c is not None for c in contents]
        if all(flags[:2]) and not any(p.is_symlink() for p in paths) and all(flags[2:]):
            kind = 'both_linked'
        elif all(present[:2]):
            kind = 'both'
        elif present[0] or any(present[2:]):
            kind = 'claude_only'
        elif present[1]:
            kind = 'codex_only'
        else:
            kind = 'none'
        sources = []
        for path, content, flag in zip(paths, contents, flags):
            if content is not None and not flag:
                if path.is_symlink():
                    continue
                data = rebase_imports(content, path, common) if path.name == 'CLAUDE.md' else content
                if not any(normalized(data) == normalized(old[1]) for old in sources):
                    sources.append((path.name if path.parent == directory else path.relative_to(root).as_posix(), data, stat.S_IMODE(path.stat().st_mode)))
        existing = common.read_bytes() if common.exists() else None
        distinct = {normalized(data) for _, data, _ in sources if normalized(data)}
        if existing is not None and normalized(existing):
            distinct.add(normalized(existing))
        entry = {'directory': directory.relative_to(root).as_posix(),
                 'common_file': common.relative_to(root).as_posix(),
                 'classification': kind, 'needs_dedupe': len(distinct) >= 2}
        entries.append(entry)
        inventory.append((entry, common, paths, contents, ptrs, sources, existing))
    return {'directories': entries, 'claude_only_files': claude_only}, inventory


def new_common(root):
    commands = load_config(root).get('commands', {})
    if not isinstance(commands, dict):
        raise ValueError('config commands must be an object')
    template = TEMPLATE.read_text(encoding='utf-8')
    for name in ('test', 'lint', 'typecheck'):
        template = template.replace('{{' + name + '}}', str(commands.get(name) or 'Not configured'))
    return template.encode('utf-8')


def stage(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.sync-instructions-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), mode)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


JOURNAL = Path('.claude/orchestrator/instructions-sync.journal.json')
BACKUPS = Path('.claude/orchestrator/instructions-sync-backups')


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_journal(root, journal):
    path = root / JOURNAL
    temp = stage(path, json.dumps(journal, indent=2).encode(), 0o600)
    try:
        os.replace(str(temp), str(path))
        sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def cleanup(root, journal):
    for entry in journal['entries']:
        if entry['backup']:
            backup = root / entry['backup']
            try:
                backup.unlink(missing_ok=True)
            except OSError as exc:
                print('warning: could not remove backup {}: {}'.format(backup, exc), file=sys.stderr)
    backup_dir = root / journal['backup_dir']
    # Only remove empty directories, never unrelated files.
    for current, _, _ in os.walk(backup_dir, topdown=False):
        try:
            Path(current).rmdir()
        except OSError:
            pass
    try:
        (root / BACKUPS).rmdir()
    except OSError:
        pass
    (root / JOURNAL).unlink()
    sync_dir((root / JOURNAL).parent)


def recover(root, common_file=Path('docs/AI_CONTEXT.md')):
    path = root / JOURNAL
    if not path.exists():
        return False
    validate_path(root, path)
    journal = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(journal, dict) or journal.get('state') not in ('preparing', 'ready', 'committed'):
        raise ValueError('invalid instruction sync journal')
    backup_dir = journal.get('backup_dir')
    if not isinstance(backup_dir, str) or BACKUPS not in Path(backup_dir).parents:
        raise ValueError('invalid journal backup directory')
    validate_path(root, root / backup_dir)
    entries = journal.get('entries')
    if not isinstance(entries, list):
        raise ValueError('invalid journal entries')
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('invalid journal entry')
        target = root / entry['path']
        if target.name not in ('CLAUDE.md', 'AGENTS.md', 'AI_CONTEXT.md') and target != root / common_file:
            raise ValueError('invalid journal target: {}'.format(target))
        validate_path(root, target.parent)
        if target.is_symlink() and entry.get('symlink') is None:
            raise ValueError('unexpected symlink in recovery: {}'.format(target))
        if not isinstance(entry.get('existed'), bool):
            raise ValueError('invalid journal entry')
        backup = entry.get('backup')
        if entry['existed']:
            if not isinstance(backup, str) or Path(backup_dir) not in Path(backup).parents:
                raise ValueError('invalid journal backup path')
            validate_path(root, root / backup)
            if journal['state'] == 'ready':
                data = (root / backup).read_bytes()
                if hashlib.sha256(data).hexdigest() != entry.get('sha256'):
                    raise ValueError('backup sha256 mismatch: {}'.format(backup))
        elif backup is not None:
            raise ValueError('invalid journal backup path')
    if journal['state'] == 'ready':
        for entry in entries:
            target = root / entry['path']
            if entry['existed']:
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry.get('symlink') is not None:
                    if target.exists() or target.is_symlink():
                        target.unlink()
                    target.symlink_to(entry['symlink'])
                else:
                    temp = stage(target, (root / entry['backup']).read_bytes(), entry['mode'])
                    os.replace(str(temp), str(target))
            elif target.exists() or target.is_symlink():
                target.unlink()
            if target.parent.exists():
                sync_dir(target.parent)
    cleanup(root, journal)
    return True


def get_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def apply(root, inventory):
    outputs = []
    for entry, common, paths, contents, ptrs, sources, existing in inventory:
        if entry['classification'] == 'both_linked':
            continue
        if existing is None:
            if not sources:
                data = new_common(root)
            elif len(sources) == 1:
                data = sources[0][1]
            else:
                data = b'\n\n'.join(('## From {}\n\n'.format(name)).encode() + data for name, data, _ in sources)
        else:
            data = existing
            sections = re.split(rb'(?m)^## (?:From|Moved from) [^\n]+\n\n', existing)
            known = {normalized(section) for section in sections}
            known.add(normalized(existing))
            for name, source, _ in sources:
                if normalized(source) and normalized(source) not in known:
                    data += (b'\n\n' if data else b'') + ('## Moved from {}\n\n'.format(name)).encode() + source
                    known.add(normalized(source))
        mode = stat.S_IMODE(common.stat().st_mode) if existing is not None else 0o666 & ~get_umask()
        for _, _, source_mode in sources:
            mode &= source_mode
        mode |= 0o600
        if data != existing or existing is not None and stat.S_IMODE(common.stat().st_mode) != mode:
            outputs.append((common, data, mode, existing, False))
        for path, old, pointer in zip(paths, contents, ptrs):
            if old is None or not linked(old, pointer) or path.is_symlink():
                mode = stat.S_IMODE(path.stat().st_mode) if old is not None else 0o666 & ~get_umask()
                outputs.append((path, pointer, mode, old, os.readlink(path) if path.is_symlink() else None))
    outputs.sort(key=lambda item: item[0].name in ("CLAUDE.md", "AGENTS.md"))
    if not outputs:
        return
    journal_path = root / JOURNAL
    validate_path(root, journal_path)
    backup_dir = BACKUPS / uuid.uuid4().hex
    entries = []
    for path, _, _, old, symlink in outputs:
        relative = path.relative_to(root)
        entries.append({'path': relative.as_posix(), 'existed': old is not None,
                        'backup': (backup_dir / relative).as_posix() if old is not None else None,
                        'sha256': hashlib.sha256(old).hexdigest() if old is not None else None,
                        'mode': stat.S_IMODE(path.stat().st_mode) if old is not None else None,
                        'symlink': symlink})
    journal = {'state': 'preparing', 'backup_dir': backup_dir.as_posix(), 'entries': entries}
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    write_journal(root, journal)
    for entry, (_, _, _, old, _) in zip(entries, outputs):
        if old is None:
            continue
        backup = root / entry['backup']
        backup.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(old)
            target.flush()
            os.fsync(target.fileno())
        sync_dir(backup.parent)
    journal['state'] = 'ready'
    write_journal(root, journal)
    for path, data, mode, _, _ in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = stage(path, data, mode)
        try:
            os.replace(str(temp), str(path))
            sync_dir(path.parent)
        finally:
            temp.unlink(missing_ok=True)
    for path, data, mode, _, _ in outputs:
        if (path.is_symlink() or path.read_bytes() != data
                or stat.S_IMODE(path.stat().st_mode) != mode):
            raise ValueError('output verification failed: {}'.format(path))
    journal['state'] = 'committed'
    write_journal(root, journal)
    cleanup(root, journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, default=Path.cwd())
    parser.add_argument('--common-file', type=Path, default=Path('docs/AI_CONTEXT.md'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    root = args.project_dir.resolve()
    try:
        if not root.is_dir():
            raise ValueError('project directory does not exist: {}'.format(root))
        if args.common_file.is_absolute() or '..' in args.common_file.parts:
            raise ValueError('common file must be a path relative to the project root')
        pending = (root / JOURNAL).exists() or (root / JOURNAL).is_symlink()
        if args.apply and pending:
            recover(root, args.common_file)
        config = load_config(root)
        if not isinstance(config.get('commands', {}), dict):
            raise ValueError('config commands must be an object')
        plan, inventory = scan(root, args.common_file)
        if pending and not args.apply:
            plan['pending_recovery'] = True
        if args.apply:
            apply(root, inventory)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            if pending and not args.apply:
                print('Pending recovery from interrupted instruction sync.')
            for entry in plan['directories']:
                print('{directory}: {classification} -> {common_file} (needs_dedupe: {needs_dedupe})'.format(**entry))
            for path in plan['claude_only_files']:
                print('Claude-only file: ' + path)
            print('Applied.' if args.apply else 'Dry run; no changes made.')
    except (OSError, ValueError, TypeError, ConfigError) as exc:
        print('error: {}'.format(exc), file=sys.stderr)
        if args.apply:
            print('re-run with --apply to roll back and retry', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
