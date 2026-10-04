"""Read-only evidence of Codex conversations loaded by the desktop backend.

A reservation, saved preview, thread source, or running app alone is insufficient.
Join open rollout files from a direct desktop backend to the thread index. This
means loaded context (the same meaning as t's CLI checkmark), not a running turn
or a guarantee that a particular window is visible.
"""
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys


def processes(run):
    result = run(['ps', '-Axo', 'pid=,ppid=,comm='], capture_output=True, text=True, timeout=2)
    if result.returncode:
        return {}
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            rows[int(fields[0])] = (int(fields[1]), fields[2])
    return rows


def backends(rows):
    found = set()
    for pid, (parent, executable) in rows.items():
        if os.path.basename(executable) != 'codex' or '.app/Contents/' not in executable:
            continue
        bundle = executable.split('.app/Contents/', 1)[0] + '.app'
        parent_exe = rows.get(parent, (0, ''))[1]
        # Exclude CLI runs, code-mode subprocesses, and nested agent helpers.
        if not parent_exe.startswith(bundle + '/Contents/MacOS/'):
            continue
        try:
            with open(bundle + '/Contents/Info.plist', 'rb') as fh:
                if plistlib.load(fh).get('CFBundleIdentifier') == 'com.openai.codex':
                    found.add(pid)
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    return found


def loaded_threads(home, run=subprocess.run):
    try:
        before = processes(run)
        pids = backends(before)
        if not pids:
            return []
        files = run(['lsof', '-n', '-P', '-p', ','.join(map(str, sorted(pids))), '-Fn'],
                    capture_output=True, text=True, timeout=2)
        if files.returncode:
            return []
        after = processes(run)
        valid = {pid for pid in pids if after.get(pid) == before[pid]
                 and after.get(before[pid][0]) == before.get(before[pid][0])}
        opened, owner = set(), None
        for line in files.stdout.splitlines():
            if line.startswith('p'):
                owner = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith('n') and owner in valid and '/rollout-' in line:
                opened.add(os.path.realpath(line[1:]))
        if not opened:
            return []
        db = sqlite3.connect((Path(home) / 'state_5.sqlite').resolve().as_uri() + '?mode=ro',
                             uri=True, timeout=.2)
        try:
            rows = db.execute("select id, cwd, coalesce(nullif(name,''), title, ''), rollout_path "
                              "from threads where archived=0 and instr(source, '\"subagent\"')=0").fetchall()
        finally:
            db.close()
        return [(sid, cwd, title or '(untitled Codex session)') for sid, cwd, title, rollout in rows
                if isinstance(sid, str) and re.fullmatch(r'[\da-fA-F]{8}(?:-[\da-fA-F]{4}){3}-[\da-fA-F]{12}', sid)
                and isinstance(cwd, str) and os.path.isabs(cwd)
                and isinstance(rollout, str) and os.path.realpath(rollout) in opened]
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired):
        return []


if __name__ == '__main__':
    if sys.platform == 'darwin':
        for row in loaded_threads(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')):
            print('\t'.join(value.replace('\t', ' ').replace('\n', ' ') for value in row))
