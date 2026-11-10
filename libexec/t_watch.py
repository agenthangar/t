"""Bounded observation and cached, verified session watcher activity.

Outcomes remain in provider receipts. This display cache never implies agent
acknowledgement, changes ownership, or polls a provider from a list command.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import socket
import subprocess
import sys
import threading
import time
import uuid

TTL = 15
TERMINAL = {'succeeded', 'failed', 'timed_out', 'cancelled', 'error', 'completed', 'inactivity_timed_out', 'output_limit'}


def helper(name):
    return runpy.run_path(str(Path(__file__).with_name(name + '.py')))


def root():
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 't/local-activity/watchers'


def owner():
    """Require the hook's exact conversation ID plus current process ancestry."""
    local = helper('t_local_status')
    transport = os.environ.get('TMUX', '').rsplit(',', 2)[0]
    pane = os.environ.get('TMUX_PANE', '')
    if not transport.startswith('/'):
        return None
    info = local['pane_info'](transport, pane)
    if not info or not local['ancestry'](os.getpid(), int(info[1])):
        return None
    value = local['tmux'](transport, 'show-environment', '-t', info[2], 'CLAUDE_RESUME_ID').stdout.strip()
    sid = value.removeprefix('CLAUDE_RESUME_ID=')
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sid):
        return None
    registry = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'claude-sessions'
    pid, verified = os.getpid(), False
    for _ in range(64):
        record = registry / str(pid)
        if record.is_file() and record.read_text().split('\t', 1)[0] == sid:
            verified = True
            break
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid='], capture_output=True, text=True, timeout=2)
        if not result.stdout.strip().isdigit() or int(result.stdout) <= 1:
            break
        pid = int(result.stdout)
    if not verified:
        return None  # A stale pane environment must not claim the preceding session.
    local['install'](transport, info[2], local['root']())
    return {'session_id': sid, 'host': socket.gethostname(), 'socket': transport,
            'pane': pane, 'pane_pid': int(info[1])}


def records(sid, folder=None, now=None):
    if not sid or sid == '-':
        return []
    now = time.time() if now is None else now
    found = []
    directory = (Path(folder) if folder else root()) / hashlib.sha256(sid.encode()).hexdigest()
    for path in directory.glob('*.json'):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                continue
            if data.get('session_id') != sid or data.get('host') != socket.gethostname():
                continue
            if not re.fullmatch(r'watch-[0-9a-f]{12}', data.get('id', '')):
                continue
            if any(not isinstance(data.get(key), (int, float)) for key in ('created', 'updated')):
                continue
            if data.get('active') and not 0 <= now - data['updated'] <= TTL:
                data = dict(data, state='stale', active=False)
            found.append(data)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(found, key=lambda item: (item.get('created', 0), item['id']))


def label(items):
    visible = [item for item in items if isinstance(item.get('id'), str) and
               (item.get('active') or item.get('state') == 'stale')]
    if not visible:
        return '—'
    first = visible[0]
    return ('active ' if first.get('active') else 'stale ') + first['id'] + (f' +{len(visible)-1}' if len(visible) > 1 else '')


class Activity:
    def __init__(self, run_id, kind='runner', next_step='', identity=None):
        try:
            self.identity = owner() if identity is None else identity
        except (OSError, ValueError, subprocess.SubprocessError):
            self.identity = None
        self.stop = threading.Event()
        self.thread = None
        self.cache_warning = False
        self.data = dict(self.identity or {}, id='watch-' + uuid.uuid4().hex[:12], run_id=run_id,
                         kind=kind, next_step=next_step, created=time.time(), active=True, state='waiting')

    def update(self, state='waiting', **values):
        self.data.update(values, state=state, updated=time.time())
        if self.identity:
            try:
                folder = root() / hashlib.sha256(self.data['session_id'].encode()).hexdigest()
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = folder / (self.data['id'] + '.json')
                temporary = path.with_suffix('.tmp')
                with open(temporary, 'w', encoding='utf-8') as stream:
                    os.chmod(temporary, 0o600)
                    json.dump(self.data, stream)
                os.replace(temporary, path)
            except OSError as exc:
                if not self.cache_warning:
                    print(f't watch: activity display unavailable: {exc}', file=sys.stderr)
                    self.cache_warning = True

    def finish(self, result=None):
        self.stop.set()
        if self.thread:
            self.thread.join()
        self.update('result ready' if result and result.get('status') in TERMINAL else 'stopped',
                    active=False, outcome=(result or {}).get('status', 'pending'),
                    snapshot=(result or {}).get('snapshot'), head=(result or {}).get('head'))

    def start(self):
        if self.thread is not None:
            return self
        self.update('watching ' + self.data['kind'])
        def heartbeat():
            while not self.stop.wait(2):
                self.update('watching ' + self.data['kind'])
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self


