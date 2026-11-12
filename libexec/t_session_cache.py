"""The sole owner of discovery cache storage and policy.

Public boundary: request(operation, payload). Clients never open SQLite. One
per-user/source service serves decoded RAM snapshots; disk is recovery only.
Collectors run outside the request loop and cannot publish across an invalidation.
"""
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import signal
import socketserver
import sqlite3
import stat
import subprocess
import sys
import threading
import time

VERSION = 1
TTL = 3600
LIMIT = 64 * 1024 * 1024
SOURCE = Path(__file__).resolve().parents[1]


def _identity():
    return [str(SOURCE), os.path.expanduser('~'),
            os.environ.get('XDG_CACHE_HOME', ''), os.environ.get('XDG_STATE_HOME', ''),
            os.environ.get('XDG_CONFIG_HOME', ''),
            os.environ.get('CODEX_HOME', ''), os.environ.get('CLAUDE_CONFIG_DIR', ''),
            os.environ.get('T_LOCAL_RC', ''), os.environ.get('TMUX_TMPDIR', '')]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError('unsafe session cache directory')
    path.chmod(0o700)
    return path


def _runtime():
    # Short enough for macOS sockaddr_un, even with a long HOME/XDG path.
    base = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    if not os.path.isabs(base) or len(os.fsencode(base)) > 40:
        base = '/tmp'
    return Path(base) / ('t-sessions-' + str(os.getuid()) + '-' + _digest(_identity())[:16])


def _database():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 't/discovery/cache.sqlite3'


def _revision():
    return [int((SOURCE / name).stat().st_mtime_ns) for name in
            ('bin/t', 'libexec/t_session_cache.py', 'libexec/t_session_discovery.py')]


def _wire(operation, payload, deadline=None):
    with socket.socket(socket.AF_UNIX) as connection:
        def budget():
            remaining = 2 if deadline is None else min(2, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError('session cache deadline reached')
            connection.settimeout(remaining)
        budget()
        connection.connect(str(_runtime() / 'service.sock'))
        budget()
        connection.sendall(json.dumps({'operation': operation, 'payload': payload, 'revision': _revision()}).encode() + b'\n')
        raw = bytearray()
        while len(raw) <= LIMIT:
            budget()
            part = connection.recv(min(65536, LIMIT + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
            if b'\n' in part:
                raw = raw.split(b'\n', 1)[0]
                break
    if len(raw) > LIMIT:
        raise ValueError('session cache response too large')
    result = json.loads(raw)
    if result.get('error'):
        raise ValueError(result['error'])
    return result


def request(operation, payload=None, *, deadline=None):
    """Single API for discovery, lifecycle events, and private index artifacts."""
    payload = payload or {}
    _private(_runtime())
    try:
        return _wire(operation, payload) if deadline is None else _wire(operation, payload, deadline)
    except (OSError, ValueError):
        pass
    if deadline is not None:
        # A waiter already contacted the service. Never restart it or extend the
        # caller's deadline when that service disappears during observation.
        return {'hosts': [], 'loading': False, 'unavailable': True}
    if operation == 'artifact':
        return {'value': None}  # Standalone live collectors must not start a daemon.
    _private(_runtime())
    # The daemon holds its own lifetime lock. Multiple requesting processes may
    # spawn candidates; exactly one can bind/restore and all use its RAM.
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, cwd='/', env=env)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            return _wire(operation, payload)
        except (OSError, ValueError):
            time.sleep(.025)
    return {'hosts': [], 'loading': False, 'unavailable': True,
            'message': 'Session discovery unavailable; retry shortly.'}


def invalidate(targets=None):
    """Schedule optional invalidation without waiting for the cache service.

    Never defer a live-row overlay: a delayed launch row could resurrect a slot
    that has since closed or been reused. An invalidation only requests a fresh
    observation, so delivery order cannot invent an owner.
    """
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'invalidate',
                          json.dumps(list(dict.fromkeys(targets or ['local'])))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                         cwd='/', env={k: v for k, v in os.environ.items()
                                       if not k.startswith('COV_CORE_')})
    except OSError:
        pass  # Discovery expiry remains a fallback; never fail an agent launch.


def _collect(spec):
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env['T_DISCOVERY_COLLECTOR'] = '1'
    result = subprocess.run([sys.executable, str(SOURCE / 'bin/t'), '__cache-collect'],
                            input=json.dumps(spec), capture_output=True, text=True,
                            env=env, timeout=120, start_new_session=True)
    if result.returncode:
        raise ValueError(' '.join(result.stderr.split())[:512] or 'session scan failed')
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get('rows'), str) or not isinstance(value.get('resume'), list):
        raise ValueError('invalid session discovery response')
    return value


