"""Bounded local tmux observations for browsing. Rich metadata is optional."""
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time


def socket_path():
    inherited = os.environ.get('TMUX', '').rsplit(',', 2)
    if len(inherited) == 3 and os.path.isabs(inherited[0]):
        return os.path.realpath(inherited[0])
    return os.path.realpath(Path(os.environ.get('TMUX_TMPDIR') or '/tmp') /
                            f'tmux-{os.getuid()}' / 'default')


def notify_ready(address=None):
    if os.environ.get('T_DISCOVERY_COLLECTOR') == '1':
        return
    try:
        if address is None:
            import runpy
            cache = runpy.run_path(str(Path(__file__).with_name('t_session_cache.py')))
            address = cache['_runtime']() / 'ready.sock'
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.sendto(b'R', str(address))
    except OSError:
        # A cold cache is observed on first browse, and a full socket already
        # has a scan queued. Publication never holds a worker waiting for either.
        pass


def scan():
    # A single tmux invocation collects all slots. The stamped SID only matches
    # already verified metadata. It is never published as proof of ownership.
    path = socket_path()
    fmt = '|'.join(('#{socket_path}', '#{pid}', '#{session_id}', '#{session_name}',
                    '#{?session_attached,attached,detached}', '#{DEV_AGENT}',
                    '#{CLAUDE_RESUME_ID}', '#{session_path}'))
    proc = subprocess.run(['tmux', 'list-sessions', '-F', fmt], capture_output=True,
                          text=True, errors='replace', stdin=subprocess.DEVNULL, timeout=5)
    if proc.returncode:
        if not os.environ.get('TMUX') and ('no server running on ' in proc.stderr or
                (not os.path.exists(path) and 'No such file or directory' in proc.stderr)):
            return path, {}
        raise ValueError(f'tmux list-sessions exited {proc.returncode}: {proc.stderr.strip()}')
    slots = {}
    for line in proc.stdout.splitlines():
        parts = line.split('|', 7)
        if len(parts) != 8:
            raise ValueError('incomplete tmux inventory response')
        actual, pid, terminal, session, state, agent, sid, cwd = parts
        if os.path.realpath(actual) != path:
            raise ValueError(f'tmux socket mismatch: expected {path}, observed {actual}')
        if re.fullmatch(r'dev-.+-[0-9]+', session):
            slots[session[4:]] = dict(terminal=pid + '/' + terminal, cwd=cwd, state=state,
                                      agent=agent or 'claude', stamp=sid)
    return path, slots


class Inventory:
    def __init__(self, report):
        self.report = report
        self.slots = None
        self.checked = self.attempt = self.duration = 0
        self.socket = socket_path()
        self.error = ''
        self.busy = self.again = False
        self.revision = 0
        self.closed = set()
        self.lock = threading.RLock()

    def request(self, *, explicit=False, changed=False):
        with self.lock:
            if self.busy:
                self.again |= changed
            elif explicit or changed or time.time() - self.attempt >= 5:
                self.busy = True
                threading.Thread(target=self._run, daemon=True).start()
            return self.revision + int(self.busy) + int(self.again)

    def close(self, label):
        with self.lock:
            if self.slots is not None:
                self.slots.pop(label, None)
            if self.busy:
                self.closed.add(label)
            self.request(changed=True)

    def _run(self):
        while True:
            started = time.monotonic()
            try:
                path, slots = scan()
                error = ''
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                error = ('tmux inventory timed out after 5s' if isinstance(exc, subprocess.TimeoutExpired)
                         else str(exc))
            with self.lock:
                self.attempt = time.time()
                self.duration = round(time.monotonic() - started, 3)
                self.error = error
                if not error:
                    for label in self.closed:
                        slots.pop(label, None)
                    self.socket, self.slots, self.checked = path, slots, self.attempt
                self.closed.clear()
                status = dict(socket=self.socket, count=len(self.slots) if self.slots is not None else None,
                              checked=self.checked, attempt=self.attempt, duration=self.duration, error=error,
                              retry_at=self.attempt + 5 if error else 0)
            self.report(status)
            with self.lock:
                # Complete the ticket and release its worker in one transition.
                self.revision += 1
                if not self.again:
                    self.busy = False
                    return
                self.again = False

    def terminals(self):
        with self.lock:
            return {label: slot['terminal'] for label, slot in (self.slots or {}).items()}

    def project(self, host, enriched=None):
        with self.lock:
            host['live_revision'] = self.revision
            host['live_pending'] = self.busy
            host['live_error'] = self.error
            if self.error:
                host.update(error=self.error, error_kind='collector', stale=True)
            if self.slots is None:
                return
            value = host.get('value') or {}
            numbered = {}
            rows = []
            for line in value.get('rows', '').splitlines():
                fields = line.split('\t')
                if len(fields) >= 7 and fields[3] in ('attached', 'detached') and re.fullmatch(r'.+-[0-9]+', fields[2]):
                    numbered[fields[2]] = fields
                else:
                    rows.append(line)
            for label, slot in self.slots.items():
                fields = numbered.get(label)
                if not (fields and fields[1] == slot['cwd'] and (
                        (enriched or {}).get(label) == slot['terminal'] or
                        (fields[0] != '-' and fields[0] == slot['stamp']))):
                    fields = ['-', slot['cwd'], label, slot['state'], 'unknown', '-', slot['agent']]
                fields[3] = slot['state']
                rows.append('\t'.join(fields))
            host['value'] = dict(value, rows=''.join(row+'\n' for row in rows))
            host['terminals'] = self.terminals()
            host['live_checked'] = self.checked
