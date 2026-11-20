"""Read-only discovery of loaded Codex desktop conversations outside t.

A held writer lock, exact rollout identity and a live desktop ancestor are all
required. Saved index entries, backend cwd and process names alone are not live
conversations. These rows deliberately have no process-close capability.
"""
import os
from pathlib import Path
import re
import runpy
import sqlite3
import subprocess
import sys


OWNERS = runpy.run_path(str(Path(__file__).with_name('t_codex_ownership.py')))
STORE = runpy.run_path(str(Path(__file__).with_name('t_codex_store.py')))
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=5)


def processes(runner):
    result = runner(['ps', '-A', '-o', 'pid=,ppid=,comm='])
    if result.returncode:
        raise ValueError('could not inspect Codex desktop ownership')
    table = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            table[int(fields[0])] = (int(fields[1]), fields[2])
    return table


def desktop_owner(pid, table):
    """Actual frontend ancestry, not an app bundle's CLI or helper executable."""
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent, executable = table[pid]
        if any(executable.endswith('/' + name + '.app/Contents/MacOS/' + name)
               for name in ('ChatGPT', 'Codex')):
            return pid
        pid = parent
    return None


def holders(folder, runner):
    result = runner(['lsof', '-nP', '-Fpn', '+d', str(folder)])
    # lsof uses 1 for no open files. Permission/inspection errors are unknown.
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ValueError('could not inspect Codex writer holders')
    found, pid = {}, None
    for line in result.stdout.splitlines():
        if line.startswith('p'):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith('n') and pid:
            filename = Path(line[1:])
            if filename.parent == folder and filename.suffix == '.lock' and UUID.fullmatch(filename.stem):
                found.setdefault(filename.stem, set()).add(pid)
    return found


def clean(value):
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in str(value or '')).split())


def discover(home, existing=(), *, runner=run, held=None, transcripts=False):
    home = Path(home)
    folder = OWNERS['_lock_dir'](str(home))
    if not folder:
        return []
    folder = Path(folder)
    held = held or OWNERS['_lock_held']
    table = processes(runner)
    if not any(desktop_owner(pid, table) for pid in table):
        return []
    owners = holders(folder, runner)
    candidates = {sid: pids for sid, pids in owners.items()
                  if all(desktop_owner(pid, table) for pid in pids) and held(folder / (sid + '.lock'))}
    seen = {line.split('\t')[0] for line in existing}
    candidates = {sid: pids for sid, pids in candidates.items() if sid not in seen}
    if not candidates:
        return []
    records = STORE['rows'](home / 'state_5.sqlite',
        'select id,cwd,rollout_path,title,source from threads where archived=0',
        initialize_sidecars=False, timeout=.2)
    pending = []
    for sid, cwd, rollout, title, source in records:
        if sid not in candidates or not isinstance(source, str) or '"subagent"' in source:
            continue
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or any(not c.isprintable() for c in cwd):
            continue
        try:
            if os.path.realpath(OWNERS['_rollout_cwd'](rollout, sid)) != os.path.realpath(cwd):
                continue
        except (OSError, ValueError, TypeError):
            continue
        repo = re.sub(r'[^\w.-]', '-', Path(cwd).name) or 'codex'
        short = sid if sum(other[:8] == sid[:8] for other in candidates) > 1 else sid[:8]
        label = f'{repo}:app-{short}'  # Full identity also stays in field 1.
        row = [sid, cwd, label, 'app', 'active', clean(title) or 'Codex app conversation', 'codex']
        if transcripts:
            row.append(rollout)
        pending.append((sid, '\t'.join(row)))
    if not pending:
        return []
    # A second live observation rejects owner/ancestry changes during metadata I/O.
    after, current = processes(runner), holders(folder, runner)
    return [row for sid, row in pending if current.get(sid) == candidates[sid]
            and all(after.get(pid) == table.get(pid)
                    and desktop_owner(pid, after) == desktop_owner(pid, table)
                    for pid in candidates[sid]) and held(folder / (sid + '.lock'))]


def main(argv):
    try:
        for row in discover(argv[0], sys.stdin.read().splitlines(), transcripts='--transcripts' in argv):
            print(row)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        # Preserve usable CLI inventory, but never imply complete discovery.
        print('t: external Codex discovery unavailable: ' + clean(error), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
