"""Explicit per-user periodic advisory worker; no system service installation."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import signal
import stat
import subprocess
import sys
import threading
import time

HELPERS = runpy.run_path(str(Path(__file__).with_name('t_vulnerabilities.py')))
METADATA = runpy.run_path(str(Path(__file__).with_name('t_tool_inventory.py')))


def directory():
    base = os.environ.get('XDG_STATE_HOME', '')
    if not os.path.isabs(base):
        base = os.path.expanduser('~/.local/state')
    return Path(base) / 't/advisories'


def load(name):
    try:
        return METADATA['document'](directory() / name)
    except (OSError, ValueError):
        return {}


def save(name, data):
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    HELPERS['write'](root / name, data)


def running():
    """Read-only ownership check; do not trust or signal a cached PID."""
    try:
        fd = os.open(directory() / 'worker.lock', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                return False
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        pass
    return False


def configuration():
    config = load('service.json')
    if config.get('enabled') is True and type(config.get('interval')) is int and 3600 <= config['interval'] <= 2592000:
        return config
    return {}


def enabled():
    return bool(configuration())


def enable(hours):
    if type(hours) is not int or not 1 <= hours <= 720:
        raise ValueError('interval must be 1–720 hours')
    save('service.json', {'enabled': True, 'interval': hours * 3600})
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, cwd='/', env=env)
    return 'Periodic advisories enabled; worker requested. Verify with t system advisories status.'


def disable():
    save('service.json', {'enabled': False})
    return 'Periodic advisories disabled; worker stops after its current bounded request. Startup env opt-in is separate.'


def record_changes(data):
    if not data or data.get('error'):
        return
    # Coverage changes/removals must not be reported as vulnerability resolution.
    findings = sorted(line for line in data['lines'] if 'OSV version match' in line)
    digest = hashlib.sha256(json.dumps(findings).encode()).hexdigest()
    old = load('notices.json')
    if digest == old.get('fingerprint'):
        return
    events = old.get('events', [])
    if not isinstance(events, list):
        events = []
    if findings or old.get('findings'):
        events = (events + [{'at': data['checked'], 'message': 'Findings changed; review current coverage (removal is not proof of remediation).',
                             'findings': findings[:20]}])[-10:]
    save('notices.json', {'fingerprint': digest, 'findings': findings[:20], 'events': events})


def daemon(stop=None):
    stop = stop or threading.Event()
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(root / 'worker.lock', 'a') as lock:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Allow a just-disabled worker to drain its bounded request before
                # a rapid re-enable; an already-active owner keeps its lock.
                if stop.is_set() or not enabled() or time.monotonic() >= deadline:
                    return
                stop.wait(.2)
        while not stop.is_set():
            config = configuration()
            if not config:
                break
            interval = config['interval']
            cached = HELPERS['read'](HELPERS['cache_path']())
            delay = min(interval, 3600) if cached.get('error') else interval
            now = time.time()
            if cached.get('checked', 0) > now or now >= cached.get('checked', 0) + delay:
                # Explicit enable is itself opt-in; no inherited environment flag required.
                data = HELPERS['check'](force=True, interval=interval,
                                        cancelled=lambda: stop.is_set() or not enabled(), authorized=True)
                record_changes(data)
            stop.wait(1)


def status():
    config = configuration()
    state = 'running' if running() else 'stopped'
    lines = [f"Periodic advisories: {'enabled' if config else 'disabled'}; worker {state}."]
    if config:
        lines.append(f"Interval: {config['interval'] // 3600} hours; no boot/login service installed.")
    data = HELPERS['read'](HELPERS['cache_path']())
    if data:
        stale = data['expires'] < time.time()
        lines.append(f"Report: {'stale' if stale else 'cached'}; last attempt Unix time {data['checked']:.0f}.")
        lines.extend(data['lines'])
    else:
        lines.append('No cached inventory/advisory results; coverage unknown.')
    events = load('notices.json').get('events', [])
    if isinstance(events, list):
        for event in events[-5:]:
            if isinstance(event, dict):
                lines.append(f"Notice {event.get('at', '?')}: {event.get('message', '')}")
    return '\n'.join(''.join(c for c in line if c.isprintable()) for line in lines)


if __name__ == '__main__':
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with contextlib.suppress(OSError, ValueError):
        daemon(stop)
