"""Confirmed, backed-up migration of t-owned instruction blocks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import time


def safe(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f'{parent}: refusing instruction migration through a symlink')


def repositories(document):
    roots = set(document['repositories'])
    for catalogue in document.get('tools', {}).values():
        roots.update(catalogue['repositories'])
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/t'))
    roots.update(api['Config']().repos.values())
    result = set()
    for root in roots:
        result.add(Path(root))
        proc = subprocess.run(['git', '-C', str(root), 'worktree', 'list', '--porcelain', '-z'],
                              text=True, capture_output=True, timeout=5)
        if proc.returncode == 0:
            result.update(Path(line[9:]) for line in proc.stdout.split('\0') if line.startswith('worktree '))
    return result


def migrate_defaults(document):
    """Only the exact retired Codex stage preset is eligible."""
    after = copy.deepcopy(document)
    old = {'planning': {'model': 'gpt-6-astra', 'effort': 'auto'},
           'implementation': {'model': 'gpt-6.1-sol', 'effort': 'auto'},
           'review': {'model': 'gpt-6-astra', 'effort': 'auto'}}
    for policy in [*after['workflows'].values(),
                   *(entry['inline'] for entry in after['repositories'].values() if 'inline' in entry)]:
        if (all(policy[key] == value for key, value in old.items()) and
                not any(key in policy for key in ('stages_enabled', 'model_stages', 'separate_stages',
                                                  'session_defaults', 'inference'))):
            policy['planning'] = {'model': 'gpt-6.1-sol', 'effort': 'medium'}
            policy['implementation'] = {'model': 'gpt-6.1-sol', 'effort': 'medium'}
            policy['review'] = {'model': 'gpt-6-astra', 'effort': 'medium'}
            policy['model_stages'] = ['review']
            policy['session_defaults'] = {'codex': {'model': 'gpt-6.1-sol', 'effort': 'medium'}}
    return after


def changes(w, document, roots):
    candidates = {path: ('claude' if path.name == 'CLAUDE.md' else 'codex', True)
                  for path in w['native_paths']()}
    for root in roots:
        for name, tool in (('AGENTS.md', 'codex'), ('AGENTS.override.md', 'codex'), ('CLAUDE.md', 'claude')):
            path = root / name
            if path.exists() or path.is_symlink():
                candidates[path] = (tool, False)
    updates = []
    for path, (tool, global_file) in sorted(candidates.items()):
        if not global_file and path.is_symlink():
            print('Preserving linked repository instructions: ' + str(path))
            continue  # Never replace a link or mutate its external destination.
        safe(path)
        before = w['snapshot'](path)
        text = before[0] if before else ''
        if not global_file and not any(marker in text for marker in (w['BEGIN'], w['ISSUE_BEGIN'])):
            continue  # A hand-written repository file is not ours to replace.
        if path.name == 'AGENTS.override.md' and not text.strip():
            continue
        after = w['merge'](text, w['fallback'](tool))
        if text != after:
            updates.append((path, before, after))
    return updates


def install(w, *, roots=None, confirm=None):
    document, before = w['load']()
    proposed = w['_ensure_claude_catalogue'](migrate_defaults(document), document if before else None)
    updates = changes(w, proposed, repositories(proposed) if roots is None else roots)
    config = w['config_path']()
    if before is None or w['decode'](before[0]) != proposed:
        safe(config)
        updates.append((config, before, w['encode'](proposed)))
    if not updates:
        return 0
    backup = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 't/instruction-backups' / (str(time.time_ns()) + '-v1')
    print('Instruction update: back up and rebuild t-managed global and repository blocks.')
    for path, _, _ in updates:
        print('  ' + str(path))
    print('Backup: ' + str(backup))
    try:
        accepted = confirm() if confirm is not None else (
            sys.stdin.isatty() and input('Back up and rebuild these global and repository instructions? [y/N] ').strip().lower() in ('y', 'yes'))
    except EOFError:
        accepted = False
    if not accepted:
        print('Instruction migration deferred; existing instructions preserved. Run t system update again to review.')
        return 0
    safe(backup)
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for index, (path, original, _) in enumerate(updates):
        safe(path)
        if w['snapshot'](path) != original:
            raise ValueError(f'{path}: changed after review; retry the migration')
        entry = {'path': str(path), 'existed': original is not None}
        if original is not None:
            data = original[0].encode('utf-8')
            stored = backup / str(index)
            stored.write_bytes(data)
            stored.chmod(0o600)
            entry.update(backup=stored.name, sha256=hashlib.sha256(data).hexdigest(),
                         mode=stat.S_IMODE(original[1][2]))
        manifest.append(entry)
    index = backup / 'manifest.json'
    index.write_text(json.dumps({'version': 1, 'files': manifest}, indent=2) + '\n')
    index.chmod(0o600)
    w['publish'](updates)
    print('Rebuilt managed instructions; hand-written content preserved.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(install(runpy.run_path(str(Path(__file__).with_name('t_workflow.py')))))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('t install: instruction migration pending: ' + str(exc), file=sys.stderr)
        sys.exit(1)
