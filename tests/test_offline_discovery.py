"""A known OS network failure must not enter an interactive discovery wait."""
import errno
import socket
from types import SimpleNamespace

import pytest

from test_session_discovery import adapter, view  # noqa: F401


def network(monkeypatch, failures):
    calls = []
    class Socket:
        def __init__(self, family, kind):
            assert kind == socket.SOCK_DGRAM
            self.family = family
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setblocking(self, enabled): assert enabled is False
        def connect(self, address):
            calls.append(self.family)
            error = failures[self.family]
            if error:
                raise OSError(error, 'fixture network failure')
    monkeypatch.setattr(socket, 'socket', Socket)
    return calls


@pytest.mark.parametrize('error', [errno.EPERM, errno.EACCES, errno.ENETUNREACH, errno.ENETDOWN])
def test_open_known_offline_bails_before_discovery_or_picker(adapter, monkeypatch, error):
    calls = network(monkeypatch, {socket.AF_INET: error, socket.AF_INET6: error})
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: pytest.fail('discovery started while offline'))
    cfg = SimpleNamespace(hosts={'mini': 'mini.invalid'})
    with pytest.raises(OSError, match='network unavailable.*t open --local'):
        adapter.open_snapshot(cfg, lambda *a: pytest.fail('offline recovery picker'))
    assert calls == [socket.AF_INET, socket.AF_INET6]


@pytest.mark.parametrize('options,hosts', [({'local': True}, {'mini': 'mini.invalid'}),
    ({'host': 'local'}, {'mini': 'mini.invalid'}), ({}, {})])
def test_local_only_discovery_does_not_require_network(adapter, monkeypatch, options, hosts):
    monkeypatch.setattr(socket, 'socket', lambda *a: pytest.fail('local discovery probed network'))
    snapshot = view()
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: snapshot)
    assert adapter.open_snapshot(SimpleNamespace(hosts=hosts), lambda *a: pytest.fail('picker'), **options) == (snapshot, '')


@pytest.mark.parametrize('v4,v6', [(None, errno.ENETUNREACH), (errno.ENETUNREACH, None),
    (errno.EMFILE, errno.ENETUNREACH), (errno.ENETUNREACH, errno.EINPROGRESS)])
def test_unknown_or_one_available_family_keeps_normal_discovery(adapter, monkeypatch, v4, v6):
    network(monkeypatch, {socket.AF_INET: v4, socket.AF_INET6: v6})
    snapshot = view()
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: snapshot)
    assert adapter.open_snapshot(SimpleNamespace(hosts={'mini': 'mini.invalid'}), lambda *a: None) == (snapshot, '')


def test_network_recovery_is_rechecked_on_next_open(adapter, monkeypatch):
    failures = {socket.AF_INET: errno.EPERM, socket.AF_INET6: errno.EAFNOSUPPORT}
    network(monkeypatch, failures)
    snapshot = view()
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: snapshot)
    cfg = SimpleNamespace(hosts={})
    with pytest.raises(OSError, match='network unavailable'):
        adapter.open_snapshot(cfg, lambda *a: None, host='explicit.invalid')
    failures[socket.AF_INET] = None
    assert adapter.open_snapshot(cfg, lambda *a: None, host='explicit.invalid') == (snapshot, '')


def test_open_offline_protocol_returns_error_without_empty_inventory(t_mod, adapter, monkeypatch, capsys):
    network(monkeypatch, {socket.AF_INET: errno.EPERM, socket.AF_INET6: errno.EPERM})
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: pytest.fail('discovery started while offline'))
    monkeypatch.setattr(t_mod, 'Config', lambda: SimpleNamespace(hosts={'mini': 'mini.invalid'}))
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: vars(adapter))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stderr, 'isatty', lambda: True)
    assert t_mod.main(['__cache-rows', '--open']) == 1
    output = capsys.readouterr()
    assert not output.out
    assert 't open: network unavailable' in output.err
    assert 'Refreshing sessions' not in output.err


def test_public_open_under_os_network_denial():
    """Headless PTY, real public command and OS policy; no desktop or user state."""
    import os
    from pathlib import Path
    import runpy
    import shlex
    import shutil
    import sys
    import tempfile
    import time
    from conftest import REPO_ROOT
    from pty_helpers import run_terminal

    sandbox = '/usr/bin/sandbox-exec'
    if sys.platform != 'darwin' or not Path(sandbox).exists():
        pytest.skip('macOS network sandbox required')
    if not shutil.which('zsh') or not shutil.which('fzf'):
        pytest.skip('zsh and fzf required')
    with tempfile.TemporaryDirectory(prefix='t-offnet-', dir='/tmp') as directory:
        root = Path(directory).resolve()
        home, bins = root/'home', root/'bin'
        home.mkdir(); bins.mkdir()
        # A regression must never launch real tmux, agents or remote commands.
        for name in ('tmux', 'ssh', 'codex', 'claude'):
            path = bins/name
            path.write_text('#!/bin/sh\necho unexpected >> "$HOME/actions"\nexit 1\n')
            path.chmod(0o700)
        rc = home/'local.zsh'
        rc.write_text('REMOTE_HOSTS[fixture]=fixture.invalid\n')
        env = dict(HOME=str(home), PATH=f'{bins}:{REPO_ROOT}/bin:'+os.environ['PATH'],
                   TERM='xterm-256color', T_LOCAL_RC=str(rc), T_NO_AUTORELOAD='1',
                   T_RECOVERY_DISABLE='1', XDG_RUNTIME_DIR=str(root), TMUX_TMPDIR=str(root))
        for name in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            env[name] = str(home/name)
        policy = runpy.run_path(str(REPO_ROOT/'scripts/offline_shell.py'))['PROFILE']
        script = 'source '+shlex.quote(str(REPO_ROOT/'t.plugin.zsh'))+'; t open'
        start = time.monotonic()
        status, output = run_terminal([sandbox, '-p', policy, 'zsh', '-fc', script], env, root, (100, 30), [])
        assert status == 1, output
        assert 't open: network unavailable or blocked' in output
        assert 't open --local' in output
        assert 'Refreshing sessions' not in output
        assert time.monotonic()-start < 5
        assert not (home/'actions').exists()
        assert not list(root.rglob('service.sock'))
        assert not list(home.rglob('cache.sqlite3'))
