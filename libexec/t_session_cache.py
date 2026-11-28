"""The sole owner of discovery cache storage and policy.

Public boundary: request(operation, payload). Clients never open SQLite. One
per-user/source service serves decoded RAM snapshots; disk is recovery only.
Collectors run outside the request loop and cannot publish across an invalidation.
"""
import copy
import fcntl
import hashlib
import itertools
import json
import math
import os
import runpy
import re
import tempfile
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
import traceback

VERSION = 2
TTL = 86400
FAILURE_TTL = 3600
ACTIVE_TTL = 3600
LOCAL_INTERVAL = 5
REMOTE_INTERVAL = 30
LIMIT = 64 * 1024 * 1024
SOURCE = Path(__file__).resolve().parents[1]
_local_rc = runpy.run_path(str(SOURCE / 'libexec/t_config_path.py'))['local_rc']


def _homebrew(source=None):
    """Validated rack and stable opt identity; never trust a marker in a worktree."""
    source = Path(source or SOURCE)
    try:
        if source != source.resolve() or (source / '.git').exists():
            return None
        if source.name != 'libexec' or source.parent.parent.name != 't' or source.parent.parent.parent.name != 'Cellar':
            return None
        for name in ('.t-homebrew', '.t-release-version'):
            info = (source / name).lstat()
            if not stat.S_ISREG(info.st_mode):
                return None
        marker = json.loads((source / '.t-homebrew').read_text())
        opt = marker.get('opt_libexec', '')
        if (marker.get('formula') != 'agenthangar/tap/t' or not isinstance(opt, str)
                or not os.path.isabs(opt) or os.path.normpath(opt) != opt):
            return None
        active = Path(opt).resolve()
        rack = source.parent.parent
        if active.name != 'libexec' or active.parent.parent != rack:
            return None
        return dict(opt=opt, rack=rack, source=source)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _brew_process_sources(brew):
    """Read bounded process evidence, never signal a helper or inspect its env."""
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(['ps', '-ww', '-u', str(os.getuid()), '-o', 'command='],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=.2)
            if result.returncode or output.tell() > 262144:
                return []
            output.seek(0)
            lines = output.read(262144).decode(errors='replace').splitlines()
        # ps does not preserve argv quoting. Only unambiguous Python invocations
        # with a whitespace-free absolute script path can recover removed kegs.
        pattern = re.compile(r'^(?:/\S*/)?(?:python[0-9.]*|Python) (' + re.escape(str(brew['rack'])) +
                             r'/[^/\s]+/libexec)/libexec/t_session_cache\.py serve$')
        return [Path(match.group(1)) for line in lines
                if (match := pattern.fullmatch(line.strip()))][:64]
    except (OSError, subprocess.TimeoutExpired):
        return []


def _brew_sources(brew):
    """Physical namespaces belonging to this rack, including pruned live kegs."""
    sources = [brew['source']]
    try:
        # Version tuples give numerical newest-first order, independent of mtime.
        kegs = list(itertools.islice(brew['rack'].iterdir(), 128))
        kegs.sort(key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p.name)), reverse=True)
        for keg in kegs:
            candidate = keg / 'libexec'
            marker = _homebrew(candidate)
            if marker and marker['opt'] == brew['opt'] and candidate not in sources:
                sources.append(candidate)
    except OSError:
        pass
    for candidate in _brew_process_sources(brew):
        if candidate in sources or candidate.parent.parent != brew['rack']:
            continue
        if candidate.exists():
            marker = _homebrew(candidate)
            if not marker or marker['opt'] != brew['opt']:
                continue
        elif candidate.parent.exists() or candidate.parent.is_symlink():
            # A partial/untrusted keg is not evidence of a removed package.
            continue
        sources.append(candidate)
    previous = sorted(sources[1:], key=lambda p: (
        tuple(int(n) for n in re.findall(r'\d+', p.parent.name)), str(p)), reverse=True)
    return [sources[0], *previous][:128]


def _snapshot_identities():
    yield from _compatible_identities()
    brew = _homebrew()
    if brew:
        for source in _brew_sources(brew):
            yield from _compatible_identities(source)
    canonical = _canonical_source()
    if canonical is not None:
        yield from _compatible_identities(canonical)


_LIVE = runpy.run_path(str(SOURCE / 'libexec/t_live_inventory.py'))


def _identity():
    brew = _homebrew()
    return [brew['opt'] if brew else str(SOURCE), os.path.expanduser('~'),
            os.environ.get('XDG_CACHE_HOME', ''), os.environ.get('XDG_STATE_HOME', ''),
            os.environ.get('XDG_CONFIG_HOME', ''),
            os.environ.get('CODEX_HOME', ''), os.environ.get('CLAUDE_CONFIG_DIR', ''),
            os.path.realpath(_local_rc()), os.environ.get('TMUX_TMPDIR', ''), _LIVE['socket_path']()]