def wait(read, timeout=60, progress=None, activity=None, interval=1):
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('watch timeout must be 1..3600 seconds')
    deadline, previous = time.monotonic() + timeout, None
    result = None
    try:
        if activity:
            activity.start()
        while True:
            result = read()
            signature = json.dumps(result, sort_keys=True, default=str)
            if signature != previous and progress:
                progress(result)
            previous = signature
            if result.get('status') in TERMINAL:
                return dict(result, wait_status='complete', watcher_id=activity.data['id'] if activity else None, next_step=activity.data['next_step'] if activity else '')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(result, wait_status='pending', watcher_id=activity.data['id'] if activity else None, next_step=activity.data['next_step'] if activity else '')
            time.sleep(min(max(interval, result.get('retry_after', 0)), remaining))
    finally:
        if activity:
            activity.finish(result)


def runner(job_id, timeout=60, progress=None, next_step=''):
    remote = re.fullmatch(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)@([0-9a-f]{40})', job_id)
    if remote:
        return github(remote[1], int(remote[2]), remote[3], timeout, progress, next_step)
    runners = helper('t_runners')
    receipt = runners['status'](job_id)
    kind = 'local tests' if all(target.get('type') == 'local' for target in receipt['targets'].values()) else 'remote checks'
    return wait(lambda: runners['status'](job_id), timeout, progress, Activity(job_id, kind, next_step))


def github(repository, number, sha, timeout=60, progress=None, next_step=''):
    """Pin the expected PR head; never accept an older head's green checks."""
    deadline = time.monotonic() + timeout
    def read():
        try:
            output = subprocess.run(['gh', 'pr', 'view', str(number), '--repo', repository,
                                     '--json', 'headRefOid,statusCheckRollup'], capture_output=True, text=True,
                                    timeout=max(.1, min(20, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            return {'id': f'{repository}#{number}@{sha}', 'status': 'running', 'unavailable': 'GitHub request timed out'}
        if output.returncode:
            if 'rate limit' in output.stderr.lower():
                return {'id': f'{repository}#{number}@{sha}', 'status': 'running',
                        'unavailable': 'GitHub rate limit', 'retry_after': 30}
            raise ValueError(output.stderr.strip() or 'GitHub checks unavailable')
        data = json.loads(output.stdout)
        if data['headRefOid'] != sha:
            return {'id': f'{repository}#{number}@{sha}', 'status': 'error', 'exit_code': 1,
                    'error': 'PR head changed; restart validation', 'head': data['headRefOid']}
        checks = data.get('statusCheckRollup') or []
        pending = not checks or any(item.get('status') not in (None, 'COMPLETED') or item.get('state') == 'PENDING' for item in checks)
        failed = any(item.get('conclusion') not in (None, '', 'SUCCESS', 'NEUTRAL', 'SKIPPED') or item.get('state') in ('FAILURE', 'ERROR') for item in checks)
        return {'id': f'{repository}#{number}@{sha}', 'status': 'running' if pending else 'failed' if failed else 'succeeded',
                'exit_code': 1 if failed else 0, 'head': sha, 'checks': checks}
    return wait(read, timeout, progress, Activity(f'{repository}#{number}@{sha}', 'remote checks', next_step), interval=10)
