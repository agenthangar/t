"""Portable regression coverage; never touch real tmux, models, or networking."""
import json
from pathlib import Path
import shlex
import signal
import subprocess
from types import SimpleNamespace

import pytest
from conftest import load_script

ROOT = Path(__file__).parents[1]
PROOF = {'external': 'blocked by OS', 'localhost': 'allowed'}


@pytest.fixture
def shell(monkeypatch):
    module = load_script(ROOT/'scripts/offline_shell.py', 'offline_shell_test')
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/test/tmux')
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(module.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(module, 'SANDBOX', str(ROOT/'scripts/offline_shell.py'))
    return module


def test_environment_keeps_user_settings_but_separates_tmux(shell, monkeypatch, tmp_path):
    monkeypatch.setenv('TMUX', '/existing/server,123,0')
    monkeypatch.setenv('TMUX_PANE', '%3')
    monkeypatch.setenv('TMUX_TMPDIR', '/existing')
    monkeypatch.setenv('COV_CORE_SOURCE', 'measured')
    env = shell.isolated_environment(tmp_path)
    assert env['TMUX_TMPDIR'] == str(tmp_path) and env['T_OFFLINE_TEST'] == '1'
    assert 'TMUX' not in env and 'TMUX_PANE' not in env
    assert not any(k.startswith('COV_CORE_') for k in env)
    assert env['HOME'] == shell.os.environ['HOME']
    assert shell.os.environ['TMUX'] == '/existing/server,123,0'


@pytest.mark.parametrize('failure', [PermissionError(), ConnectionRefusedError(), TimeoutError(), None])
def test_probe_requires_actual_denial_and_loopback(shell, monkeypatch, failure):
    closed, calls = [], []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def bind(self, address): assert address == ('127.0.0.1', 0)
        def listen(self, n): pass
        def getsockname(self): return ('127.0.0.1', 12345)
        def accept(self): return Socket(), None
        def close(self): closed.append(True)
    def connect(address, timeout):
        calls.append(address)
        if address[0] == '1.1.1.1' and failure is not None:
            raise failure
        return Socket()
    monkeypatch.setattr(shell.socket, 'create_connection', connect)
    monkeypatch.setattr(shell.socket, 'socket', Socket)
    if isinstance(failure, PermissionError):
        assert shell.probe() == PROOF
        assert len(calls) == 2 and closed
    else:
        with pytest.raises(type(failure) if failure else RuntimeError): shell.probe()
        assert len(calls) == 1
        if failure is None: assert closed


@pytest.mark.parametrize('reason', ['platform', 'tmux', 'sandbox', 'stdin', 'stdout'])
def test_startup_refuses_missing_prerequisites(shell, monkeypatch, reason):
    if reason == 'platform': monkeypatch.setattr(shell.sys, 'platform', 'linux')
    if reason == 'tmux': monkeypatch.setattr(shell.shutil, 'which', lambda name: None)
    if reason == 'sandbox': monkeypatch.setattr(shell, 'SANDBOX', '/not/installed')
    if reason in ('stdin', 'stdout'): monkeypatch.setattr(getattr(shell.sys, reason), 'isatty', lambda: False)
    monkeypatch.setattr(shell.tempfile, 'mkdtemp', lambda **kw: pytest.fail('No temporary state on invalid invocation'))
    with pytest.raises(SystemExit) as error: shell.main([])
    assert error.value.code == 2


def test_help_and_private_probe(shell, monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as error: shell.main(['--help'])
    assert error.value.code == 0
    assert '--check' in capsys.readouterr().out
    monkeypatch.setattr(shell, 'probe', lambda: PROOF)
    assert shell.main(['--probe']) == 0
    assert json.loads(capsys.readouterr().out) == PROOF
    target = tmp_path/'proof.json'
    assert shell.main(['--probe', str(target)]) == 0
    assert json.loads(target.read_text()) == PROOF
    assert not target.with_suffix('.tmp').exists()


@pytest.mark.parametrize('mode', ['shell', 'check', 'probe_failed', 'cleanup_failed', 'remove_failed'])
def test_main_uses_private_paths_and_cleans_up(shell, tmp_path, monkeypatch, mode):
    root = tmp_path/'directory with spaces'; root.mkdir()
    monkeypatch.setattr(shell.tempfile, 'mkdtemp', lambda **kw: str(root))
    calls, cleanup = [], []
    def run(argv, **kw):
        calls.append((argv, kw))
        assert argv[-1] == '--probe' and Path(argv[-2]).is_absolute()
        assert kw['env']['TMUX_TMPDIR'] == str(root)
        assert '(deny network-outbound)' in (root/'network.sb').read_text()
        if mode == 'probe_failed': raise subprocess.CalledProcessError(9, argv)
    monkeypatch.setattr(shell.subprocess, 'run', run)
    monkeypatch.setattr(shell, 'interactive_shell', lambda sandbox, env: 7)
    monkeypatch.setattr(shell, 'check_tmux', lambda *a: 0)
    def clean(tmux, sock, env):
        cleanup.append(sock)
        assert sock == root/f'tmux-{shell.os.getuid()}'/'default'
        return mode != 'cleanup_failed'
    monkeypatch.setattr(shell, 'cleanup', clean)
    if mode == 'remove_failed':
        monkeypatch.setattr(shell.shutil, 'rmtree', lambda p: (_ for _ in ()).throw(OSError('cannot remove')))
    if mode == 'probe_failed':
        with pytest.raises(subprocess.CalledProcessError): shell.main([])
    else:
        assert shell.main([] if mode == 'shell' else ['--check']) == (7 if mode == 'shell' else 1 if mode in ('cleanup_failed', 'remove_failed') else 0)
    assert len(calls) == len(cleanup) == 1
    assert root.exists() == (mode in ('cleanup_failed', 'remove_failed'))


@pytest.mark.parametrize('state', ['ok', 'wrong_socket', 'wrong_proof', 'timeout'])
def test_check_validates_the_private_pane(shell, tmp_path, monkeypatch, state):
    script = tmp_path/'a script with spaces.py'
    monkeypatch.setattr(shell, 'SCRIPT', script)
    sock = tmp_path/'socket'; calls = []
    def run(argv, **kw):
        calls.append(argv)
        assert argv[:3] == ['sandbox', '-f', 'profile']
        assert shlex.split(argv[-1].split(';')[0]) == [shell.sys.executable, str(script), '--probe', str(tmp_path/'pane-network.json')]
        if state != 'timeout':
            (tmp_path/'pane-network.json').write_text(json.dumps(PROOF if state != 'wrong_proof' else {}))
    monkeypatch.setattr(shell.subprocess, 'run', run)
    monkeypatch.setattr(shell.subprocess, 'check_output', lambda argv, **kw: str(sock if state != 'wrong_socket' else tmp_path/'other'))
    times = iter([0, 11]); monkeypatch.setattr(shell.time, 'monotonic', lambda: next(times))
    if state == 'ok': assert shell.check_tmux(['sandbox', '-f', 'profile'], 'tmux', tmp_path, sock, {}) == 0
    else:
        with pytest.raises(RuntimeError): shell.check_tmux(['sandbox', '-f', 'profile'], 'tmux', tmp_path, sock, {})
    assert len(calls) == 1


@pytest.mark.parametrize('code,exception,kill', [(0, False, False), (7, False, False), (-9, False, False), (0, True, False), (0, True, True)])
def test_shell_owns_ctrl_c_and_cleans_up_on_interruption(shell, monkeypatch, code, exception, kill):
    events, handlers = [], []
    class Process:
        def wait(self, timeout=None):
            events.append(('wait', timeout))
            if len(events) == 1 and exception: raise KeyboardInterrupt
            if timeout and kill: raise subprocess.TimeoutExpired('shell', timeout)
            return code
        def poll(self): return None if exception else code
        def terminate(self): events.append(('terminate',))
        def kill(self): events.append(('kill',))
    def popen(argv, env):
        assert argv == ['sandbox', '/bin/zsh', '-il']
        return Process()
    monkeypatch.setattr(shell.subprocess, 'Popen', popen)
    monkeypatch.setattr(shell.signal, 'signal', lambda sig, handler: handlers.append((sig, handler)) or 'previous')
    if exception:
        with pytest.raises(KeyboardInterrupt): shell.interactive_shell(['sandbox'], {})
        assert ('terminate',) in events
        assert (('kill',) in events) == kill
    else: assert shell.interactive_shell(['sandbox'], {}) == (137 if code == -9 else code)
    assert handlers == [(signal.SIGINT, signal.SIG_IGN), (signal.SIGINT, 'previous')]


@pytest.mark.parametrize('state', ['missing', 'ok', 'failed', 'timeout'])
def test_cleanup_can_only_address_its_private_socket(shell, tmp_path, monkeypatch, state, capsys):
    sock = tmp_path/'private socket'
    if state != 'missing': sock.touch()
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if state == 'timeout': raise subprocess.TimeoutExpired('tmux', 5)
        return SimpleNamespace(returncode=0 if state == 'ok' else 1)
    monkeypatch.setattr(shell.subprocess, 'run', run)
    assert shell.cleanup('tmux', sock, {}) == (state in ('missing', 'ok'))
    assert calls == ([] if state == 'missing' else [['tmux', '-S', str(sock), 'kill-server']])
    if state in ('failed', 'timeout'): assert shlex.quote(str(sock)) in capsys.readouterr().err


@pytest.mark.parametrize('error,code', [(KeyboardInterrupt(), 130), (RuntimeError('bad probe'), 1), (None, 7)])
def test_cli_reports_errors_and_handles_terminal_close(shell, monkeypatch, error, code):
    handlers = {}
    monkeypatch.setattr(shell.signal, 'signal', lambda sig, fn: handlers.update({sig: fn}))
    def main():
        if error: raise error
        return 7
    monkeypatch.setattr(shell, 'main', main)
    assert shell.cli() == code
    with pytest.raises(SystemExit) as stopped: handlers[signal.SIGHUP](signal.SIGHUP, None)
    assert stopped.value.code == 128+signal.SIGHUP