def _compatible_identities(source=None):
    """Known old raw-RC namespaces, only when they resolve to this config."""
    identity = _identity()
    if source is not None:
        identity[0] = str(source)
    yield identity
    effective = identity[7]
    # Empty and relative old keys omitted effective selection/caller cwd and
    # cannot prove isolation after a bridge change. Recover only absolute keys.
    candidates = [os.environ.get('T_LOCAL_RC', ''), _local_rc(), _local_rc(explicit=False)]
    for candidate in dict.fromkeys(candidates):
        expanded = os.path.expanduser(candidate)
        if not os.path.isabs(expanded) or os.path.realpath(expanded) != effective:
            continue
        legacy = list(identity)
        legacy[7] = candidate
        if legacy != identity:
            yield legacy


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
    return _identity_runtime(_identity())


def _identity_runtime(identity):
    # Short enough for macOS sockaddr_un, even with a long HOME/XDG path.
    base = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    if not os.path.isabs(base) or len(os.fsencode(base)) > 40:
        base = '/tmp'
    return Path(base) / ('t-sessions-' + str(os.getuid()) + '-' + _digest(identity)[:16])


def linked_source(link):
    """Snapshot an installed executable's source before replacing its link."""
    link = Path(link)
    if not link.is_symlink():
        return ''
    target = link.resolve()
    return str(target.parents[1]) if target.name == 't' and target.parent.name == 'bin' else ''


def _retirement_identities(source):
    for identity in _compatible_identities(source):
        yield identity
        yield identity[:-1]  # Retire pre-socket listeners without adopting their rows.
    identity = _identity()
    # Unlike snapshot adoption, retiring an obsolete listener needs no cache
    # data. An empty old RC key belongs to the implicit configuration; include
    # it only when that is still the selected configuration.
    if identity[7] == os.path.realpath(_local_rc(explicit=False)):
        identity[0], identity[7] = str(source), ''
        yield identity
        yield identity[:-1]


def _same_installation(previous):
    """Recognize exact sources and package relocations, never Git worktrees."""
    previous = Path(previous)
    if (SOURCE / '.git').is_file() or (previous / '.git').is_file():
        return False
    if previous == SOURCE:
        return True
    try:
        # Homebrew moves the physical source while retaining the opt identity.
        old = json.loads((previous / '.t-homebrew').read_text())
        new = json.loads((SOURCE / '.t-homebrew').read_text())
        if (old.get('formula') == new.get('formula') == 'agenthangar/tap/t'
                and old.get('opt_libexec') == new.get('opt_libexec')
                and os.path.isabs(new.get('opt_libexec', ''))):
            return True
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # Release installer destinations are versioned siblings in one release store.
    return (previous.parent == SOURCE.parent and SOURCE.parent.name == 'releases'
            and (previous / '.t-release-version').is_file()
            and (SOURCE / '.t-release-version').is_file())


