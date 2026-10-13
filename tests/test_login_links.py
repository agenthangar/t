"""Synthetic terminal login output only; never invoke real vendor credentials."""
import errno
import io
import os
import select
import signal
import subprocess
import sys
import time
import termios
from types import SimpleNamespace

import pytest

from test_login import ROOT, cli_sandbox


URL = b'https://example.invalid/oauth?state=fixture&redirect_uri=http%3A%2F%2Flocalhost'
LINK = b'  \x1b]8;;' + URL + b'\x1b\\\x1b[4;36mOpen sign-in page (Cmd-click)\x1b[0m\x1b]8;;\x1b\\\r\n'


def test_links_across_every_chunk_boundary_and_unterminated_prompt(t_mod):
    vendor = b'Visit ' + URL + b'\nPaste code: '
    expected = b'Visit ' + URL + b'\n' + LINK + b'Paste code: '
    for at in range(len(vendor) + 1):
        links = t_mod._LoginLinks()
        result = links.feed(vendor[:at]) + links.feed(vendor[at:]) + links.finish()
        assert result == expected


def test_link_output_preserves_ansi_unicode_and_existing_links(t_mod):
    original = b'\x1b[32mVisit \x1b]8;;' + URL + b'\x07' + URL + b'\x1b]8;;\x07\x1b[0m\r\n'
    links = t_mod._LoginLinks()
    assert links.feed(original) == original + LINK
    assert links.feed('Paste code → '.encode()) == 'Paste code → '.encode()
    assert links.finish() == b''


def test_links_flush_final_url_and_ignore_invalid_or_oversized_lines(t_mod):
    links = t_mod._LoginLinks()
    assert links.feed(URL) == URL
    assert links.finish() == b'\n' + LINK
    assert links.finish() == b''
    for text in [b'https://\n', b'https://[bad/\n', b'https://example.invalid/' + b'x' * 17000 + b'\n']:
        assert links.feed(text) == text
    assert links.feed(URL + b'\n') == URL + b'\n' + LINK


def test_redirected_login_output_is_inherited_without_hyperlinks(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    sentinel = SimpleNamespace(returncode=7)
    calls = []
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda cmd: calls.append(cmd) or sentinel)
    assert t_mod._login_run(['fixture', 'login']) is sentinel
    assert calls == [['fixture', 'login']]


@pytest.mark.parametrize('mode', ['remote', 'local', 'hidden', 'ctrl-c', 'sigterm', 'outer-noecho', 'headless'])
@pytest.mark.parametrize('answer,expected', [('fixture-code\n', 0), ('fail\n', 1), (None, 130)])
def test_login_terminal_links_input_exit_and_interrupt(tmp_path, answer, expected, mode):
    env = cli_sandbox(tmp_path)
    if mode == 'sigterm' and answer is None: expected = 143
    if mode not in ('local', 'headless'): env['SSH_CONNECTION'] = 'fixture'
    if mode == 'hidden': env['HIDE_INPUT'] = '1'
    vendor = tmp_path / 'bin/claude'
    vendor.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time, termios
root = pathlib.Path(os.environ['HOME'])
if 'status' in sys.argv:
    print(json.dumps({'loggedIn': (root / 'signed-in').exists()}))
    sys.exit(0)
print('TTY:' + ','.join(str(os.isatty(fd)) for fd in (0, 1, 2)), flush=True)
assert len({os.ttyname(fd) for fd in (0, 1, 2)}) == 1
with open('/dev/tty') as terminal: assert os.isatty(terminal.fileno())
os.write(1, b'Visit https://example.invalid/oauth?state=')
time.sleep(.05)
os.write(2, b'fixture&redirect_uri=http%3A%2F%2Flocalhost\\n')
if os.environ.get('HIDE_INPUT'):
    attrs = termios.tcgetattr(0); attrs[3] &= ~termios.ECHO
    termios.tcsetattr(0, termios.TCSANOW, attrs)
