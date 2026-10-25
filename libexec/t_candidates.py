"""Disposable, change-validated Codex discovery shared by session pickers.

Only saved metadata is cached. Callers must still check live ownership before
acting. A resume invocation takes one snapshot for all of its worktrees.
"""
import glob
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

VERSION = 1


def stamp(path):
    try:
        st = os.stat(path)
        return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
    except OSError:
        return None


def load(path):
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get('version') == VERSION:
            return data
    except (OSError, ValueError):
        pass
    return {}


def save(path, data):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
            temporary = file.name
            json.dump(data, file)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def rollout_metadata(path):
    try:
        with open(path, 'rb') as file:
            record = json.loads(file.readline(65536))
        payload = record.get('payload') or {}
        if record.get('type') == 'session_meta':
            cwd = payload.get('cwd')
            source = payload.get('source')
            sub = payload.get('thread_source') == 'subagent' or isinstance(source, dict) and 'subagent' in source
            if isinstance(cwd, str):
                return [cwd, bool(sub)]
    except (OSError, ValueError, AttributeError):
        pass
    return ['', False]


def index_rows(db):
    """Failure is retryable; never persist an unavailable database as empty."""
    try:
        with sqlite3.connect(Path(db).as_uri() + '?mode=ro', uri=True, timeout=0.5) as connection:
            rows = connection.execute(
                "select cwd, rollout_path from threads where archived=0 "
                "and instr(source, '\"subagent\"')=0 order by updated_at desc").fetchall()
            return [list(row) for row in rows if all(isinstance(value, str) for value in row)]
    except sqlite3.Error:
        return None


def valid_rows(rows):
    return isinstance(rows, list) and all(
        isinstance(row, list) and len(row) == 2 and all(isinstance(x, str) for x in row)
        for row in rows)


def discover(home, cache_root, *, rollouts_only=False):
    home = Path(home).absolute()
    key = hashlib.sha256(os.fsencode(home)).hexdigest()[:20]
    cache = Path(cache_root) / 'claude-sessions' / ('candidates-' + key + '.json')
    old = load(cache)
    known = old.get('rollouts', {})
    if not isinstance(known, dict):
        known = {}
    entries, rollouts = {}, []
    # Stat every current path: directory mtimes alone miss appends and in-place
    # rewrites. Enumerating also detects additions, removals, and moved rollouts.
    for path in glob.glob(str(home / 'sessions' / '*' / '*' / '*' / 'rollout-*.jsonl')):
        signature = stamp(path)
        if signature is None:
            continue
        entry = known.get(path)
        if not (isinstance(entry, list) and len(entry) == 3 and entry[0] == signature
                and isinstance(entry[1], str) and isinstance(entry[2], bool)):
            entry = [signature, *rollout_metadata(path)]
            # A concurrent writer gets another read next time, not a cached race.
            if stamp(path) != signature:
                continue
        entries[path] = entry
        if entry[1] and not entry[2]:
            rollouts.append((signature[3], entry[1], path))
    data = {'version': VERSION, 'rollouts': entries}
    rows = []
    if not rollouts_only:
        db = str(home / 'state_5.sqlite')
        signature = [stamp(db), stamp(db + '-wal')]
        indexed = old.get('index', {})
        if (isinstance(indexed, dict) and indexed.get('stamp') == signature
                and valid_rows(indexed.get('rows'))):
            rows = indexed['rows']
        else:
            rows = index_rows(db) if signature[0] is not None else []
        if rows is not None and [stamp(db), stamp(db + '-wal')] == signature:
            data['index'] = {'stamp': signature, 'rows': rows}
        rows = rows or []
    elif isinstance(old.get('index'), dict):
        data['index'] = old['index']
    if data != old:
        save(cache, data)
    seen, result = set(), []
    # The index follows resume cwd; preserve the existing union with original
    # rollout cwd, and its index-first ordering, independently for each cwd.
    for cwd, path in [*rows, *((cwd, path) for _, cwd, path in sorted(rollouts, reverse=True))]:
        if (cwd, path) not in seen and os.path.isfile(path):
            seen.add((cwd, path))
            result.append((cwd, path))
    return result


def main():
    mode, *args = sys.argv[1:]
    home = os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')
    cache = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    for cwd, path in discover(home, cache, rollouts_only=mode == 'rollouts'):
        if args and cwd != args[0]:
            continue
        # The shell protocol is line/tab delimited, like _codex_threads.
        if any(c in cwd + path for c in '\n\r\t'):
            continue
        print(path if args else cwd + '\t' + path)


if __name__ == '__main__':
    main()