def _existing_status(root, revision, deadline):
    """Handshake only with an existing private endpoint; never start or unlink."""
    directory = root.lstat()
    address = root / 'service.sock'
    endpoint = address.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077 or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
        return
    with socket.socket(socket.AF_UNIX) as connection:
        def budget():
            remaining = min(.1, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError('session cache retirement deadline reached')
            connection.settimeout(remaining)
        budget()
        connection.connect(str(address))
        budget()
        connection.sendall(json.dumps(dict(operation='status', payload={}, revision=revision)).encode() + b'\n')
        budget()
        # Both status and the expected revision-mismatch response are tiny. No
        # parsing, retries or service startup are needed to request retirement.
        value = json.loads(connection.recv(4096))
        if not isinstance(value, dict):
            raise ValueError("invalid discovery status response")
        return value


def retire_obsolete(previous_sources=(), report=False):
    """Best-effort update reconciliation, with a half-second total I/O budget.

    A current normalized endpoint receives its actual revision; obsolete raw-RC
    namespaces and previous package sources receive a guaranteed mismatch. Old
    identities that cannot prove the same effective config are left alone.
    """
    evidence = []
    pending = []
    started = time.monotonic()
    try:
        if (SOURCE / '.git').is_file():
            return dict(status='deferred', reason='development discovery is pinned', results=[]) if report else None
        revision = _revision()
        current = _identity()
        brew = _homebrew()
        sources = ([Path(brew['opt']), *_brew_sources(brew)] if brew else [SOURCE])
        # Callers snapshot only the validated source they are updating. Its
        # files may already have been removed by the package manager.
        for source in previous_sources:
            previous = Path(source).resolve()
            if previous not in sources and not (previous / '.git').is_file():
                sources.append(previous)
        deadline = time.monotonic() + .5
        seen = set()
        for source in sources:
            for identity in _retirement_identities(source):
                root = _identity_runtime(identity)
                if root in seen:
                    continue
                seen.add(root)
                if time.monotonic() >= deadline:
                    if report and _socket_identity(root / 'service.sock') is not None:
                        evidence.append(dict(status='failed', reason='discovery retirement deadline reached'))
                    continue
                try:
                    address = root / 'service.sock'
                    before = _socket_identity(address)
                    if before is None and report:
                        continue
                    response = _existing_status(root, revision if identity == current else [], deadline)
                    if response is None:
                        evidence.append(dict(status='deferred', reason='foreign or unsupported discovery endpoint'))
                    elif response.get('protocol') == VERSION and identity == current:
                        evidence.append(dict(status='current'))
                    elif response.get('error') == 'session cache service version changed; restarting':
                        if report:
                            pending.append((address, before))
                    else:
                        evidence.append(dict(status='failed', reason='unverified discovery retirement response'))
                except (OSError, ValueError) as error:
                    if report:
                        evidence.append(dict(status='failed', reason=str(error)))
    except (OSError, ValueError, TypeError) as error:
        if report:
            evidence.append(dict(status='failed', reason=str(error)))
    if report:
        for address, before in pending:
            while _socket_identity(address) == before and time.monotonic() < started + 1.5:
                time.sleep(.02)
            evidence.append(dict(status='updated' if _socket_identity(address) != before else 'failed',
                                 reason='obsolete listener retirement'))
        status = next((value for value in ('failed', 'deferred', 'updated')
                       if any(item['status'] == value for item in evidence)), 'current')
        return dict(status=status, reason='; '.join(item.get('reason', '') for item in evidence
                    if item.get('reason')), results=evidence)


def _database():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 't/discovery/cache.sqlite3'


def _canonical_source():
    marker = SOURCE / '.git'
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        text = marker.read_text().strip()
        if not text.startswith('gitdir: '):
            return None
        gitdir = (SOURCE / text[8:]).resolve()
        if gitdir.parent.name != 'worktrees' or gitdir.parent.parent.name != '.git':
            return None
        canonical = gitdir.parent.parent.parent
        if (canonical / 'libexec/t_session_cache.py').is_file():
            return canonical
    except OSError:
        pass
    return None


def _revision():
    return [int((SOURCE / name).stat().st_mtime_ns) for name in
            ('bin/t', 'libexec/t_session_cache.py', 'libexec/t_session_discovery.py', 'libexec/t_history.py', 'libexec/t_config_path.py', 'libexec/t_live_inventory.py')]


class DiscoveryError(ValueError):
    def __init__(self, message, kind='collector', diagnostic=''):
        super().__init__(message)
        self.kind, self.diagnostic = kind, diagnostic


def _diagnostic(spec, detail):
    """Private bounded evidence, never transcript bodies or command stdout."""
    root = _private(_database().parent / 'diagnostics')
    path = root / (_digest([_identity(), spec.get('target')])[:24] + '.log')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(detail[-65536:])
    return str(path)



def _report_path(stage):
    return _database().parent / 'diagnostics' / (_digest([_identity(), 'local'])[:24] + '-' + stage + '.json')


def _report(stage, status):
    # Discovery evidence must never cause the collection itself to fail.
    temporary = None
    try:
        path = _report_path(stage)
        _private(path.parent)
        if status.get('error'):
            clean = runpy.run_path(str(SOURCE / 'libexec/t_lifecycle.py'))['diagnostic_error']
            status['error'] = clean(status['error'], 400)
            status['last_failure'] = dict(at=status['attempt'], error=status['error'])
        elif path.is_file() and not path.is_symlink():
            try:
                with path.open() as previous:
                    status['last_failure'] = json.loads(previous.read(16384)).get('last_failure')
            except (OSError, AttributeError, ValueError):
                pass
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(status, stream)
        temporary.replace(path)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def doctor_inventory(count, actual_socket):
    helper = runpy.run_path(str(SOURCE / 'libexec/t_lifecycle.py'))
    lines = [f'Tmux now       {count if count is not None else "unknown"} numbered slots on {actual_socket or "unknown socket"}']
    for stage in ('inventory', 'enrichment'):
        try:
            raw, _ = helper['diagnostic_read'](_report_path(stage), 16384)
            record = json.loads(raw)
            age = max(0, int(time.time() - record['attempt']))
            lines.append(f'{stage.capitalize():14} {age}s ago, took {record["duration"]}s')
            if stage == 'inventory':
                lines.append(f'Collector      {record["count"] if record["count"] is not None else "unknown"} numbered slots on {record["socket"]}')
                if actual_socket and os.path.realpath(actual_socket) != os.path.realpath(record['socket']):
                    lines.append('Observation    collector used a different socket')
                elif count is not None and record.get('count') is not None and count != record['count']:
                    lines.append('Observation    numbered-slot count changed since the scan')
                if record.get('checked'):
                    lines.append(f'Live snapshot  {max(0, int(time.time() - record["checked"]))}s old')
            if record.get('error'):
                lines.append('Failure        ' + helper['diagnostic_error'](record['error'], 400))
            elif record.get('last_failure'):
                failure = record['last_failure']
                age = max(0, int(time.time() - failure['at']))
                lines.append(f'Last failure   {age}s ago, ' + helper['diagnostic_error'](failure['error'], 400))
            if record.get('retry_at', 0) > time.time():
                lines.append(f'Retry          in {math.ceil(record["retry_at"] - time.time())}s')
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            lines.append(f'{stage.capitalize():14} no readable observation')
    return lines


def _service_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    # Detached workers start at /. Pin selection before changing cwd, including
    # implicit bridge/legacy paths and relative or symlinked explicit overrides.
    env['T_LOCAL_RC'] = os.path.abspath(_local_rc())
    return env


def _start_candidate(*, ready=False):
    # Elect a starter across processes. A slow/unreachable owner must not cause
    # every waiter to fork a replacement on every poll.
    fd = os.open(_runtime() / 'start.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if ready:
            lock.seek(0)
            lock.truncate()
            return
        try:
            last = float(lock.read() or 0)
        except ValueError:
            last = 0
        now = time.monotonic()
        if last and 0 <= now - last < 4:
            return
        env = _service_env()
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, cwd='/', env=env)
        lock.seek(0)
        lock.truncate()
        lock.write(str(now))


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
    if operation == 'discover' and payload.get('observe') is True:
        return {'hosts': [], 'loading': False, 'unavailable': True}
    if operation == 'artifact':
        return {'value': None}  # Standalone live collectors must not start a daemon.
    # The daemon holds its own lifetime lock. Multiple requesting processes may
    # spawn candidates; exactly one can bind/restore and all use its RAM.
    if deadline is not None and time.monotonic() >= deadline:
        return {'hosts': [], 'loading': False, 'unavailable': True}
    _start_candidate()
    # An old owner may need two seconds to flush before releasing its lock.
    # One candidate waits for that handoff; do not fork repeatedly on each poll.
    deadline = deadline if deadline is not None else time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            result = _wire(operation, payload, deadline)
            _start_candidate(ready=True)
            return result
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
                         cwd='/', env=_service_env())
    except OSError:
        pass  # Discovery expiry remains a fallback; never fail an agent launch.