print('Paste code: ', end='', flush=True)
answer = input()
if answer != 'fixture-code': sys.exit(7)
(root / 'signed-in').touch()
''')
    master, slave = os.openpty()
    process = None
    if mode == 'outer-noecho':
        attrs = termios.tcgetattr(slave); attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
    saved = termios.tcgetattr(master)
    try:
        process = subprocess.Popen([sys.executable, str(ROOT / 'bin/t'), 'login', 'claude', '--force', '-y', *(['--headless'] if mode == 'headless' else [])],
                                   env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = None
        output, answered = b'', False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    assert exc.errno == errno.EIO
                    break
                if not data:
                    break
                output += data
            if b'Paste code: ' in output and not answered:
                assert b'TTY:True,True,True' in output
                if mode == 'local':
                    assert b'http://127.0.0.1:' in output and b'Cmd-double-click in Terminal' in output
                else:
                    assert b'\x1b]8;;' + URL + b'\x1b\\\x1b[4;36mOpen sign-in page' in output
                if answer is None:
                    if mode == 'sigterm': os.kill(process.pid, signal.SIGTERM)
                    elif mode == 'ctrl-c': os.write(master, b'\x03')
                    else: os.killpg(process.pid, signal.SIGINT)
                else:
                    os.write(master, answer.rstrip('\n').encode())
                    end = time.monotonic() + 2
                    feedback = b'Input received; press Enter to submit.' if mode == 'hidden' else answer.rstrip('\n').encode()
                    while feedback not in output and time.monotonic() < end:
                        if select.select([master], [], [], .1)[0]:
                            output += os.read(master, 65536)
                    assert feedback in output, 'pasted input needs feedback before Enter'
                    if mode == 'hidden': assert answer.rstrip('\n').encode() not in output
                    os.write(master, b'\r')
                answered = True
        assert answered, output
        assert process.wait(timeout=2) == expected, output
        assert termios.tcgetattr(master) == saved
        assert (tmp_path / 'signed-in').exists() is (expected == 0)
        assert output.count(b'Open sign-in page') == 1
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)
        if slave is not None:
            os.close(slave)


@pytest.mark.parametrize('read_error', [errno.EIO, errno.EBADF])
def test_login_relay_cleans_up_child_and_restores_resize_handler(t_mod, monkeypatch, read_error):
    master, slave = os.openpty()
    stream = SimpleNamespace(isatty=lambda: True, fileno=lambda: slave,
                             flush=lambda: None, buffer=io.BytesIO())
    monkeypatch.setattr(t_mod.sys, 'stdin', stream)
    monkeypatch.setattr(t_mod.sys, 'stdout', stream)
    events = []
    monkeypatch.setenv('SSH_CONNECTION', 'fixture')
    monkeypatch.setattr(t_mod.select, 'select', lambda fds, *a: ([fds[0]], [], []))
    monkeypatch.setattr(t_mod.os, 'killpg', lambda pid, sig: events.append(sig))
    class Child:
        pid = 4242
        def poll(self): return None
        def terminate(self): events.append('terminate')
        def kill(self): events.append('kill')
        def wait(self, timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return 0
    monkeypatch.setattr(t_mod.subprocess, 'Popen', lambda *a, **kw: Child())
    def fail_read(*args):
        raise OSError(read_error, 'fixture')
    monkeypatch.setattr(t_mod.os, 'read', fail_read)
    previous = signal.getsignal(signal.SIGWINCH)
    try:
        if read_error == errno.EIO:
            assert t_mod._login_run(['fixture']).returncode == 0
        else:
            with pytest.raises(OSError):
                t_mod._login_run(['fixture'])
        assert events == [signal.SIGTERM, 'kill']
        assert signal.getsignal(signal.SIGWINCH) == previous
    finally:
        os.close(master)
        os.close(slave)


def test_login_relay_spawn_failure_closes_pty(t_mod, monkeypatch):
    master, slave = os.openpty()
    monkeypatch.setattr(t_mod.os, 'openpty', lambda: (master, slave))
    monkeypatch.setattr(t_mod.sys, 'stdin', SimpleNamespace(isatty=lambda: True, fileno=lambda: slave))
    monkeypatch.setattr(t_mod.sys, 'stdout', SimpleNamespace(
        isatty=lambda: True, fileno=lambda: slave, flush=lambda: None))
    def fail(*args, **kwargs):
        raise OSError('fixture missing')
    monkeypatch.setattr(t_mod.subprocess, 'Popen', fail)
    with pytest.raises(OSError, match='fixture missing'):
        t_mod._login_run(['fixture'])
    for fd in (master, slave):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_local_browser_link_redirect_and_lifetime(t_mod, capsys):
    import http.client
    from urllib.parse import urlsplit
    bridge = t_mod._LoginBrowserLinks()
    try:
        links = t_mod._LoginLinks(bridge.shorten)
        output = links.feed(URL + b'\n')
        assert b'Cmd-double-click in Terminal' in output
        assert URL in output
        path, = bridge.targets
        address = '127.0.0.1:%d' % bridge.server.server_port
        connection = http.client.HTTPConnection(address, timeout=2)
        connection.request('GET', path)
        result = connection.getresponse()
        assert result.status == 302
        assert result.getheader('Location') == URL.decode()
        assert result.getheader('Cache-Control') == 'no-store'
        assert result.getheader('Referrer-Policy') == 'no-referrer'
        result.read()
        connection.close()
        for request_path, host in [(path, 'external.invalid'), ('/unknown', address)]:
            connection = http.client.HTTPConnection(address, timeout=2)
            connection.request('GET', request_path, headers={'Host': host})
            result = connection.getresponse()
            assert result.status == 404
            result.read()
            connection.close()
        unicode_url = 'https://example.invalid/é'
        path = urlsplit(bridge.shorten(unicode_url)).path
        assert bridge.targets[path] == 'https://example.invalid/%C3%A9'
    finally:
        bridge.close()
    assert not bridge.thread.is_alive() and not bridge.targets
    assert not capsys.readouterr().err
