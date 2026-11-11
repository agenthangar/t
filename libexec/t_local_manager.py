"""Per-user local inference coordinator. Prompts travel in memory, never in status files."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import collections
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import runpy
import secrets
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

LIB = Path(__file__).resolve().parent
VERSION = 3
LIMIT = 16 * 1024**2
TERMINAL = {'completed', 'cancelled', 'failed', 'timed_out', 'busy'}
DEFAULTS = {'memory_percent': 80, 'idle_seconds': 300,
            'providers': {'lmstudio': {'mode': 'headless', 'url': 'http://127.0.0.1:1234'},
                          'ollama': {'mode': 'headless', 'url': 'http://127.0.0.1:11434'}}}


def module(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def root():
    # HOME/XDG in a worker sandbox must not create a second scheduler.
    return Path(os.environ.get('T_LOCAL_RUNTIME_ROOT') or
                Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/t/local-runtime')


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Local runtime directory must be owned by this user with mode 0700')
    return path


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temp.replace(path)


def read_json(path, default=None):
    try:
        path = Path(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe local runtime state file')
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        value = subprocess.check_output(['ps', '-p', str(pid), '-o', 'uid=,lstart='], text=True, timeout=2).strip()
        if not value or int(value.split()[0]) != os.getuid():
            return None
        return value
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def settings(value=None):
    value = read_json(root() / 'settings.json', {}) if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unsupported machine settings')
    result = json.loads(json.dumps(DEFAULTS))
    result.update(value)
    if type(result['memory_percent']) is not int or not 10 <= result['memory_percent'] <= 80:
        raise ValueError('Memory budget must be from 10 to 80 percent')
    if type(result['idle_seconds']) is not int or not 30 <= result['idle_seconds'] <= 3600:
        raise ValueError('Idle lifetime must be from 30 to 3600 seconds')
    providers = result['providers']
    if not isinstance(providers, dict) or set(providers) != {'lmstudio', 'ollama'}:
        raise ValueError('Configure both local providers')
    runtime = module('t_local_runtime')
    for config in providers.values():
        if not isinstance(config, dict) or set(config) != {'mode', 'url'} or config['mode'] not in ('desktop', 'headless'):
            raise ValueError('Choose desktop or headless provider mode')
        config['url'] = runtime['local_url'](config['url'])
    return result


def rpc(action, *, _timeout=5, **fields):
    path = root() / 'control.sock'
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(_timeout)
        conn.connect(str(path))
        conn.sendall(json.dumps(dict(action=action, **fields)).encode() + b'\n')
        with conn.makefile('rb') as stream:
            raw = stream.readline(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('Runtime control response exceeds limit')
    result = json.loads(raw)
    if 'control_error' in result:
        raise ValueError(result['control_error'])
    return result


def status():
    """Read-only, including when the coordinator is stopped or crashed."""
    if not root().exists():
        return {'manager': 'stopped', 'requests': [], 'models': [], 'clients': [], 'settings': settings({})}
    private_directory(root())
    try:
        return rpc('status')
    except (OSError, ValueError):
        saved = read_json(root() / 'status.json', {})
        return dict(saved, manager='stale' if saved else 'stopped',
                    requests=[dict(x, state='unknown') if x.get('state') not in TERMINAL else x
                              for x in saved.get('requests', [])], models=saved.get('models', []),
                    clients=[], settings=saved.get('settings', settings()))


def legacy_busy():
    """Probe an existing lock without creating runtime state or retaining a lease."""
    path = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 't/local-worker/worker.lock'
    try:
        with path.open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def availability(deadline=None):
    """Never start the manager, a provider, or inference for a preflight."""
    try:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Release wait expired')
            return rpc('availability', _timeout=min(5, remaining))
        view = status()
        if view.get('manager') == 'running' and view.get('protocol') == VERSION:
            return rpc('availability')
        state, reason = 'unknown', 'Runtime state is stale or incompatible; refresh the MCP connection after active sessions finish'
        if (view.get('manager') == 'running' and view.get('protocol') in (1, 2) and
                view.get('clients') == [] and
                all(j.get('state') in TERMINAL for j in view.get('requests', []))):
            # ensure() upgrades only after the old manager atomically confirms it
            # has no clients or active generation. This read does not restart it.
            state, reason = 'available', 'Idle older manager can be upgraded at admission'
        elif view.get('manager') == 'stopped':
            if legacy_busy():
                state, reason = 'busy', 'An older worker holds the shared runtime lock'
            elif read_json(root() / 'leases.json', {}).get('instances'):
                reason = 'Previous runtime instances require ownership recovery'
            else:
                state, reason = 'available', 'No managed inference is active; readiness is checked at admission'
        return dict(state=state, reason=reason, blockers=[], manager=view.get('manager'),
                    protocol=view.get('protocol'), checked_at=time.time(), retry_after_seconds=None)
    except (OSError, ValueError):
        return dict(state='unknown', reason='Runtime availability could not be verified', blockers=[],
                    checked_at=time.time(), retry_after_seconds=None)


def ensure(testing=False):
    private_directory(root())
    try:
        view = rpc('status')
        if view.get('protocol') != VERSION or (testing and not view.get('test_preflight')):
            if view.get('protocol') not in (1, 2, VERSION) or view.get('clients') != []:
                raise ValueError('Existing local manager uses a different protocol; let its sessions finish before restarting')
            # The old manager rechecks attached clients under its lock. Never
            # replace a runtime serving a live session, even if admission races.
            rpc('shutdown')
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    rpc('status')
                except (ConnectionError, FileNotFoundError, socket.timeout):
                    break
                time.sleep(.1)
            else:
                raise ValueError('Older local manager is still shutting down; retry when it finishes')
        else:
            return view
    except (ConnectionError, FileNotFoundError, socket.timeout):
        pass
    env = {k: v for k, v in os.environ.items() if not k.startswith(('COV_CORE_', 'OPENAI_', 'ANTHROPIC_'))}
    # Preserve the real user's provider model/config paths, not a nested worker HOME.
    if 'T_LOCAL_RUNTIME_ROOT' not in os.environ:
        env['HOME'] = pwd.getpwuid(os.getuid()).pw_dir
    env['T_LOCAL_RUNTIME_ROOT'] = str(root())
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            return rpc('status')
        except (OSError, ValueError):
            time.sleep(.1)
    raise ValueError('Local inference manager did not start; inspect t config models runtime status')


def owner_identity(pid, tmux='', pane=''):
    """Fairness is grouped only by verified pane ancestry; a supplied parent ID is never authority."""
    activity = module('t_local_status')
    if tmux.startswith('/') and re.fullmatch(r'%\d+', pane):
        info = activity['pane_info'](tmux, pane)
        if info and activity['ancestry'](pid, int(info[1])):
            identity = process_identity(int(info[1]))
            if identity:
                return hashlib.sha256((tmux + pane + identity).encode()).hexdigest(), True
    return uuid.uuid4().hex, False


class Scheduler:
    """One whole request per turn, FIFO within each owner and round-robin between owners."""
    def __init__(self):
        self.queues = collections.OrderedDict()
        self.last_owner = None

    def add(self, job):
        self.queues.setdefault(job['owner'], collections.deque()).append(job)

    def take(self, now, client=None):
        owners = list(self.queues)
        if self.last_owner in owners:
            offset = owners.index(self.last_owner) + 1
            owners = owners[offset:] + owners[:offset]
        for owner in owners:
            queue = self.queues[owner]
            while queue and queue[0]['state'] in TERMINAL:
                queue.popleft()
            if not queue:
                del self.queues[owner]
                continue
            job = queue[0]
            if (client and job['client'] != client) or job.get('retry_at', 0) > now:
                continue
            queue.popleft()
            self.last_owner = owner
            return job
        return None


class Engine:
    def __init__(self, provider_factory=None):
        self.generation = uuid.uuid4().hex
        self.clients, self.jobs, self.responses = {}, {}, {}
        self.condition = threading.Condition(threading.RLock())
        self.scheduler = Scheduler()
        self.active = None
        self.measurement_owner = None
        self.blocked = None
        self.stopping = threading.Event()
        self.config = settings()
        self.providers = (provider_factory or module('t_local_runtime')['Providers'])(root(), self.config, self.persist)
        self.legacy = None
        self.port = None
        self.needs_recovery = True
        self.maintenance_busy = False
        self.test_lease = None
        self.recovery_pending = []
        self.blocked_instance = None
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.maintenance = threading.Thread(target=self.maintain, daemon=True)

    def start(self):
        self.thread.start()
        self.maintenance.start()

    def recover(self):
        ledger = read_json(root() / 'leases.json', {})
        daemon = ledger.get('ollama_process')
        stopped_base = None
        if daemon and daemon.get('identity') and process_identity(daemon.get('pid')) == daemon['identity']:
            try:
                os.killpg(daemon['pid'], signal.SIGKILL)
                stopped_base = daemon['base']
            except ProcessLookupError:
                stopped_base = daemon['base']
        self.recovery_pending = list(ledger.get('instances', []))
        for item in list(self.recovery_pending):
            if item.get('base') == stopped_base:
                self.recovery_pending.remove(item)
                continue
            # Private journal + exact endpoint/model/context checks, never a name prefix.
            try:
                if self.providers.present(item):
                    if not item.get('owned'):
                        raise ValueError('Borrowed instance may still be generating')
                    self.providers.unload(item)
                self.recovery_pending.remove(item)
            except (OSError, ValueError, KeyError):
                self.blocked = 'An earlier provider instance could not be confirmed stopped; inspect the provider app'
        self.needs_recovery = False
        self.persist()

    def persist(self):
        if not hasattr(self, 'providers'):
            return
        proc = getattr(self.providers, 'ollama_process', None)
        daemon = {'pid': proc.pid, 'identity': process_identity(proc.pid), 'base': self.providers.ollama_base} if proc else None
        write_json(root() / 'leases.json', {'generation': self.generation, 'ollama_process': daemon,
                   'instances': self.recovery_pending + list(self.providers.instances.values())})

    def snapshot(self):
        now = time.time()
        with self.condition:
            queue = Scheduler()
            queue.last_owner = self.scheduler.last_owner
            queue.queues = collections.OrderedDict((k, collections.deque(v)) for k, v in self.scheduler.queues.items())
            queued = []
            while (item := queue.take(float('inf'))) is not None:
                queued.append(item)
            positions = {j['id']: i + 1 for i, j in enumerate(queued)}
            requests = [dict({k: j.get(k) for k in ('id', 'client', 'owner', 'owner_label', 'model', 'provider', 'kind', 'state',
                                                   'created', 'started', 'execution_started', 'finished', 'error', 'timeout_phase', 'tokens', 'revision')},
                             queue_position=positions.get(j['id']),
                             queue_seconds=round((j.get('started') or j.get('finished') or now) - j['created'], 2),
                             loading_seconds=round((j.get('execution_started') or j.get('finished') or now) - j['started'], 2) if j.get('started') else 0,
                             execution_seconds=round((j.get('finished') or now) - j['execution_started'], 2) if j.get('execution_started') else 0)
                        for j in self.jobs.values()]
            clients = [self.client_status(c) for c in self.clients.values()]
            models = [dict(provider=x['provider'], model=x['model'], revision=x['revision'], context=x['context'],
                           alias=x['alias'], base=x['base'], owned=x['owned'], memory_bytes=x['estimate'], estimated=True,
                           last_used=x['last_used'], pinned=x['alias'] in self.pins(),
                           active=bool(self.active and self.active.get('alias') == x['alias']))
                      for x in list(self.providers.instances.values())]
            return dict(protocol=VERSION, test_preflight=True, generation=self.generation, pid=os.getpid(), manager='blocked' if self.blocked else 'running',
                        error=self.blocked, updated=now, requests=requests[-200:], clients=clients,
                        models=models + self.providers.external, settings=self.config,
                        endpoints={'lmstudio': self.config['providers']['lmstudio']['url'],
                                   'ollama': self.providers.ollama_base or self.config['providers']['ollama']['url']})

    def client_status(self, client):
        now = time.time()
        jobs = [j for j in self.jobs.values() if j['client'] == client['id']]
        current = next((j for j in reversed(jobs) if j['state'] not in TERMINAL), None)
        def duration(intervals):
            total, end = 0, 0
            for start, stop in sorted(intervals):
                total += max(0, stop - max(start, end))
                end = max(end, stop)
            return total
        # Concurrent requests can overlap: exclude wall time once, never per request.
        wait = duration((j['created'], j.get('execution_started') or j.get('finished') or now) for j in jobs)
        queue = duration((j['created'], j.get('started') or j.get('finished') or now) for j in jobs)
        latest = jobs[-1] if jobs else None
        failure = latest if latest and latest['state'] in ('busy', 'failed', 'timed_out') else None
        return dict(id=client['id'], protocol=VERSION, model=client['chosen']['model'], provider=client['chosen']['provider'],
                    state=current['state'] if current else 'working', wait_seconds=wait,
                    queue_seconds=queue, loading_seconds=max(0, wait - queue),
                    outcome=client.get('outcome') or (failure['state'] if failure else None),
                    failure_reason=client.get('failure_reason') or (failure.get('error') if failure else None),
                    timeout_phase=client.get('timeout_phase') or (failure.get('timeout_phase') if failure else None),
                    request_ids=[j['id'] for j in jobs][-200:],
                    generation_events=client.get('generation_events', 0),
                    request_id=current['id'] if current else None, identity_verified=client.get('identity_verified', False),
                    revision=client.get('revision'), runtime_model=client.get('runtime_model'),
                    owner_verified=client['owner_verified'], owner_label=client['owner_label'],
                    interrupted=client.get('interrupted', False),
                    failed=any(j['state'] in ('failed', 'timed_out') for j in jobs))

    def test_owners(self):
        """Stable identities only: elapsed time and heartbeat do not change consent."""
        busy = {j['client'] for j in self.jobs.values() if j['state'] not in TERMINAL}
        busy |= {v['client'] for v in self.responses.values()}
        busy |= {c['id'] for c in self.clients.values() if c.get('pinned_alias')}
        return [dict(id=c['id'], pid=c['pid'], identity=c['identity'],
                     model=c['chosen']['model'], kind=c['kind'],
                     tmux=c.get('tmux', ''), pane=c.get('pane', ''))
                for c in sorted(self.clients.values(), key=lambda c: c['id']) if c['id'] in busy]

    def test_control(self, data):
        action = data['action']
        if action == 'test-preflight':
            owners = self.test_owners()
            active_label = {'running': 'Generating', 'loading': 'Loading', 'cancelling': 'Stopping',
                            'waiting_for_legacy': 'Waiting for older worker', 'waiting_for_memory': 'Waiting for memory'}
            activities = {owner['id']: (active_label.get(self.active['state'], 'Queued') if self.active and self.active['client'] == owner['id'] else
                          'Queued' if any(j['client'] == owner['id'] and j['state'] not in TERMINAL for j in self.jobs.values()) else
                          'Conversation holds model') for owner in owners}
            return dict(generation=self.generation, owners=owners, activities=activities,
                        legacy_owned=bool(self.legacy), pid=os.getpid(), reserved=bool(self.test_lease))
        if action == 'test-reserve':
            if self.test_lease or data.get('generation') != self.generation or data.get('owners') != self.test_owners():
                raise ValueError('Runtime owners changed; review the updated session list')
            identity = process_identity(data.get('pid'))
            if not identity:
                raise ValueError('Test process identity could not be verified')
            self.test_lease = dict(token=secrets.token_urlsafe(32), pid=data['pid'], identity=identity,
                                   heartbeat=time.time(), owners=data['owners'], client=None)
            return {'token': self.test_lease['token']}
        lease = self.test_lease
        if not lease or not hmac.compare_digest(lease['token'], data.get('token', '')):
            raise ValueError('Model test reservation expired')
        lease['heartbeat'] = time.time()
        if action == 'test-release':
            self.test_lease = None
            self.condition.notify_all()
            return {'released': True}
        if action == 'test-interrupt':
            if any(owner not in lease['owners'] for owner in self.test_owners()):
                raise ValueError('Runtime owners changed; review the updated session list')
            # Consent covers every managed pin and active request. Only t-owned
            # instances may be unloaded; borrowed instances retain their guard.
            for c in self.clients.values():
                if c['id'] in {o['id'] for o in lease['owners']} and c['kind'] == 'worker':
                    c['interrupted'] = True
            for job in self.jobs.values():
                if job['client'] in {o['id'] for o in lease['owners']}:
                    job['interruptible'] = job.get('owned_instance', False)
                    self.cancel_locked(job)
            lease['interrupted'] = True
            return {'interrupt_requested': True}
        if action == 'test-heartbeat':
            return {'reserved': True}
        if action == 'test-ready':
            if self.blocked or self.active or self.maintenance_busy:
                return {'ready': False, 'error': self.blocked}
            if lease.get('interrupted'):
                ids = {o['id'] for o in lease['owners']}
                self.responses = {k: v for k, v in self.responses.items() if v['client'] not in ids}
                for c in self.clients.values():
                    if c['id'] in ids:
                        c.pop('pinned_alias', None)
            lease['ready'] = self.legacy_lock()
            return {'ready': lease['ready']}
        raise ValueError('Unknown model test control action')

    def availability(self, client_id=None):
        """Called under the scheduler lock, also used for atomic admission."""
        blockers = [dict(request_id=j['id'], model=j['model'], provider=j['provider'],
                         owner_label=j['owner_label'], state=j['state'],
                         owner_verified=self.clients.get(j['client'], {}).get('owner_verified', False))
                    for j in self.jobs.values() if j['client'] != client_id and j['state'] not in TERMINAL]
        leases = [c for c in self.clients.values() if c['id'] != client_id and c.get('fail_fast')]
        state, reason, retry = 'available', 'Shared runtime is available', None
        if self.blocked or self.recovery_pending or (self.needs_recovery and read_json(root() / 'leases.json', {}).get('instances')):
            state, reason = 'unknown', self.blocked or 'Runtime ownership recovery is pending'
        elif self.test_lease:
            state, reason = 'busy', 'A model test holds the shared runtime reservation'
        elif blockers or leases or self.measurement_owner:
            state, reason = 'busy', 'Another local task is using the shared runtime'
            # Only an observed end of generation permits a bounded release wait.
            if (self.active and self.active.get('backend_stopped') and
                    all(b['request_id'] == self.active['id'] for b in blockers) and
                    all(c['id'] == self.active['client'] for c in leases) and not self.measurement_owner):
                retry = 15
        elif not self.legacy and legacy_busy():
            state, reason = 'busy', 'An older worker holds the shared runtime lock'
        elif self.maintenance_busy:
            state, reason = 'unknown', 'Runtime maintenance is in progress'
        elif any(x.get('memory_bytes') is None for x in self.providers.external):
            state, reason = 'unknown', 'External model memory use is unverified'
        if leases and not blockers:
            blockers = [dict(owner_label=c['owner_label'], owner_verified=c['owner_verified'],
                             model=c['chosen']['model'], state='admitted') for c in leases]
        return dict(state=state, reason=reason, blockers=blockers, protocol=VERSION,
                    manager='running', checked_at=time.time(), retry_after_seconds=retry)

    def pins(self):
        return ({v['alias'] for v in self.responses.values() if v['client'] in self.clients} |
                {c['pinned_alias'] for c in self.clients.values() if c.get('pinned_alias')})

    def prune_history(self):
        # Provider instance IDs can be reused after unload. Never attach old response
        # IDs to a newly loaded instance with the same public alias.
        with self.condition:
            loaded = {x['alias'] for x in list(self.providers.instances.values())}
            self.responses = {k: v for k, v in self.responses.items() if v['alias'] in loaded}
            for client in self.clients.values():
                if client.get('pinned_alias') not in loaded:
                    client.pop('pinned_alias', None)

    def control(self, data):
        action = data.get('action')
        with self.condition:
            if isinstance(action, str) and action.startswith('test-'):
                return self.test_control(data)
            if action == 'shutdown':
                if self.clients or self.active or self.test_lease:
                    raise ValueError('Local sessions are still attached')
                self.stopping.set()
                return {'stopping': True}
            if action == 'status':
                return self.snapshot()
            if action == 'availability':
                return self.availability()
            if action == 'register':
                if len(self.clients) >= 64:
                    raise ValueError('Local client limit reached')
                chosen = module('t_local_inference')['policy'](data['chosen'])
                fail_fast = data.get('kind', 'session') == 'worker' and chosen['mode'] == 'automatic'
                if fail_fast:
                    admission = self.availability()
                    if admission['state'] != 'available':
                        return dict(admitted=False, availability=admission)
                pid = data.get('pid')
                identity = process_identity(pid)
                if not identity:
                    raise ValueError('Client process identity could not be verified')
                owner, verified = owner_identity(pid, data.get('tmux', ''), data.get('pane', ''))
                token = secrets.token_urlsafe(32)
                client = dict(id=uuid.uuid4().hex, token=token, chosen=chosen, pid=pid, identity=identity, owner=owner,
                              fail_fast=fail_fast, owner_verified=verified, owner_label=('Terminal pane ' + data.get('pane', '') if verified and data.get('pane') else 'Process ' + str(pid)), heartbeat=time.time(), kind=data.get('kind', 'session')[:32],
                              tmux=data.get('tmux', ''), pane=data.get('pane', ''))
                if data.get('test_token'):
                    lease = self.test_lease
                    if (not lease or not hmac.compare_digest(lease['token'], data['test_token'])
                            or lease['pid'] != pid or lease['identity'] != identity or lease['client'] or not lease.get('ready')):
                        raise ValueError('Model test reservation expired or already in use')
                    lease['client'] = client['id']
                if client['kind'] == 'measurement':
                    client['owner'] = 'measurement:' + client['id']
                self.clients[client['id']] = client
                return {'id': client['id'], 'protocol': VERSION, 'token': token, 'base': f'http://127.0.0.1:{self.port}/{token}',
                        'generation': self.generation}
            if action in ('heartbeat', 'close', 'client'):
                client = self.clients.get(data.get('client'))
                if not client or not hmac.compare_digest(client['token'], data.get('token', '')):
                    raise ValueError('Unknown client lease')
                if action == 'close':
                    for job in self.jobs.values():
                        if job['client'] == client['id']:
                            self.cancel(job)
                    del self.clients[client['id']]
                    if self.measurement_owner == client['id']:
                        self.measurement_owner = None
                    self.responses = {k: v for k, v in self.responses.items() if v['client'] != client['id']}
                    return {'closed': True}
                client['heartbeat'] = time.time()
                return self.client_status(client)
            if action == 'cancel':
                job = self.jobs.get(data.get('request'))
                if not job:
                    raise ValueError('Unknown request')
                self.cancel(job)
                return {'id': job['id'], 'state': job['state']}
            if action == 'release':
                instance = next((v for v in self.providers.instances.values() if v['alias'] == data.get('alias')), None)
                if not instance:
                    raise ValueError('This model was not loaded by t')
                if self.active or self.maintenance_busy or self.test_lease or instance['alias'] in self.pins():
                    raise ValueError('Finish active responses and conversations before releasing this model')
                self.maintenance_busy = True
                self.condition.release()
                try:
                    self.providers.unload(instance)
                finally:
                    self.condition.acquire()
                    self.maintenance_busy = False
                    self.condition.notify_all()
                return {'released': True}
            if action == 'settings':
                value = settings(data.get('value'))
                if self.active or self.maintenance_busy or self.providers.instances or self.clients or self.test_lease:
                    raise ValueError('Finish local sessions and release cached models before changing machine settings')
                self.config = self.providers.settings = value
                write_json(root() / 'settings.json', value)
                return {'saved': True}
            raise ValueError('Unknown runtime control action')

    def cancel(self, job):
        with self.condition:
            self.cancel_locked(job)

    def cancel_locked(self, job):
        if job['state'] in TERMINAL:
            return
        job['cancel'].set()
        if job is not self.active:
            self.finish(job, 'cancelled')
        else:
            job['state'] = 'cancelling'
            connection = job.get('connection')
            sock = job.get('upstream_socket') or (connection.sock if connection else None)
            if sock and job.get('interruptible', False):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.condition.notify_all()

    def submit(self, client, path, payload, handler):
        with self.condition:
            if client.get('interrupted'):
                raise ValueError('This local worker was interrupted for a model test')
            if client.get('outcome') in ('busy', 'timed_out'):
                raise ValueError('Local task has ended; lead continuing')
            if client.get('fail_fast'):
                admission = self.availability(client['id'])
                if admission['state'] != 'available':
                    client.update(outcome='busy', failure_reason=admission['reason'])
                    raise ValueError('Local runtime busy; lead continuing')
            if self.blocked:
                raise ValueError(self.blocked)
            if len([j for j in self.jobs.values() if j['state'] not in TERMINAL]) >= 128:
                raise ValueError('Local request queue is full')
            previous = payload.get('previous_response_id')
            if previous and (previous not in self.responses or self.responses[previous]['client'] != client['id']):
                raise ValueError('Response history does not belong to this client; start a new conversation')
            if client['chosen']['provider'] == 'ollama' and previous:
                raise ValueError('Ollama requires stateless requests with full conversation input')
            now = time.time()
            job = dict(id=uuid.uuid4().hex, client=client['id'], owner=client['owner'], model=client['chosen']['model'],
                       provider=client['chosen']['provider'], kind=client['kind'], owner_label=client['owner_label'], state='queued', created=now,
                       deadline=now + client['chosen'].get('queue_timeout_seconds', 300), cancel=threading.Event(),
                       done=threading.Event(), payload=payload, handler=handler, path=path, chosen=client['chosen'],
                       method=handler.command)
            self.jobs[job['id']] = job
            self.scheduler.add(job)
            self.condition.notify_all()
            return job

    def finish(self, job, state, error=None):
        if job.get('timeout_phase') and state == 'cancelled':
            state, error = 'timed_out', job['timeout_phase'].capitalize() + ' budget expired'
        job.update(state=state, finished=time.time(), error=error)
        client = self.clients.get(job['client'])
        if client and client['kind'] == 'worker' and state in ('busy', 'timed_out'):
            client.update(outcome=state, failure_reason=error, timeout_phase=job.get('timeout_phase'))
        job.pop('payload', None)
        job.pop('connection', None)
        job.pop('upstream_socket', None)
        job['done'].set()

    def legacy_lock(self):
        if self.legacy:
            return True
        directory = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 't/local-worker'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (directory / 'worker.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return False
        self.legacy = lock
        return True

    def work(self):
        while not self.stopping.is_set():
            with self.condition:
                test_client = self.test_lease['client'] if self.test_lease else None
                job = (None if self.blocked or self.maintenance_busy or (self.test_lease and not test_client)
                       else self.scheduler.take(time.time(), test_client or self.measurement_owner))
                if not job:
                    self.condition.wait(.2)
                    continue
                self.active = job
            try:
                client = self.clients.get(job['client'])
                if client and client.get('outcome') in ('busy', 'timed_out'):
                    job['timeout_phase'] = client.get('timeout_phase')
                    self.finish(job, client['outcome'], client.get('failure_reason'))
                    continue
                if not self.legacy_lock():
                    if self.clients.get(job['client'], {}).get('fail_fast'):
                        self.finish(job, 'busy', 'An older worker holds the shared runtime lock')
                    else:
                        self.retry(job, 'waiting_for_legacy')
                    continue
                if self.needs_recovery:
                    self.recover()
                    if self.blocked:
                        raise ValueError(self.blocked)
                if job['cancel'].is_set() or job['client'] not in self.clients:
                    self.finish(job, 'cancelled')
                    continue
                job.update(state='loading', started=time.time())
                instance = self.providers.get(job['chosen'], self.pins())
                job['alias'], job['revision'] = instance['alias'], instance['revision']
                job['owned_instance'] = instance['owned']
                # A shared stateful instance must survive cancellation by another client.
                other_pins = {v['alias'] for v in self.responses.values() if v['client'] != job['client']} | {c.get('pinned_alias') for c in self.clients.values() if c['id'] != job['client']}
                job['interruptible'] = instance['owned'] and instance['alias'] not in other_pins
                client = self.clients.get(job['client'])
                if not client or job['cancel'].is_set():
                    self.finish(job, 'cancelled')
                    continue
                if job['cancel'].is_set() or time.time() > job['deadline']:
                    if time.time() > job['deadline']:
                        job['timeout_phase'] = 'loading'
                    self.finish(job, 'cancelled')
                    continue
                client.update(identity_verified=True, revision=instance['revision'], runtime_model=instance['alias'])
                previous = job['payload'].get('previous_response_id')
                if previous and self.responses[previous]['alias'] != instance['alias']:
                    raise ValueError('Conversation instance changed; start a new conversation')
                if job['kind'] == 'measurement':
                    self.measurement_owner = job['client']
                job.update(state='running', execution_started=time.time())
                self.forward(job, instance)
                if job['cancel'].is_set():
                    if job.get('sent') and not job.get('backend_stopped'):
                        self.providers.unload(instance)
                        self.prune_history()
                    self.finish(job, 'cancelled')
                else:
                    self.finish(job, 'completed')
            except Exception as exc:
                self.prune_history()
                if type(exc).__name__ == 'WaitingForMemory' and not job['cancel'].is_set():
                    if self.clients.get(job['client'], {}).get('fail_fast'):
                        self.finish(job, 'busy', str(exc))
                    else:
                        self.retry(job, 'waiting_for_memory', str(exc))
                else:
                    # Once a request was sent, a broken stream is not proof that generation stopped.
                    if job.get('sent') and not job.get('backend_stopped'):
                        try:
                            if not job.get('interruptible'):
                                raise ValueError('Shared instance must be preserved')
                            self.providers.unload(instance)
                            self.prune_history()
                        except Exception:
                            self.blocked_instance = instance
                            self.blocked = 'Could not confirm cancelled generation stopped; unload the affected model in its provider app'
                            self.persist()
                    self.finish(job, 'cancelled' if job['cancel'].is_set() else 'failed',
                                str(exc) if isinstance(exc, ValueError) else 'Provider request failed; inspect the provider runtime')
            finally:
                with self.condition:
                    if self.active is job:
                        self.active = None
                    self.condition.notify_all()

    def retry(self, job, state, error=None):
        with self.condition:
            job.pop('started', None)
            job.update(state=state, error=error, retry_at=time.time() + 1)
            self.scheduler.queues.setdefault(job['owner'], collections.deque()).appendleft(job)

    def forward(self, job, instance):
        payload = dict(job['payload'], model=instance['alias'])
        if instance['provider'] == 'ollama':
            payload['store'] = False
            if job['path'].startswith('/api/'):
                payload.pop('store', None)
                payload.update(keep_alive=-1, options=dict(payload.get('options', {}), num_ctx=instance['context']))
        if instance['provider'] == 'lmstudio' and payload.get('store') is not False and job['path'] == '/v1/responses':
            self.clients[job['client']]['pinned_alias'] = instance['alias']
        parsed = urllib.parse.urlsplit(instance['base'])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=job['chosen']['timeout_seconds'])
        job['connection'] = connection
        handler = job['handler']
        try:
            if job['cancel'].is_set():
                return
            job['sent'] = True
            connection.request(job['method'], job['path'], json.dumps(payload).encode() if job['method'] == 'POST' else None, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            job['upstream_socket'] = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if response.status >= 400:
                # Provider errors may echo prompts. Do not put them into shared state.
                raise ValueError('Provider rejected the request (HTTP ' + str(response.status) + ')')
            handler.send_response(response.status)
            handler.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
            handler.send_header('Connection', 'close')
            handler.end_headers()
            job['headers_sent'] = True
            tracker = ResponseTracker(self, job, instance, payload.get('store') is not False,
                                      'text/event-stream' in response.getheader('Content-Type', ''))
            while chunk := response.read1(65536):
                if job['cancel'].is_set():
                    if job.get('interruptible'):
                        return
                    continue  # Borrowed instances drain to EOF; never unload another app's model.
                tracker.feed(chunk)
                handler.wfile.write(chunk)
                handler.wfile.flush()
            tracker.finish()
            job['backend_stopped'] = not (job['cancel'].is_set() and job.get('interruptible'))
        finally:
            connection.close()
            handler.close_connection = True
            instance['last_used'] = time.time()

    def maintain(self):
        probe_index = 0
        while not self.stopping.wait(1):
            clients = list(self.clients.values())
            probe = clients[probe_index % len(clients)] if clients else None
            identity = process_identity(probe['pid']) if probe else None
            probe_index += 1
            with self.condition:
                now = time.time()
                if self.test_lease and now - self.test_lease['heartbeat'] > 15:
                    self.test_lease = None
                for job in list(self.jobs.values()):
                    if job is not self.active and job['state'] not in TERMINAL and now > job['deadline']:
                        job['timeout_phase'] = 'queue'
                        self.finish(job, 'timed_out', 'Queue wait expired; execution budget was not consumed')
                    if job is self.active and job.get('execution_started') and now - job['execution_started'] > job['chosen']['timeout_seconds']:
                        job['timeout_phase'] = 'execution'
                        self.cancel(job)
                for client in list(self.clients.values()):
                    if now - client['heartbeat'] > 15 or (client is probe and identity != client['identity']):
                        self.control({'action': 'close', 'client': client['id'], 'token': client['token']})
                # Reserve maintenance without holding the control lock over provider I/O.
                # Heartbeats and cancellation remain responsive during slow unloads.
                if not self.active and not self.maintenance_busy:
                    self.maintenance_busy = True
                    pins = self.pins()
                    self.condition.release()
                    try:
                        if self.blocked_instance and not self.providers.present(self.blocked_instance):
                            self.blocked_instance = None
                            self.blocked = None
                        if self.recovery_pending:
                            for item in list(self.recovery_pending):
                                if not self.providers.present(item):
                                    self.recovery_pending.remove(item)
                            if not self.recovery_pending:
                                self.blocked = None
                                self.persist()
                        if self.blocked and not self.blocked_instance and not self.recovery_pending:
                            if all(not self.providers.present(x) for x in list(self.providers.instances.values())):
                                self.blocked = None
                        if not self.blocked:
                            self.providers.expire(pins)
                    except (OSError, ValueError):
                        self.blocked = self.blocked or 'An idle model could not be confirmed unloaded'
                    finally:
                        self.condition.acquire()
                        self.maintenance_busy = False
                        self.condition.notify_all()
                    if not self.providers.instances and not self.blocked and self.legacy and not self.test_lease:
                        self.legacy.close()
                        self.legacy = None
                # A bounded metadata history; no request bodies are written here.
                for key, job in list(self.jobs.items()):
                    if job['state'] in TERMINAL and now - job['finished'] > 3600:
                        del self.jobs[key]
                write_json(root() / 'status.json', self.snapshot())

    def close(self):
        self.stopping.set()
        with self.condition:
            for job in self.jobs.values():
                self.cancel(job)
            self.condition.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=35)
        if self.maintenance.is_alive():
            self.maintenance.join(timeout=3)
        if not self.active:
            self.providers.close()
        if self.legacy:
            self.legacy.close()


class ResponseTracker:
    """Retain response IDs and counts only. Never persist text or entire SSE frames."""
    def __init__(self, engine, job, instance, store, streaming=False):
        self.engine, self.job, self.instance, self.store = engine, job, instance, store
        self.buffer = b''
        self.streaming = streaming

    def accept(self, value):
        if not isinstance(value, dict):
            return
        response = value.get('response', value)
        if not isinstance(response, dict):
            return
        delta = value.get('delta')
        choices = value.get('choices') if isinstance(value.get('choices'), list) else []
        message = value.get('message', {})
        if ((str(value.get('type', '')).endswith('.delta') and delta) or
                any(isinstance(x, dict) and any(x.get('delta', {}).get(k) for k in ('content', 'reasoning', 'reasoning_content', 'tool_calls')) for x in choices if isinstance(x, dict) and isinstance(x.get('delta'), dict)) or
                (isinstance(message, dict) and any(message.get(k) for k in ('content', 'thinking', 'tool_calls')))):
            client = self.engine.clients.get(self.job['client'])
            if client is not None:
                client['generation_events'] = client.get('generation_events', 0) + 1
        ident = response.get('id')
        if self.store and self.instance['provider'] == 'lmstudio' and isinstance(ident, str) and len(ident) <= 256:
            if len(self.engine.responses) >= 8192:
                self.engine.responses.pop(next(iter(self.engine.responses)))
            self.engine.responses[ident] = {'client': self.job['client'], 'alias': self.instance['alias']}
        usage = response.get('usage', {})
        if isinstance(usage, dict):
            tokens = usage.get('output_tokens', usage.get('completion_tokens'))
            if type(tokens) is int and tokens >= 0:
                self.job['tokens'] = tokens

    def feed(self, chunk):
        self.buffer += chunk
        while self.streaming and b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            if line.startswith(b'data: '):
                try:
                    self.accept(json.loads(line[6:]))
                except ValueError:
                    pass
        if len(self.buffer) > LIMIT:
            self.buffer = b''

    def finish(self):
        try:
            self.accept(json.loads(self.buffer))
        except ValueError:
            pass
        self.buffer = b''


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def json_response(self, value, code=200):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        engine = self.server.engine
        parts = self.path.split('/', 2)
        token = parts[1] if len(parts) == 3 else ''
        client = next((c for c in list(engine.clients.values()) if hmac.compare_digest(c['token'], token)), None)
        if not client or self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):
            self.json_response({'error': 'Unknown local client'}, 403)
            return
        path = '/' + parts[2]
        chosen = client['chosen']
        try:
            if self.command == 'GET' and path in ('/v1/models', '/api/tags'):
                model = chosen['model']
                result = {'object': 'list', 'data': [{'id': model, 'object': 'model', 'owned_by': 'local'}]}
                if path == '/api/tags':
                    result = {'models': [{'name': model, 'model': model}]}
                self.json_response(result)
                return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= LIMIT or (self.command == 'POST' and not size):
                raise ValueError('Request body is missing or too large')
            payload = json.loads(self.rfile.read(size)) if size else {}
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object')
            if not ((self.command == 'POST' and path in ('/api/show', '/v1/responses', '/v1/chat/completions', '/api/chat', '/api/generate')) or (self.command == 'GET' and path == '/api/version')):
                raise ValueError('Unsupported managed inference route')
            if payload.get('model') not in (None, chosen['model']):
                raise ValueError('Client may only use its selected local model')
            job = engine.submit(client, path, payload, self)
            while not job['done'].wait(.25):
                if engine.stopping.is_set():
                    engine.cancel(job)
            if not job.get('headers_sent'):
                self.json_response({'error': job.get('error') or job['state']}, 503)
            job.pop('handler', None)
        except (OSError, ValueError, KeyError):
            try:
                self.json_response({'error': 'Managed inference request could not be accepted'}, 400)
            except OSError:
                pass


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.connection.settimeout(5)
            data = self.rfile.readline(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError('Control request exceeds limit')
            result = self.server.engine.control(json.loads(data))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {'control_error': str(exc) if isinstance(exc, ValueError) else 'Invalid local runtime control request'}
        self.wfile.write(json.dumps(result).encode() + b'\n')


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve():
    directory = private_directory(root())
    lockfd = os.open(directory / 'manager.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(lockfd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = directory / 'control.sock'
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError('Unsafe runtime socket path')
            path.unlink()
        engine = Engine()
        with ControlServer(str(path), ControlHandler) as control, ThreadingHTTPServer(('127.0.0.1', 0), ProxyHandler) as proxy:
            path.chmod(0o600)
            proxy.daemon_threads = True
            control.engine = proxy.engine = engine
            engine.port = proxy.server_port
            threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (control, proxy)]
            for thread in threads:
                thread.start()
            engine.start()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: engine.stopping.set())
            try:
                engine.stopping.wait()
            finally:
                engine.close()
                control.shutdown()
                proxy.shutdown()
                path.unlink(missing_ok=True)


class Client:
    def __init__(self, chosen, kind='session', activity=None, cancel=None):
        self.chosen, self.kind, self.activity = chosen, kind, activity
        self.cancel_event = cancel or threading.Event()
        self.closed = threading.Event()
        self.info = {}
        self.last = {'wait_seconds': 0, 'state': 'working'}
        self.error = None

    def __enter__(self):
        if self.info:
            return self  # Admission may precede snapshot handoff to the worker thread.
        ensure()
        self.info = rpc('register', chosen=self.chosen, pid=os.getpid(), kind=self.kind,
                        tmux=os.environ.get('TMUX', '').rsplit(',', 2)[0], pane=os.environ.get('TMUX_PANE', ''),
                        test_token=getattr(self.cancel_event, 'test_token', ''))
        if self.info.get('admitted') is False:
            admission = self.info['availability']
            self.info = {}
            raise AdmissionDenied(admission)
        self.base = self.info['base']
        self.cancel_event.client_id = self.info['id']
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self

    def call(self, action):
        return rpc(action, client=self.info['id'], token=self.info['token'])

    def heartbeat(self):
        while not self.closed.wait(.5):
            try:
                if self.cancel_event.is_set():
                    self.call('close')
                    return
                self.last = self.call('heartbeat')
                if self.last.get('interrupted') or (self.kind == 'worker' and self.last.get('outcome') in ('busy', 'timed_out')):
                    self.cancel_event.set()
                if self.activity:
                    state = self.last['state']
                    self.activity.update({'running': 'inferring', 'waiting_for_memory': 'waiting',
                                          'waiting_for_legacy': 'queued'}.get(state, state))
            except (OSError, ValueError) as exc:
                self.error = str(exc)
                self.cancel_event.set()  # Managed clients never fall back to a direct endpoint.
                return

    def waited(self):
        return self.last.get('wait_seconds', 0)

    def __exit__(self, *args):
        self.closed.set()
        self.thread.join(timeout=3)
        try:
            self.last = self.call('client')
            self.call('close')
        except (OSError, ValueError):
            pass


class AdmissionDenied(ValueError):
    def __init__(self, availability):
        self.availability = availability
        super().__init__(availability['reason'])


if __name__ == '__main__':
    if sys.argv[1:] == ['serve']:
        serve()