def _collect(spec):
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env['T_DISCOVERY_COLLECTOR'] = '1'
    result = subprocess.run([sys.executable, str(SOURCE / 'bin/t'), '__cache-collect'],
                            input=json.dumps(spec), capture_output=True, text=True,
                            env=env, timeout=120, start_new_session=True)
    if result.returncode:
        detail = result.stderr or 'session scan failed'
        kind = 'collector'
        try:
            failure = json.loads(detail.splitlines()[-1])
            kind, summary = failure['kind'], failure['message']
            detail = failure.get('detail', detail)
        except (ValueError, KeyError, TypeError):
            summary = detail.splitlines()[-1] if 'Traceback' in detail else detail
        path = _diagnostic(spec, detail)
        raise DiscoveryError(' '.join(summary.split())[:512], kind, path)
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get('_pending'):
        return value
    if not isinstance(value, dict) or not isinstance(value.get('rows'), str) or not isinstance(value.get('resume'), list):
        raise ValueError('invalid session discovery response')
    return value


class _Memory:
    """No storage access: all request handlers operate on these dictionaries."""
    def __init__(self, collect=_collect, clock=time.time, persist=lambda: None, submit=None):
        self.entries, self.generations, self.artifacts = {}, {}, {}
        self.failures, self.reset_epochs = {}, {}
        self.active_targets = set()
        self.instance = os.urandom(12).hex()
        self.waiting = {}
        self.overlays = {}
        self.export_versions = {}
        self.inventory = None
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
                        and math.isfinite(failure['retry_at']) and 0 < failure['retry_at'] <= self.clock() + FAILURE_TTL):
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
                entry.pop('refresh_revision', None)
                entry.pop('peer_ticket', None)
                entry['pending'] = False
                entry.pop('last_used', None)
                entry.pop('requested', None)
                entry.pop('force_refresh', None)
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
        for payload in waiting:
            self.handle('discover', payload)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(entries=self.entries, generations=self.generations, artifacts=self.artifacts,
                                      overlays=self.overlays, failures=self.failures))

    def handle(self, operation, payload):
        with self.lock:
            if operation == 'live-view':
                host = dict(value=None)
                if self.inventory:
                    self.inventory.request()
                    self.inventory.project(host)
                return host
            if operation == 'catalog-changed':
                for entry in self.entries.values():
                    if entry['spec']['target'] == 'local':
                        entry['dirty'] = True
                        if payload.get('refresh_active') and self.clock() - entry.get('last_used', 0) < ACTIVE_TTL:
                            entry['requested'] = True
                            entry['next_poll'] = 0
                        if entry['refreshing']:
                            entry['catalog_changed'] = True
                return {'scheduled': True}
            if operation == 'status':
                return dict(loading=self.loading, persistence_error=self.persistence_error, protocol=VERSION)
            if operation == 'artifact':
                key = str(payload['key'])
                if 'value' in payload:
                    self.artifacts[key] = payload['value']
                    self.persist()
                return {'value': copy.deepcopy(self.artifacts.get(key))}
            if operation == 'export':
                return self.export(payload)
            if operation == 'activity':
                if 'local' in payload.get('targets', ['local']):
                    self.generations['local'] = self.generations.get('local', 0) + 1
                for entry in self.entries.values():
                    if entry['spec']['target'] in payload.get('targets', ['local']):
                        entry['next_poll'] = 0
                        if (entry['spec']['target'] == 'local' and 'last_used' in entry
                                and self.clock() - entry['last_used'] < ACTIVE_TTL):
                            entry['requested'] = True
                        if entry['spec']['target'] != 'local':
                            entry['last_used'] = self.clock()
                return {'scheduled': True}
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
                        if target == 'local':
                            entry['requested'] = True
                        entry['next_poll'] = 0
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
                    entry['next_poll'] = 0
                    if target == 'local':
                        entry['requested'] = True
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
                            entry['dirty'] = True
                            entry['next_poll'] = 0
                            if target == 'local' and entry['refreshing']:
                                entry['requested'] = True
                            # Keep the in-flight marker until its worker exits:
                            # no duplicate collector while old generation drains.
                    row = payload.get('row', '')
                    if target == 'local' and len(row.split('\t')) >= 7:
                        self.overlays.setdefault(target, {})[row.split('\t')[2]] = {
                            'row': row, 'close': payload.get('event') == 'close'}
                    for entry in self.entries.values():
                        if entry['spec']['target'] == target:
                            self._overlay(entry)
                if self.inventory and 'local' in targets and len(payload.get('row', '').split('\t')) >= 7:
                    if payload.get('event') == 'close':
                        self.inventory.close(payload['row'].split('\t')[2])
                    else:
                        self.inventory.request(changed=True)
                self.persist()
                return {'invalidated': targets}
            if operation != 'discover':
                raise ValueError('unknown session cache operation')
            observe = payload.get('observe') is True
            hosts = []
            for spec in payload.get('hosts', []):
                if not isinstance(spec, dict) or not isinstance(spec.get('target'), str):
                    raise ValueError('invalid discovery host')
                # Friendly aliases do not create a second host cache.
                key = _digest({k: v for k, v in spec.items() if k != 'host'})
                if self.loading and not observe:
                    self.waiting[key] = {'hosts': [spec], 'fields': list(dict.fromkeys(
                        self.waiting.get(key, {}).get('fields', []) + payload.get('fields', []))),
                                         'refresh': payload.get('refresh') is True or self.waiting.get(key, {}).get('refresh', False)}
                entry = self.entries.get(key)
                if entry is None and not self.loading and not observe:
                    entry = self.entries.setdefault(key, dict(spec=spec, checked=0, retry=0, refreshing=False))
                if entry is not None:
                    now = self.clock()
                    failure = self.failures.get(spec['target'], {})
                    if not observe:
                        self._overlay(entry)
                        entry['last_used'] = now
                    retry = failure.get('retry_at', 0)
                    missing = any(field not in entry.get('value', {}) for field in payload.get('fields', []) if field in ('rows', 'resume', 'catalog', 'mcp', 'scopes'))
                    # Live commands request their own local inventory and verified details.
                    # Forcing historical reconciliation here would compete with the foreground read.
                    force_request = payload.get('refresh') is True and not (
                        spec['target'] == 'local' and self.inventory and payload.get('live') == 'wait')
                    force = force_request and spec['target'] not in self.active_targets and not entry.get('pending')
                    stale = bool(entry.get('dirty')) or not entry.get('checked') or now - entry['checked'] >= TTL or now < entry['checked']
                    if not observe:
                        if spec['target'] == 'local':
                            due = stale or missing or entry.get('pending') or 'catalog' not in entry.get('value', {}) or now - entry['checked'] > LOCAL_INTERVAL
                            if due or force:
                                entry['requested'] = True
                        if force_request and not entry['refreshing'] and not entry.get('pending'):
                            entry['force_refresh'] = True
                            entry['requested'] = True
                            entry['next_poll'] = 0
                        if force and not self.loading and not entry['refreshing']:
                            entry['force_refresh'] = True
                            self._start(key, entry)
                        elif spec['target'] == 'local':
                            self._schedule(key, entry)
                        elif ((stale or missing or entry.get('pending') or entry.get('requested')) and not self.loading
                              and not entry['refreshing'] and spec['target'] not in self.active_targets
                              and now >= retry and now >= entry.get('next_poll', 0)):
                            self._start(key, entry)
                    auth_failed = 'resume' in payload.get('fields', []) and any(
                        item.get('code') == 'github_auth_failed' for item in
                        entry.get('value', {}).get('catalog', {}).get('diagnostics', [])
                        if isinstance(item, dict))
                    hosts.append(dict(host=spec.get('host', spec['target']), target=spec['target'],
                                      checked=entry['checked'], stale=stale,
                                      refreshing=entry['refreshing'] or (entry.get('requested', False) and not failure) or (missing and not failure and spec['target'] != 'local') or entry.get('pending', False) or (stale and now >= retry
                                          and spec['target'] in self.active_targets),
                                      error=failure.get('error', entry.get('error', '')), retry_at=retry,
                                      error_kind=failure.get('kind', ''), diagnostic=failure.get('diagnostic', ''),
                                      observed=entry.get('observed', entry['checked']),
                                      resume_pr_auth_failed=auth_failed,
                                      work_revision=entry.get('work_revision', 0),
                                      timings=entry.get('timings', {}), refresh_revision=entry.get('refresh_revision', 0),
                                      refresh_requested=entry.get('refresh_revision', 0) + int(
                                          bool(entry['refreshing'] or entry.get('pending') or
                                               entry.get('requested') and (force_request or now >= retry))),
                                      **({'enriched': entry.get('terminals')} if payload.get('live') else {}),
                                      value=copy.deepcopy({k: v for k, v in entry['value'].items()
                                                           if k in payload.get('fields', entry['value'])}) if 'value' in entry else None))
                else:
                    hosts.append(dict(host=spec.get('host', spec['target']), target=spec['target'],
                                      checked=0, stale=True, refreshing=False, error='', retry_at=0, value=None,
                                      refresh_revision=0, refresh_requested=int(self.loading)))
            if self.inventory and payload.get('live'):
                for host in hosts:
                    if host['target'] == 'local':
                        if not observe:
                            host['live_requested'] = self.inventory.request(explicit=payload.get('live') == 'wait')
                        self.inventory.project(host, host.pop('enriched', None))
                        host['refreshing'] |= host.get('live_pending', False)
            return dict(hosts=hosts, loading=self.loading, persistence_error=self.persistence_error, instance=self.instance)

    def _start(self, key, entry):
        target = entry['spec']['target']
        entry['refreshing'] = True
        self.active_targets.add(target)
        self.submit(self.refresh, key, self.generations.get(target, 0), self.reset_epochs.get(target, 0))

    def _schedule(self, key, entry):
        target = entry['spec']['target']
        if (not self.loading and entry.get('requested') and not entry['refreshing']
                and target not in self.active_targets and self.clock() >= entry.get('next_poll', 0)
                and self.clock() >= self.failures.get(target, {}).get('retry_at', 0)):
            self._start(key, entry)

    def tick(self):
        """Drain requested local work; retain existing remote observation leases."""
        with self.lock:
            if self.loading:
                return
            now = self.clock()
            for key, entry in self.entries.items():
                target = entry['spec']['target']
                if target == 'local':
                    self._schedule(key, entry)
                    continue
                if ('last_used' not in entry or now - entry['last_used'] >= ACTIVE_TTL or target in self.active_targets
                        or now < entry.get('next_poll', 0)
                        or now < self.failures.get(target, {}).get('retry_at', 0)):
                    continue
                self._start(key, entry)

    def export(self, payload):
        """Versioned host-local metadata only. No imported evidence or prompts."""
        if payload.get('spec', {}).get('target') != 'local':
            raise ValueError('only local discovery may be exported')
        view = self.handle('discover', {'hosts': [payload['spec']], 'fields': ['rows', 'catalog', 'scopes'],
                                        'refresh': payload.get('refresh') is True})
        host = view['hosts'][0]
        value = host.get('value')
        health = {k: host.get(k) for k in ('checked', 'observed', 'refreshing', 'error', 'error_kind',
                                            'refresh_revision', 'refresh_requested')}
        health['instance'] = self.instance
        if value is None or 'catalog' not in value:
            return dict(protocol=2, pending=True, health=health)
        token = _digest(value)
        previous = self.export_versions.get(payload.get('generation'))
        self.export_versions[token] = copy.deepcopy(value)
        while len(self.export_versions) > 8:
            self.export_versions.pop(next(iter(self.export_versions)))
        result = dict(protocol=2, generation=token, health=health)
        if payload.get('generation') == token:
            return result
        if previous is None:
            return dict(result, full=copy.deepcopy(value))
        before = {record_key(r): r for r in previous.get('catalog', {}).get('conversations', [])}
        after = {record_key(r): r for r in value.get('catalog', {}).get('conversations', [])}
        return dict(result, delta=dict(
            fields={k: copy.deepcopy(v) for k, v in value.items() if k != 'catalog' and previous.get(k) != v},
            catalog={k: copy.deepcopy(v) for k, v in value['catalog'].items() if k != 'conversations'},
            changed=[copy.deepcopy(r) for k, r in after.items() if before.get(k) != r],
            removed=[k for k in before if k not in after]))

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
        with self.lock:
            entry = self.entries[key]
            spec = dict(entry['spec'], reconcile=not entry.get('reconciled') or self.clock() - entry['reconciled'] >= TTL)
            # Demand drives scheduling; only an explicit refresh forces reconciliation.
            if entry.pop('force_refresh', False):
                spec.update(refresh=True, reconcile=True)
            if spec['target'] == 'local':
                terminals = self.inventory.terminals() if self.inventory else None
                spec['live_only'] = not entry.get('value')
            if entry.get('peer_ticket'):
                spec['peer_ticket'] = entry['peer_ticket']
            if spec['target'] != 'local' and entry.get('value'):
                spec['previous'] = copy.deepcopy(entry['value'])
        target = spec['target']
        if reset_epoch is None:
            reset_epoch = self.reset_epochs.get(target, 0)
        started = time.monotonic()
        kind, diagnostic = '', ''
        try:
            value, error = self.collect(spec), ''
            health = value.get('_remote_health') or {}
            if health.get('error'):
                error, kind = health['error'], health.get('error_kind') or 'collector'
        except Exception as exc:
            value, error = None, ' '.join(str(exc).split())[:512] or 'session scan failed'
            kind = getattr(exc, 'kind', 'transport' if isinstance(exc, OSError) and target != 'local' else 'collector')
            diagnostic = getattr(exc, 'diagnostic', '')
            if not diagnostic:
                try:
                    diagnostic = _diagnostic(spec, traceback.format_exc())
                except OSError:
                    pass
        with self.lock:
            entry = self.entries[key]
            entry['refreshing'] = False
            self.active_targets.discard(target)
            if reset_epoch != self.reset_epochs.get(target, 0):
                return
            # Ordinary row invalidation must not erase evidence that a host is
            # unreachable. Only an explicit reset can supersede this failure.
            if error:
                if generation == self.generations.get(target, 0):
                    entry['requested'] = False
                entry['pending'] = False
                attempts = self.failures.get(target, {}).get('attempts', 0) + 1
                delay = FAILURE_TTL if kind in ('transport', 'authentication') else (5, 15, 60, 300)[min(attempts - 1, 3)]
                self.failures[target] = dict(retry_at=self.clock() + delay, error=error, kind=kind,
                                             diagnostic=diagnostic, attempts=attempts)
                self.persist()
            status = dict(attempt=self.clock(), duration=round(time.monotonic()-started, 3),
                          error=error, retry_at=self.failures.get(target, {}).get('retry_at', 0) if error else 0)
            if generation == self.generations.get(target, 0):
                if not error:
                    self.failures.pop(target, None)
                    for sibling in self.entries.values():
                        if sibling['spec']['target'] == target:
                            sibling['error'] = ''
                            sibling['retry'] = 0
                entry['error'] = error
                entry['retry'] = self.failures.get(target, {}).get('retry_at', 0)
                entry['next_poll'] = self.clock() + (LOCAL_INTERVAL if target == 'local' else REMOTE_INTERVAL)
                entry['timings'] = {**((value or {}).pop('_timings', {})), 'total_seconds': round(time.monotonic() - started, 4)}
                if value is not None:
                    if value.pop('_pending', False):
                        entry['peer_ticket'] = value.get('_peer_ticket')
                        entry['next_poll'] = self.clock() + 1
                        entry['pending'] = True
                        entry['requested'] = target == 'local'
                    else:
                        health = value.get('_remote_health') or {}
                        if target == 'local' and terminals is not None:
                            entry['terminals'] = terminals
                        if work_reports(value) != work_reports(entry.get('value', {})):
                            entry['work_revision'] = entry.get('work_revision', 0) + 1
                        entry['value'], entry['checked'] = value, health.get('checked') or self.clock()
                        entry['observed'] = health.get('observed') or entry['checked']
                        entry['pending'] = False
                        entry['dirty'] = entry.pop('catalog_changed', False)
                        entry['requested'] = entry['dirty']
                        if spec['reconcile']:
                            entry['reconciled'] = self.clock()
                        if spec.get('live_only') and 'catalog' not in value:
                            entry['next_poll'] = self.clock()
                            entry['pending'] = True
                            entry['requested'] = True
                        if value.get('_legacy_remote'):
                            entry['next_poll'] = self.clock() + FAILURE_TTL
                        self.overlays.pop(spec['target'], None)
                if error or not entry.get('pending'):
                    entry.pop('peer_ticket', None)
                    entry['refresh_revision'] = entry.get('refresh_revision', 0) + 1
                self.persist()
        if target == 'local' and self.inventory:
            _report('enrichment', status)


