"""Activity observes HTTP and pane ownership, never a real inference service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
from conftest import load_script

ROOT = Path(__file__).parents[1]


@pytest.fixture
def status(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.delenv('TMUX', raising=False)
    return load_script(ROOT / 'libexec/t_local_status.py', 'status_test')


def info(pane='%1', pid=123):
    return [pane, str(pid), '$2', 'Parser fix | t-13', '/work/t/t-13', 'codex']


def test_ownership_and_read_only_render(status, tmp_path, monkeypatch):
    m = status
    monkeypatch.setattr(m, 'tmux', lambda *a: SimpleNamespace(returncode=0, stdout='\t'.join(info())+'\n'))
    assert m.pane_info('/socket', '%1') == info()
    assert m.pane_info('/socket', 'bad') is None
    assert m.pane_info('/socket', '%2') is None
    assert m.render('left', tmp_path, '/socket', '%1') == 'Parser fix t/t-13 '
    assert m.render('right', tmp_path, '/socket', '%1') == ''
    path = m.record_path(tmp_path, '/socket', '%1')
    row = dict(pane_pid=123, updated=time.time(), model='publisher/Qwen#test', state='inferring')
    path.write_text(json.dumps(row))
    before = path.stat().st_mtime_ns
    assert m.render('right', tmp_path, '/socket', '%1') == 'local Qwen##test · inferring '
    assert path.stat().st_mtime_ns == before
    for change in ({'pane_pid':124}, {'updated':time.time()-20}, {'updated':time.time()+20}, {'state':'invalid'}):
        path.write_text(json.dumps(dict(row, **change)))
        assert m.render('right', tmp_path, '/socket', '%1') == ''
    assert m.record_path(tmp_path, '/other', '%1') != path
    assert m.render('right', tmp_path, '/socket', '%2') == ''
    assert m.safe('a\x1b\n#', 20) == 'a##'
    parents = iter(['20', '1'])
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=next(parents)))
    assert m.ancestry(21, 20)
    assert not m.ancestry(21, 19)


def test_install_preserves_custom_status_and_is_idempotent(status, tmp_path, monkeypatch):
    m = status; values = {'status-left': '[#S] ', 'status-right': 'my clock', 'status-left-length':'10', 'status-right-length':'90', 'status-interval':'15'}
    calls = []
    def tmux(socket, *args):
        calls.append(args)
        if args[0] == 'set-option':
            values[args[-2]] = args[-1]; return SimpleNamespace(returncode=0, stdout='')
        return SimpleNamespace(returncode=0, stdout=values[args[-1]])
    monkeypatch.setattr(m, 'tmux', tmux)
    m.install('/private/socket', '$1', tmp_path)
    assert '[#S]' not in values['status-left'] and 'pane_id' in values['status-left']
    assert values['status-right'].endswith('my clock') and values['status-right-length'] == '90'
    installed = dict(values)
    m.install('/private/socket', '$1', tmp_path)
    assert values == installed
    values['status-left'] = 'Recovery: Ctrl-R '
    m.install('/private/socket', '$1', tmp_path)
    assert values['status-left'].endswith('Recovery: Ctrl-R ')
    monkeypatch.setattr(m, 'tmux', lambda *a: SimpleNamespace(returncode=1, stdout=''))
    m.install('/socket', '$1', tmp_path)


def test_activity_is_optional_and_heartbeat_expires(status, tmp_path, monkeypatch):
    m = status
    with m.Activity('model') as activity:
        assert activity.path is None
        with activity.endpoint('http://127.0.0.1:12') as endpoint:
            assert endpoint == 'http://127.0.0.1:12'
    monkeypatch.setenv('TMUX', '/socket,1,2'); monkeypatch.setenv('TMUX_PANE', '%1')
    monkeypatch.setattr(m, 'pane_info', lambda *a: info())
    monkeypatch.setattr(m, 'ancestry', lambda *a: False)
    with m.Activity('model') as activity: assert activity.path is None
    monkeypatch.setattr(m, 'ancestry', lambda *a: True)
    monkeypatch.setattr(m, 'install', lambda *a: None)
    with m.Activity('model') as activity:
        assert json.loads(activity.path.read_text())['state'] == 'loading'
        activity.request(1); activity.request(1); activity.request(-1)
        assert json.loads(activity.path.read_text())['state'] == 'inferring'
        activity.request(-1)
        assert json.loads(activity.path.read_text())['state'] == 'working'
        # Run one heartbeat without slowing the suite.
        waits = iter([False, True])
        activity.stop.set()
        activity.thread.join()
        monkeypatch.setattr(activity.stop, 'wait', lambda *a: next(waits))
        activity.heartbeat()
        with activity.endpoint('https://remote.invalid') as endpoint: assert endpoint == 'https://remote.invalid'
    assert json.loads(activity.path.read_text())['state'] == 'idle'
    assert activity.path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(m, 'pane_info', lambda *a: (_ for _ in ()).throw(OSError('gone')))
    with m.Activity('model') as unowned: assert unowned.path is None


def test_http_stream_activity_and_failures(status, tmp_path):
    m = status; observed = []; release = threading.Event(); entered = threading.Event()
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
        def do_POST(self):
            observed.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            entered.set(); release.wait(3)
            self.send_response(500 if self.path == '/api/generate' else 200)
            self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(b'data: fixture\n\n'); self.wfile.flush()
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    activity = m.Activity('model'); activity.path = tmp_path/'activity.json'; activity.pane_pid=123
    finished = threading.Event()
    original_request = activity.request
    def record_request(delta, failed=False):
        original_request(delta, failed)
        if delta < 0:
            finished.set()
    activity.request = record_request
    try:
        with activity.endpoint(f'http://127.0.0.1:{upstream.server_port}') as endpoint:
            assert urllib.request.urlopen(endpoint+'/v1/models').read() == b'{"data":[]}'
            assert activity.state == 'working'
            answer = []
            def request():
                answer.append(urllib.request.urlopen(urllib.request.Request(endpoint+'/v1/responses', data=b'{"model":"pinned"}')).read())
            thread = threading.Thread(target=request); thread.start()
            assert entered.wait(2) and activity.state == 'inferring'
            release.set(); thread.join(3)
            assert finished.wait(2)
            assert answer == [b'data: fixture\n\n'] and observed[0][1] == b'{"model":"pinned"}'
            assert activity.state == 'working'
            finished.clear()
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(urllib.request.Request(endpoint+'/api/generate', data=b'{}'))
            assert finished.wait(2)
            assert activity.state == 'failed'
            finished.clear()
            with pytest.raises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(urllib.request.Request(endpoint+'/v1/responses', data=b'{}', headers={'Content-Length':str(20*1024**2)}))
            assert rejected.value.code == 413
            assert finished.wait(2)
        assert not activity.active
    finally:
        upstream.shutdown(); upstream.server_close()
