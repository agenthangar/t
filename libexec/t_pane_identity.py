"""Codex pane identity: live observations, cached immutable metadata, batch reads.

No cached value establishes ownership or absence. Callers supply current panes;
native writer checks remain in the lifecycle engine. Only the existing cache
service stores artifacts, and its absence never prevents direct validation.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sqlite3
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STORE = runpy.run_path(str(ROOT / 't_codex_store.py'))
CACHE = runpy.run_path(str(ROOT / 't_session_cache.py'))
VERSION = 1
# Python str.strip's Unicode whitespace set (all are at or below U+3000).
WHITESPACE = ''.join(chr(code) for code in range(0x3001) if chr(code).isspace())


def stamp(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('identity metadata is not a regular file')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def database_stamp(path):
    main = stamp(path)
    try:
        wal = stamp(str(path) + '-wal')
    except FileNotFoundError:
        wal = None
    return [main, wal]


class Metadata:
    def __init__(self, database, request=None):
        self.database = str(Path(database).absolute())
        self.request = request or CACHE['request']
        self.budget = .1
        self.key = 'ownership-metadata-v1:' + hashlib.sha256(self.database.encode()).hexdigest()
        self.saved = self.artifact({'key': self.key}).get('value') or {}
        if (not isinstance(self.saved, dict) or self.saved.get('version') != VERSION or
                not isinstance(self.saved.get('rows'), list) or
                not all(isinstance(row, (list, tuple)) and len(row) == 7 for row in self.saved['rows']) or
                not isinstance(self.saved.get('headers'), dict)):
            self.saved = {}
        self.headers = {}
        self.index = None
        self.signature = None
        self.dirty = False

    def artifact(self, payload):
        if self.budget <= 0:
            return {}
        started = time.monotonic()
        try:
            response = self.request('artifact', payload, deadline=started + self.budget)
            return response if isinstance(response, dict) else {}
        except (OSError, ValueError):
            return {}
        finally:
            self.budget -= time.monotonic() - started

    def rows(self, cwd, name):
        if self.index is None:
            before = database_stamp(self.database)
            if self.saved.get('version') == VERSION and self.saved.get('stamp') == before:
                rows = self.saved['rows']
            else:
                # One connection per batch. Prompt bodies are never read or cached.
                rows = STORE['rows'](self.database,
                    "select id,rollout_path,updated_at,archived,"
                    "typeof(first_user_message)='text' and trim(first_user_message,?)<>'',cwd,name "
                    "from threads where instr(source, '\"subagent\"')=0",
                    (WHITESPACE,),
                    initialize_sidecars=False, timeout=.2)
                self.dirty = True
            if before != database_stamp(self.database):
                raise ValueError('identity index changed during inspection')
            self.signature = before
            self.index = {}
            for row in rows:
                self.index.setdefault((row[5], row[6]), []).append(row[:5])
            self.index_rows = rows
        return self.index.get((cwd, name), [])

    def header(self, filename):
        before = stamp(filename)
        old = self.headers.get(filename) or self.saved.get('headers', {}).get(filename, {})
        if (isinstance(old, dict) and old.get('stamp') == before and
                isinstance(old.get('payload'), dict) and self.saved.get('version') == VERSION):
            payload = old['payload']
        else:
            with open(filename) as stream:
                row = json.loads(stream.readline())
            if row.get('type') != 'session_meta' or not isinstance(row.get('payload'), dict):
                raise ValueError('invalid rollout header')
            payload = {key: row['payload'].get(key) for key in ('id', 'cwd', 'source', 'thread_source')}
            self.dirty = True
        if before != stamp(filename):
            raise ValueError('rollout changed during inspection')
        self.headers[filename] = dict(stamp=before, payload=payload)
        return payload

    def stable(self):
        return self.signature is not None and self.signature == database_stamp(self.database)

    def save(self):
        if self.dirty and self.stable():
            self.artifact({'key': self.key, 'value': dict(version=VERSION,
                stamp=self.signature, rows=self.index_rows, headers=self.headers)})


def resolve(metadata, cwd, started, title, pane):
    """Same title/footer/index/rollout proof as the single-pane shell resolver."""
    start = datetime.datetime.strptime(started.strip(), '%a %b %d %H:%M:%S %Y').timestamp()
    name, sep, folder = title.rpartition(' | ')
    if not sep or not name or folder != os.path.basename(cwd):
        return ''
    name = re.sub(r'^(?:[\u2800-\u28ff] |\[ [.!] \] Action Required \| )', '', name)
    footers = [parts for line in pane.splitlines()
               if len(parts := line.strip().split(' · ')) >= 2 and parts[1].startswith(('/', '~/'))]
    if not footers:
        return ''
    footer = footers[-1]
    if len(footer) < 3 or os.path.expanduser(footer[1]) != cwd or footer[2] != name:
        return ''
    rows = metadata.rows(cwd, name)
    if len(rows) != 1:
        return ''  # Namesakes remain ambiguous even when one is older.
    sid, rollout, updated, archived, has_prompt = rows[0]
    if (updated < start or archived or not has_prompt or
            not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sid)):
        return ''
    payload = metadata.header(rollout)
    if (payload.get('id') == sid and payload.get('cwd') == cwd and
            payload.get('source') in ('cli', 'vscode') and payload.get('thread_source') != 'subagent'):
        return sid
    return ''


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=3,
                            env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        raise ValueError('live pane observation failed')
    return result.stdout.rstrip('\n')


def process_starts(entries):
    pids = sorted({str(e['pid']) for e in entries})
    if not pids:
        return {}
    output = command(['ps', '-p', ','.join(pids), '-o', 'pid=,lstart='])
    return dict(line.strip().split(None, 1) for line in output.splitlines() if len(line.split()) >= 6)


def observe(entry):
    target = '=' + entry['session'] + ':'
    args = ['tmux', 'display-message', '-p', '-t', target,
            '#{session_id}|#{pane_id}|#{pane_pid}|#{pane_title}']
    before = command(args)
    fields = before.split('|', 3)
    if len(fields) != 4 or not re.fullmatch(r'\$\d+', fields[0]) or not re.fullmatch(r'%\d+', fields[1]):
        raise ValueError('invalid pane identity')
    pane = command(['tmux', 'capture-pane', '-p', '-t', fields[1]])
    if command(args) != before:
        raise ValueError('pane changed during inspection')
    return fields[3], pane


def batch(entries, metadata):
    starts = process_starts(entries)
    observations = [(entry, *observe(entry)) for entry in entries]
    if process_starts(entries) != starts:
        raise ValueError('identity evidence changed during inspection')
    result = {}
    # Collect live surfaces first. Keep the shared index read/validation window
    # short so an unrelated active thread need not invalidate a long pane walk.
    for entry, title, pane in observations:
        result[entry['session']] = resolve(metadata, entry['cwd'], starts.get(str(entry['pid']), ''), title, pane)
    if metadata.index is not None and not metadata.stable():
        raise ValueError('identity evidence changed during inspection')
    metadata.save()
    return result


def main(argv):
    try:
        mode, database = argv[:2]
        metadata = Metadata(database)
        if mode == 'single':
            sid = resolve(metadata, *argv[2:])
            if sid and metadata.stable():
                print(sid)
            metadata.save()
        elif mode == 'batch':
            entries = []
            for line in sys.stdin:
                session, pid, cwd = line.rstrip('\n').split('\t', 2)
                if not pid.isdecimal() or not cwd.startswith('/'):
                    raise ValueError('invalid ownership input')
                entries.append(dict(session=session, pid=pid, cwd=cwd))
            for session, sid in batch(entries, metadata).items():
                print(session + '\t' + (sid or '-'))
        else:
            return 1
        return 0
    except (OSError, ValueError, TypeError, AttributeError, KeyError, sqlite3.Error, subprocess.TimeoutExpired):
        # No partial batch is published. Shell callers retain the conservative
        # individual resolver when live evidence changes or a peer is older.
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
