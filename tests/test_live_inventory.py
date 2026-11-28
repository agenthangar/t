"""Exercise the production cache boundary against disposable tmux servers."""
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def servers(tmp_path, monkeypatch):
    tmux = shutil.which('tmux')
    if not tmux:
        pytest.skip('tmux required')
    with tempfile.TemporaryDirectory(prefix='t-inv-', dir='/tmp') as raw:
        directory = Path(raw).resolve()
        home = tmp_path / 'home'
        home.mkdir()
        for name in ('TMUX', 'TMUX_PANE', 'T_DISCOVERY_COLLECTOR', 'T_LOCAL_RC'):
            monkeypatch.delenv(name, raising=False)
        for name, value in dict(HOME=home, ZDOTDIR=home, TMUX_TMPDIR=directory, CODEX_HOME=home/'.codex',
                                XDG_CONFIG_HOME=home/'config', XDG_STATE_HOME=home/'state',
                                XDG_CACHE_HOME=home/'cache', XDG_RUNTIME_DIR=directory,
                                T_DEV_CONTEXT=ROOT, T_DEV_CONTEXT_OWNER=os.getpid(),
                                T_NO_UPDATE_CHECK=1, T_RECOVERY_DISABLE=1).items():
            monkeypatch.setenv(name, str(value))
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        broken = tmp_path / 'fail'
        shim = bin_dir / 'tmux'
        shim.write_text('#!/bin/sh\nif [ "$1" = list-sessions ] && [ -f ' + shlex.quote(str(broken)) +
                        ' ]; then echo "injected inventory failure" >&2; exit 7; fi\nexec ' +
                        shlex.quote(tmux) + ' "$@"\n')
        shim.chmod(0o755)
        monkeypatch.setenv('PATH', str(bin_dir) + ':' + str(ROOT/'bin') + ':' + os.environ['PATH'])
        base = directory / f'tmux-{os.getuid()}'
        base.mkdir(mode=0o700)
        default, alternate = base/'default', directory/'alternate.sock'
        def command(*args, env=None):
            return subprocess.run(args, env=env, cwd=home, capture_output=True, text=True, timeout=35)
        def create(path, n):
            result = command(tmux, '-S', str(path), '-f', '/dev/null', 'new-session',
                             '-d', '-s', f'dev-api-{n}', '-c', str(home), 'sleep 120')
            assert result.returncode == 0, result.stderr
        try:
            yield default, alternate, create, command, broken
        finally:
            for path in (alternate, default):
                command(tmux, '-S', str(path), 'kill-server')
            cache = runpy.run_path(str(ROOT/'libexec/t_session_cache.py'))
            deadline = time.monotonic() + 5
            while endpoints := list(directory.glob('t-sessions-*/service.sock')):
                assert time.monotonic() < deadline, f'fixture discovery still running: {endpoints}'
                for endpoint in endpoints:
                    try:
                        cache['_existing_status'](endpoint.parent, [], deadline)
                    except (OSError, ValueError):
                        pass
                time.sleep(.05)


def test_public_wait_isolates_servers_and_retries_failed_scan(servers):
    default, alternate, create, command, broken = servers
    create(alternate, 2)
    create(default, 1)
    args = [sys.executable, str(ROOT/'bin/t'), 'session', 'list', '--local', '--wait']
    other = command(*args, env=dict(os.environ, TMUX=f'{alternate},0,0'))
    assert 'api-2' in other.stdout and 'api-1' not in other.stdout, other
    command('tmux', '-S', str(alternate), 'kill-server')
    assert 'api-1' in command(*args).stdout
    broken.touch()
    failed = command(*args)
    assert 'api-1' in failed.stdout and 'refresh failed' in failed.stderr, failed
    broken.unlink()
    create(default, 3)
    recovered = command(*args)
    assert 'api-1' in recovered.stdout and 'api-3' in recovered.stdout, recovered
    assert 'api-2' not in recovered.stdout
    cache = runpy.run_path(str(ROOT/'libexec/t_session_cache.py'))
    diagnosis = '\n'.join(cache['doctor_inventory'](2, str(default)))
    assert 'Last failure' in diagnosis and 'injected inventory failure' in diagnosis, diagnosis
    assert f'2 numbered slots on {default}' in diagnosis
    command('tmux', '-S', str(default), 'kill-server')
    empty = command(*args)
    assert not re.search(r'api-[13]\b', empty.stdout), empty