class _Memory:
    """No storage access: all request handlers operate on these dictionaries."""
    def __init__(self, collect=_collect, clock=time.time, persist=lambda: None, submit=None):
        self.entries, self.generations, self.artifacts = {}, {}, {}
        self.failures, self.reset_epochs = {}, {}
        self.active_targets = set()
        self.waiting = {}
        self.overlays = {}
        self.collect, self.clock, self.persist = collect, clock, persist
        self.submit = submit or self._thread
        self.lock = threading.RLock()
        self.loading = True
        self.persistence_error = ''

    @staticmethod
    def _thread(callback, *args):
        threading.Thread(target=callback, args=args, daemon=True).start()

    def restore(self, saved):
        if not all(isinstance(saved.get(k, {}), dict) for k in ('entries', 'generations', 'artifacts', 'overlays', 'failures')):
            raise ValueError('invalid discovery snapshot')
        with self.lock:
            invalidated = set(self.generations)
            failures = dict(saved.get('failures', {}))
            if 'failures' not in saved:
                # Upgrade old entry-scoped failures even if a lifecycle event
                # invalidated the rows while disk restoration was in progress.
                for entry in saved.get('entries', {}).values():
                    if not isinstance(entry, dict) or not isinstance(entry.get('spec'), dict):
                        continue
                    target, retry = entry['spec'].get('target'), entry.get('retry')
                    if (isinstance(target, str) and isinstance(entry.get('error'), str) and entry['error']
                            and isinstance(retry, (int, float)) and math.isfinite(retry)
                            and retry > failures.get(target, {}).get('retry_at', 0)):
                        failures[target] = {'retry_at': retry, 'error': entry['error']}
            for target, failure in failures.items():
                if (isinstance(target, str) and target and isinstance(failure, dict)
                        and isinstance(failure.get('error'), str) and failure['error']
                        and isinstance(failure.get('retry_at'), (int, float))
                        and math.isfinite(failure['retry_at']) and 0 < failure['retry_at'] <= self.clock() + TTL):
                    self.failures.setdefault(target, dict(failure))
            for key, entry in saved.get('entries', {}).items():
                if not isinstance(entry, dict) or not isinstance(entry.get('spec'), dict):
                    continue
                target = entry['spec'].get('target')
                # A lifecycle event during restoration always wins.
                if target in self.generations or key in self.entries:
                    continue
                if (not isinstance(target, str) or any(not isinstance(entry.get(k), (int, float)) or not math.isfinite(entry[k]) for k in ('checked', 'retry'))
                        or ('value' in entry and not isinstance(entry['value'], dict))):
                    continue
                entry['refreshing'] = False
                if entry['checked'] > self.clock() or entry['retry'] > self.clock() + TTL:
                    entry['checked'] = entry['retry'] = 0
                self.entries[key] = entry
            for target, generation in saved.get('generations', {}).items():
                if isinstance(target, str) and type(generation) is int and generation >= 0:
                    self.generations.setdefault(target, generation)
            self.overlays = {**{k: v for k, v in saved.get('overlays', {}).items() if k not in invalidated and isinstance(v, dict)}, **self.overlays}
            self.artifacts = {**saved.get('artifacts', {}), **self.artifacts}
            self.loading = False
            waiting = list(self.waiting.values())
            self.waiting.clear()
        if waiting:
            self.handle('discover', {'hosts': waiting, 'fields': []})

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(entries=self.entries, generations=self.generations, artifacts=self.artifacts,
                                      overlays=self.overlays, failures=self.failures))

    def handle(self, operation, payload):
        with self.lock:
            if operation == 'status':
                return dict(loading=self.loading, persistence_error=self.persistence_error, protocol=VERSION)
            if operation == 'artifact':
                key = str(payload['key'])
                if 'value' in payload:
                    self.artifacts[key] = payload['value']
                    self.persist()
                return {'value': copy.deepcopy(self.artifacts.get(key))}
            if operation == 'reset-host':
                target = payload.get('target')
                if not isinstance(target, str) or not target:
                    raise ValueError('invalid session discovery host')
                if self.loading:
                    raise ValueError('session cache is still loading; retry reset')
                self.failures.pop(target, None)
                self.generations[target] = self.generations.get(target, 0) + 1
                self.reset_epochs[target] = self.reset_epochs.get(target, 0) + 1
                for entry in self.entries.values():
                    if entry['spec']['target'] == target:
                        entry['checked'] = entry['retry'] = 0
                        entry['error'] = ''
                self.persist()
                return {'reset': target}
            if operation == 'forget':
                target = payload.get('target', 'local')
                workspace, sid = payload.get('workspace'), payload.get('sid', '')
                if not isinstance(target, str) or not target or not isinstance(workspace, str) or not os.path.isabs(workspace) or not isinstance(sid, str):
                    raise ValueError('invalid session cache reset target')
                if self.loading:
                    raise ValueError('session cache is still loading; retry reset')
                def matches(row, resume=False):
                    if isinstance(row, str):
                        fields = row.split('\t')
                        if resume:
                            return len(fields) >= 5 and (bool(sid and fields[3] == sid) or fields[4] == workspace)
                        return len(fields) >= 2 and (bool(sid and fields[0] == sid) or fields[1] == workspace)
                    if not isinstance(row, dict):
                        return False
                    return bool(sid and row.get('sid', row.get('id')) == sid) or row.get('cwd') == workspace or any(
                        a.get('workspace') == workspace for a in row.get('associations', []) if isinstance(a, dict))
                count = 0
                self.generations[target] = self.generations.get(target, 0) + 1
                for entry in self.entries.values():
                    if entry['spec']['target'] != target:
                        continue
                    entry['checked'] = entry['retry'] = 0
                    value = entry.get('value', {})
                    rows = value.get('rows', '').splitlines()
                    kept = [row for row in rows if not matches(row)]
                    count += len(rows) - len(kept)
                    value['rows'] = '\n'.join(kept) + ('\n' if kept else '')
                    rows = value.get('resume', [])
                    value['resume'] = [row for row in rows if not matches(row, resume=True)]
                    count += len(rows) - len(value['resume'])
                    catalog = value.get('catalog', {})
                    if 'conversations' in catalog:
                        catalog['conversations'] = [r for r in catalog['conversations'] if not matches(r)]
                    mcp = value.get('mcp', {})
                    if 'recs' in mcp:
                        mcp['recs'] = [r for r in mcp['recs'] if not matches(r)]
                    for field in ('live', 'registry'):
                        if isinstance(mcp.get(field), dict):
                            mcp[field] = {key: row for key, row in mcp[field].items()
                                          if not (sid and key == sid) and not matches(row)}
                self.overlays[target] = {key: value for key, value in self.overlays.get(target, {}).items()
                                         if not matches(value['row'])}
                self.persist()
                return {'forgotten': count}
            if operation == 'event':
                targets = payload.get('targets', ['local'])
                for target in targets:
                    self.generations[target] = self.generations.get(target, 0) + 1
                    for entry in self.entries.values():
                        if entry['spec']['target'] == target:
                            entry['checked'] = entry['retry'] = 0
                            # Keep the in-flight marker until its worker exits:
                            # no duplicate collector while old generation drains.
                    row = payload.get('row', '')
                    if target == 'local' and len(row.split('\t')) >= 7:
                        self.overlays.setdefault(target, {})[row.split('\t')[2]] = {
                            'row': row, 'close': payload.get('event') == 'close'}
                    for entry in self.entries.values():
                        if entry['spec']['target'] == target:
                            self._overlay(entry)
                self.persist()
                return {'invalidated': targets}
            if operation != 'discover':
                raise ValueError('unknown session cache operation')
            hosts = []
            for spec in payload.get('hosts', []):
                if not isinstance(spec, dict) or not isinstance(spec.get('target'), str):
                    raise ValueError('invalid discovery host')
                # Friendly aliases do not create a second host cache.
                key = _digest({k: v for k, v in spec.items() if k != 'host'})
                if self.loading:
                    self.waiting[key] = spec
                entry = self.entries.get(key)
                if entry is None and not self.loading:
                    entry = self.entries.setdefault(key, dict(spec=spec, checked=0, retry=0, refreshing=False))
                if entry is not None:
                    self._overlay(entry)
                    now = self.clock()
                    failure = self.failures.get(spec['target'], {})
                    retry = max(entry['retry'], failure.get('retry_at', 0))
                    stale = not entry.get('checked') or now - entry['checked'] >= TTL or now < entry['checked']
                    if stale and not self.loading and not entry['refreshing'] and spec['target'] not in self.active_targets and now >= retry:
                        entry['refreshing'] = True
                        self.active_targets.add(spec['target'])
                        generation = self.generations.get(spec['target'], 0)
                        self.submit(self.refresh, key, generation, self.reset_epochs.get(spec['target'], 0))
                    hosts.append(dict(host=spec.get('host', spec['target']), target=spec['target'],
                                      checked=entry['checked'], stale=stale,
                                      refreshing=entry['refreshing'] or (stale and now >= retry
                                          and spec['target'] in self.active_targets),
                                      error=failure.get('error', entry.get('error', '')), retry_at=retry,
                                      value=copy.deepcopy({k: v for k, v in entry['value'].items()
                                                           if k in payload.get('fields', entry['value'])}) if 'value' in entry else None))
                else:
                    hosts.append(dict(host=spec.get('host', spec['target']), target=spec['target'],
                                      checked=0, stale=True, refreshing=False, error='', retry_at=0, value=None))
            return dict(hosts=hosts, loading=self.loading, persistence_error=self.persistence_error)

    def _overlay(self, entry):
        changes = self.overlays.get(entry['spec']['target'], {})
        if not changes:
            return
        value = entry.setdefault('value', {})
        rows = [r for r in value.get('rows', '').splitlines()
                if len(r.split('\t')) >= 3 and r.split('\t')[2] not in changes]
        rows += [change['row'] for change in changes.values() if not change['close']]
        value['rows'] = '\n'.join(rows) + ('\n' if rows else '')

    def refresh(self, key, generation, reset_epoch=None):
        spec = self.entries[key]['spec']
        target = spec['target']
        if reset_epoch is None:
            reset_epoch = self.reset_epochs.get(target, 0)
        try:
            value, error = self.collect(spec), ''
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            value, error = None, ' '.join(str(exc).split())[:512] or 'session scan failed'
        with self.lock:
            entry = self.entries[key]
            entry['refreshing'] = False
            self.active_targets.discard(target)
            if reset_epoch != self.reset_epochs.get(target, 0):
                return
            # Ordinary row invalidation must not erase evidence that a host is
            # unreachable. Only an explicit reset can supersede this failure.
            if error:
                self.failures[target] = {'retry_at': self.clock() + TTL, 'error': error}
                self.persist()
            if generation != self.generations.get(target, 0):
                return
            if not error:
                self.failures.pop(target, None)
                for sibling in self.entries.values():
                    if sibling['spec']['target'] == target:
                        sibling['error'] = ''
                        sibling['retry'] = 0
            entry['error'] = error
            entry['retry'] = self.clock() + TTL
            if value is not None:
                entry['value'], entry['checked'] = value, self.clock()
                self.overlays.pop(spec['target'], None)
            self.persist()