def record_key(row):
    return json.dumps([row['repo'], row['agent'], row['sid']], separators=(',', ':'))


def work_reports(value):
    return {record_key(row): row['work_report'] for row in value.get('catalog', {}).get('conversations', [])
            if row.get('work_report')}


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
            row = None
            for identity in _snapshot_identities():
                row = db.execute('select payload from snapshots where namespace=? and version in (1,?)',
                                 (_digest(identity), VERSION)).fetchone()
                if row is not None:
                    break
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
            if message.get('revision') != (getattr(self.server, 'revision', None) or _revision()):
                brew = getattr(self.server, 'homebrew', None)
                if (not brew or Path(brew['opt']).resolve() != brew['source']
                        or _revision() != self.server.revision):
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                raise ValueError('session cache service version changed; restarting')
            result = self.server.memory.handle(message['operation'], message.get('payload') or {})
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            result = {'error': str(error)}
        try:
            self.wfile.write(json.dumps(result).encode() + b'\n')
        except OSError:
            pass


def _socket_identity(address):
    try:
        info = os.lstat(address)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISSOCK(info.st_mode) else None


class _EndpointLost(OSError):
    """The listener no longer owns a reachable filesystem endpoint."""


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def server_bind(self):
        super().server_bind()
        self.endpoint_identity = _socket_identity(self.server_address)
        if self.endpoint_identity is None:
            raise _EndpointLost('session cache socket disappeared during bind')

    def service_actions(self):
        # A live process and lifetime lock do not prove that clients can reach
        # the socket. Exit our own loop (shutdown() here would deadlock), flush,
        # and release the lock so a requesting client can replace this daemon.
        if _socket_identity(self.server_address) != self.endpoint_identity:
            raise _EndpointLost('session cache socket disappeared or was replaced')
        if hasattr(self, 'ready'):
            changed = False
            for _ in range(128):
                try:
                    changed |= self.ready.recv(1) == b'R'
                except BlockingIOError:
                    break
            if changed:
                self.memory.inventory.request(changed=True)
        if hasattr(self, 'memory'):
            self.memory.tick()