def test_live_inventory_during_56_slot_batch(servers):
    default, _, create, command, _ = servers
    live = runpy.run_path(str(ROOT/'libexec/t_live_inventory.py'))
    args = [sys.executable, str(ROOT/'bin/t'), 'session', 'list', '--local']
    create(default, 1)
    assert 'api-1' in command(*args, '-w').stdout
    for n in range(2, 57):
        create(default, n)
        live['notify_ready']()
        if n in (20, 56):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                view = command(*args)
                if len(set(re.findall(r'\bapi-(\d+)\b', view.stdout))) == n:
                    break
            else:
                pytest.fail(view.stdout + view.stderr)


def test_open_picker_adds_live_slot_with_query_focus_and_mark(servers):
    default, _, create, command, _ = servers
    if not shutil.which('fzf'):
        pytest.skip('fzf required')
    create(default, 1)
    create(default, 2)
    shell = f'source {shlex.quote(str(ROOT/"t.plugin.zsh"))}; t open --local -w'
    command('tmux', '-S', str(default), 'new-session', '-d', '-x', '110', '-y', '32',
            '-s', 'picker', 'zsh -f -c ' + shlex.quote(shell))
    stream_path = Path(os.environ['XDG_CACHE_HOME']).parent / 'picker-stream'
    command('tmux', '-S', str(default), 'pipe-pane', '-t', 'picker:', 'cat > ' + shlex.quote(str(stream_path)))
    def frame():
        return command('tmux', '-S', str(default), 'capture-pane', '-p', '-t', 'picker:').stdout
    def wait_for(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            screen = frame()
            if predicate(screen):
                return screen
            time.sleep(.05)
        history = command('tmux', '-S', str(default), 'capture-pane', '-p', '-S', '-100', '-t', 'picker:').stdout
        trace = stream_path.read_text(errors='replace') if stream_path.exists() else ''
        pytest.fail(frame() + '\nTerminal history\n' + history + '\nTerminal trace\n' + trace[-6000:])
    def row(screen, n):
        return next((line for line in screen.splitlines() if re.search(r'api-' + str(n) + r'\s', line)), '')
    wait_for(lambda screen: row(screen, 1) and row(screen, 2))
    command('tmux', '-S', str(default), 'send-keys', '-t', 'picker:', 'a', 'p', 'i', 'Tab')
    before = wait_for(lambda screen: any('✓' in row(screen, n) for n in (1, 2)))
    marked = next(n for n in (1, 2) if '✓' in row(before, n))
    focused = next(n for n in (1, 2) if '▸' in row(before, n))
    create(default, 3)
    # Use the same asynchronous notification as a completed resume worker.
    runpy.run_path(str(ROOT/'libexec/t_live_inventory.py'))['notify_ready']()
    wait_for(lambda screen: row(screen, 3) and '✓' in row(screen, marked)
             and '▸' in row(screen, focused) and '> api' in screen)
    create(default, 4)
    command('tmux', '-S', str(default), 'send-keys', '-t', 'picker:', 'C-u')
    wait_for(lambda screen: row(screen, 4) and '✓' in row(screen, marked)
             and '▸' in row(screen, focused) and '> api' in screen)
    command('tmux', '-S', str(default), 'send-keys', '-t', 'picker:', 'Escape')


def test_picker_refuses_a_replaced_slot(servers):
    default, _, create, command, _ = servers
    create(default, 7)
    old = command('tmux', '-S', str(default), 'display-message', '-p', '-t', '=dev-api-7:',
                  '#{pid}/#{session_id}').stdout.strip()
    create(default, 8)  # Keep the same server while replacing the selected session.
    command('tmux', '-S', str(default), 'kill-session', '-t', '=dev-api-7:')
    create(default, 7)
    shell = 'source "$1/t.plugin.zsh"; _t_open_existing api-7 "$2" "$3"'
    result = command('zsh', '-f', '-c', shell, 'zsh', str(ROOT), old, str(default))
    assert result.returncode and 'selected session changed' in result.stderr, result

def test_late_inventory_cannot_restore_a_closed_session(monkeypatch):
    live = runpy.run_path(str(ROOT/'libexec/t_live_inventory.py'))
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    old = dict(terminal='3/$1', cwd='/repo', state='detached', agent='codex', stamp='')
    reports = []
    def scan():
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
            return '/socket', {'repo-1': old}
        return '/socket', {}
    def report(status):
        reports.append(status['count'])
        if len(reports) == 2:
            completed.set()
    inventory = live['Inventory'](report)
    monkeypatch.setitem(inventory._run.__globals__, 'scan', scan)
    inventory.request(explicit=True)
    assert entered.wait(3)
    inventory.close('repo-1')
    release.set()
    assert completed.wait(3)
    assert reports == [0, 0]


def test_wait_returns_while_session_details_are_blocked(servers, tmp_path):
    default, _, create, command, _ = servers
    create(default, 4)
    entered, released = tmp_path/'collecting', tmp_path/'released'
    shell = shutil.which('zsh')
    wrapper = tmp_path/'bin/zsh'
    wrapper.write_text(
        '#!/bin/sh\ncase "$*" in *"_dev_session_rows --details"*)\n'
        f'  touch {shlex.quote(str(entered))}\n'
        f'  while [ ! -f {shlex.quote(str(released))} ]; do sleep 0.1; done\n'
        'esac\n' + 'exec ' + shlex.quote(shell) + ' "$@"\n')
    wrapper.chmod(0o755)
    args = [sys.executable, str(ROOT/'bin/t'), 'session', 'list', '--local', '-w']
    try:
        assert 'api-4' in command(*args).stdout
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert entered.exists()
        create(default, 5)
        started = time.monotonic()
        result = command(*args)
        assert 'api-4' in result.stdout and 'api-5' in result.stdout, result
        assert time.monotonic() - started < 5
    finally:
        released.touch()


def test_wait_joins_scan_while_its_report_is_publishing(monkeypatch):
    live = runpy.run_path(str(ROOT/'libexec/t_live_inventory.py'))
    writing, release = threading.Event(), threading.Event()
    def report(_):
        writing.set()
        release.wait(3)
    inventory = live['Inventory'](report)
    monkeypatch.setitem(inventory._run.__globals__, 'scan', lambda: ('/socket', {}))
    first = inventory.request(explicit=True)
    try:
        assert writing.wait(3)
        assert inventory.request(explicit=True) == first
    finally:
        release.set()


def test_empty_open_picker_receives_the_first_slot(servers):
    default, _, create, command, _ = servers
    shell = f'source {shlex.quote(str(ROOT/"t.plugin.zsh"))}; t open --local -w'
    result = command('tmux', '-S', str(default), '-f', '/dev/null', 'new-session', '-d',
                     '-x', '100', '-y', '26', '-s', 'empty-picker', 'zsh -f -c ' + shlex.quote(shell))
    assert result.returncode == 0, result.stderr
    def wait_for(text):
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            screen = command('tmux', '-S', str(default), 'capture-pane', '-p', '-t', 'empty-picker:').stdout
            if text in screen:
                return
            time.sleep(.05)
        pytest.fail(screen)
    wait_for('0/0')
    create(default, 1)
    runpy.run_path(str(ROOT/'libexec/t_live_inventory.py'))['notify_ready']()
    wait_for('api-1')
    command('tmux', '-S', str(default), 'send-keys', '-t', 'empty-picker:', 'Escape')


def test_reused_stamp_does_not_restore_replaced_terminal_details():
    inventory = runpy.run_path(str(ROOT / 'libexec/t_live_inventory.py'))['Inventory'](lambda _: None)
    inventory.slots = {'api-7': dict(cwd='/repo', terminal='3/$2', stamp='old', state='detached', agent='codex')}
    host = {'value': {'rows': 'old\t/repo\tapi-7\tdetached\tactive\tPrevious work\tcodex'}}
    inventory.project(host, {'api-7': '3/$1'})
    assert host['value']['rows'].split('\t')[:6] == ['-', '/repo', 'api-7', 'detached', 'unknown', '-']