def _legacy_artifacts():
    root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'claude-sessions'
    result, remaining = {}, LIMIT
    for path in list((root / 'index').glob('*')) + list(root.glob('candidates-*.json')):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > remaining:
                continue
            data = path.read_bytes()
            remaining -= len(data)
            value = json.loads(data)
            if isinstance(value, dict):
                result[str(path)] = value
        except (OSError, ValueError):
            continue
    return result


class _Persistence:
    """One background thread loads once, then serializes RAM snapshots to disk."""
    def __init__(self, memory, database=None):
        self.memory = memory
        self.database = Path(database or _database())
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.namespace = _digest(_identity())

    def _open(self):
        _private(self.database.parent)
        if self.database.is_symlink():
            raise OSError('unsafe session cache database')
        fd = os.open(self.database, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.database, timeout=.1)
        db.execute('pragma journal_mode=WAL')
        db.execute('create table if not exists snapshots (namespace text primary key, version integer, payload text)')
        return db

    def run(self):
        db = None
        try:
            db = self._open()
            row = db.execute('select payload from snapshots where namespace=? and version=?', (self.namespace, VERSION)).fetchone()
            saved = json.loads(row[0]) if row else {'artifacts': _legacy_artifacts()}
            if not isinstance(saved, dict):
                raise ValueError('invalid cache snapshot')
            self.memory.restore(saved)
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as error:
            self.memory.persistence_error = str(error)
            self.memory.restore({})
        while True:
            self.wake.wait(.5)
            if self.stop.is_set() and not self.wake.is_set():
                break
            if not self.wake.is_set():
                continue
            self.wake.clear()
            self.stop.wait(.1)
            try:
                if db is None:
                    db = self._open()
                value = json.dumps(self.memory.snapshot())
                db.execute('insert or replace into snapshots values (?,?,?)', (self.namespace, VERSION, value))
                db.commit()
                self.memory.persistence_error = ''
            except (OSError, sqlite3.Error, ValueError) as error:
                self.memory.persistence_error = str(error)
            if self.stop.is_set():
                break
        if db is not None:
            db.close()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        try:
            raw = self.rfile.readline(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError('session cache request too large')
            message = json.loads(raw)
            if message.get('revision') != getattr(self.server, 'revision', _revision()):
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                raise ValueError('session cache service version changed; restarting')
            result = self.server.memory.handle(message['operation'], message.get('payload') or {})
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            result = {'error': str(error)}
        try:
            self.wfile.write(json.dumps(result).encode() + b'\n')
        except OSError:
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def _serve():
    os.umask(0o077)
    root = _private(_runtime())
    fd = os.open(root / 'service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(.05)
        address = root / 'service.sock'
        address.unlink(missing_ok=True)
        memory = _Memory()
        disk = _Persistence(memory)
        memory.persist = disk.wake.set
        writer = threading.Thread(target=disk.run, daemon=True)
        with _Server(str(address), _Handler) as server:
            address.chmod(0o600)
            server.memory = memory
            server.revision = _revision()
            def stop(*_args):
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            writer.start()
            try:
                server.serve_forever(poll_interval=.25)
            finally:
                disk.stop.set()
                disk.wake.set()
                writer.join(2)
                address.unlink(missing_ok=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['serve']:
        _serve()
    elif len(sys.argv) == 3 and sys.argv[1] == 'invalidate':
        request('event', {'targets': json.loads(sys.argv[2])})
