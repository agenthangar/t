"""Pane-owned local inference activity. No prompts, tokens, or credentials retained."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

TTL = 12


def tmux(socket, *args):
    return subprocess.run(['tmux', '-S', socket, *args], capture_output=True, text=True, timeout=3)


def ancestry(pid, ancestor):
    for _ in range(64):
        if pid == ancestor:
            return True
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid='], capture_output=True, text=True, timeout=2)
        parent = result.stdout.strip()
        if not parent.isdigit() or int(parent) <= 1 or int(parent) == pid:
            break
        pid = int(parent)
    return False


def pane_info(socket, pane):
    if not re.fullmatch(r'%\d+', pane):
        return None
    result = tmux(socket, 'display-message', '-p', '-t', pane,
                  '#{pane_id}\t#{pane_pid}\t#{session_id}\t#{pane_title}\t#{pane_current_path}\t#{window_name}')
    fields = result.stdout.rstrip('\n').split('\t')
    return fields if result.returncode == 0 and len(fields) == 6 and fields[0] == pane and fields[1].isdigit() else None


def root():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 't/local-activity'


def record_path(folder, socket, pane):
    return Path(folder) / (hashlib.sha256((socket + '\0' + pane).encode()).hexdigest() + '.json')


def safe(text, limit):
    return ''.join(c for c in str(text) if c.isprintable())[:limit].replace('#', '##')


def render(side, folder, socket, pane):
    try:
        info = pane_info(socket, pane)
        if not info:
            return ''
        if side == 'left':
            title = info[3].rpartition(' | ')[0] or info[5]
            cwd = Path(info[4])
            return safe(title, 24) + ' ' + safe(cwd.parent.name + '/' + cwd.name, 24) + ' '
        data = json.loads(record_path(folder, socket, pane).read_text())
        if (data['pane_pid'] != int(info[1]) or not 0 <= time.time() - data['updated'] <= TTL
                or data['state'] not in ('loading', 'inferring', 'working', 'idle', 'failed')):
            return ''
        label = re.sub(r'(?i)-(?:mlx-)?\d+bit$', '', data['model'].split('/')[-1])
        return 'local ' + safe(label, 35) + ' · ' + data['state'] + ' '
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return ''


def install(socket, session, folder):
    """Add only our fragments; preserve native Codex and custom tmux content."""
    interval = tmux(socket, 'show-options', '-A', '-v', '-t', session, 'status-interval').stdout.strip()
    if interval.isdigit() and (int(interval) > 2 or int(interval) == 0):
        tmux(socket, 'set-option', '-t', session, 'status-interval', '2')
    prefix = shlex.join([sys.executable, str(Path(__file__).resolve()), 'render'])
    for side, width in (('left', 50), ('right', 70)):
        option = 'status-' + side
        result = tmux(socket, 'show-options', '-A', '-v', '-t', session, option)
        if result.returncode or 't_local_status.py' in result.stdout:
            continue
        original = result.stdout.rstrip('\n')
        fragment = '#(' + prefix + ' ' + shlex.join([side, str(folder), socket]) + " '#{pane_id}')"
        # The default session label is superseded by name + worktree. Custom
        # content (including recovery hints) is retained after the new fragment.
        if side == 'left' and original == '[#S] ':
            original = ''
        tmux(socket, 'set-option', '-t', session, option, fragment + original)
        previous = tmux(socket, 'show-options', '-A', '-v', '-t', session, option + '-length').stdout.strip()
        if previous.isdigit():
            tmux(socket, 'set-option', '-t', session, option + '-length', str(max(width, int(previous))))


class Activity:
    def __init__(self, model):
        self.model, self.state = model, 'loading'
        self.path = None
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.active = 0
        self.failed = False

    def __enter__(self):
        try:
            socket = os.environ.get('TMUX', '').rsplit(',', 2)[0]
            pane = os.environ.get('TMUX_PANE', '')
            info = pane_info(socket, pane) if socket.startswith('/') else None
            if info and ancestry(os.getpid(), int(info[1])):
                folder = root()
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.path = record_path(folder, socket, pane)
                self.pane_pid = int(info[1])
                install(socket, info[2], folder)
                self.write()
                self.thread = threading.Thread(target=self.heartbeat, daemon=True)
                self.thread.start()
        except (OSError, ValueError, subprocess.SubprocessError):
            self.path = None  # Observability must never break inference.
        return self

    def write(self):
        if self.path:
            temp = self.path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            try:
                temp.write_text(json.dumps({'model': self.model, 'state': self.state,
                                           'pane_pid': self.pane_pid, 'updated': time.time()}))
                temp.chmod(0o600)
                temp.replace(self.path)
            except OSError:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def heartbeat(self):
        while not self.stop.wait(2):
            with self.lock:
                self.write()

    def update(self, state):
        with self.lock:
            self.state = state
            self.write()

    def request(self, delta, failed=False):
        with self.lock:
            self.active += delta
            self.failed = self.failed or failed
            self.state = 'inferring' if self.active else 'failed' if self.failed else 'working'
            self.write()

    @contextmanager
    def endpoint(self, base):
        if not self.path:
            yield base
            return
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password:
            yield base
            return
        activity = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                inference = self.command == 'POST' and self.path.split('?')[0] in ('/v1/responses', '/v1/chat/completions', '/api/chat', '/api/generate')
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1800)
                failed = False
                if inference:
                    activity.request(1)
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 <= size <= 16 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                        self.send_error(413)
                        return
                    body = self.rfile.read(size) if size else None
                    connection.request(self.command, parsed.path.rstrip('/') + self.path, body,
                                       {k: v for k, v in self.headers.items() if k.lower() in ('content-type', 'accept')})
                    response = connection.getresponse()
                    failed = response.status >= 400
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() in ('content-type', 'content-length'):
                            self.send_header(key, value)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, ValueError, http.client.HTTPException):
                    failed = True
                    self.close_connection = True
                finally:
                    connection.close()
                    self.close_connection = True
                    if inference:
                        activity.request(-1, failed)
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except OSError:
            yield base
            return
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.update('working')
        try:
            yield 'http://127.0.0.1:' + str(server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.update('failed' if kind or self.failed else 'idle')


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == 'render':
        print(render(*sys.argv[2:]), end='')