def _serve():
    os.umask(0o077)
    root = _private(_runtime())
    fd = os.open(root / 'service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(.05)
        brew = _homebrew()
        if brew and Path(brew['opt']).resolve() != SOURCE:
            return  # A stale candidate must not claim the active opt namespace.
        # One migration per elected generation, never a scan on every read.
        retire_obsolete()
        address = root / 'service.sock'
        address.unlink(missing_ok=True)
        memory = _Memory()
        memory.inventory = _LIVE['Inventory'](lambda status: _report('inventory', status))
        disk = _Persistence(memory)
        memory.persist = disk.wake.set
        writer = threading.Thread(target=disk.run, daemon=True)
        ready = root / 'ready.sock'
        ready.unlink(missing_ok=True)
        with _Server(str(address), _Handler) as server, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as signals:
            signals.bind(str(ready))
            signals.setblocking(False)
            server.ready = signals
            address.chmod(0o600)
            server.memory = memory
            server.revision = _revision()
            server.homebrew = _homebrew()
            def stop(*_args):
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            writer.start()
            try:
                server.serve_forever(poll_interval=.25)
            except _EndpointLost:
                pass
            finally:
                disk.stop.set()
                disk.wake.set()
                writer.join(2)
                ready.unlink(missing_ok=True)
                # Never remove an endpoint installed after this listener bound.
                if _socket_identity(address) == server.endpoint_identity:
                    address.unlink(missing_ok=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['serve']:
        _serve()
    elif len(sys.argv) == 3 and sys.argv[1] == 'invalidate':
        request('event', {'targets': json.loads(sys.argv[2])})
    elif len(sys.argv) == 3 and sys.argv[1] == 'linked-source':
        print(linked_source(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == 'retire':
        previous = sys.argv[2]
        retire_obsolete([previous] if previous and _same_installation(previous) else [])
