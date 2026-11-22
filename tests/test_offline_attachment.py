"""Recovery handoff through real private tmux; no real models or user settings."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('keys,expected', [(['Enter'], 'fixture-local'),
    (['Right', 'Enter'], 'fixture-other'), (['Left', 'Enter'], 'fixture-other'),
    (['Right', 'Left', 'Enter'], 'fixture-local'), (['Escape'], None),
    (['Down', 'Down', 'Enter'], None)])
def test_fresh_readiness_reaches_offline_model_picker(keys, expected):
    tmux, zsh = shutil.which('tmux'), shutil.which('zsh')
    if not tmux or not zsh:
        pytest.skip('tmux and zsh required')
    with tempfile.TemporaryDirectory(prefix='t-attach-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        home, bins = root/'home', root/'bin'
        home.mkdir(); bins.mkdir()
        sock = root/'tmux.sock'
        wrapper = bins/'tmux'
        wrapper.write_text('#!/bin/sh\nexec '+shlex.join([tmux, '-S', str(sock)])+' "$@"\n')
        wrapper.chmod(0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')
               and k not in ('TMUX', 'TMUX_PANE')}
        env.update(HOME=str(home), T_HOME=str(ROOT), ZDOTDIR=str(home), TERM='xterm-256color',
                   PATH=str(bins)+os.pathsep+os.environ['PATH'])
        for name in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'CODEX_HOME'):
            env[name] = str(root/name)
        driver = root/'driver.py'
        driver.write_text('''import json, pathlib, runpy, subprocess, sys
from contextlib import nullcontext
from types import SimpleNamespace
import threading
m = runpy.run_path(sys.argv[1])
g = m['recover'].__globals__
root = pathlib.Path(sys.argv[2])
def cloud(argv, chosen, cwd, **kwargs):
 cp = kwargs['checkpoint']
 cp.client({'id': 1, 'method': 'account/read'})
 cp.server({'id': 1, 'error': {'code': -32603, 'message': 'workspace routing discovery failed'}})
 return {'action': None, 'exit_code': 1}
def lead(argv, **kwargs):
 assert not subprocess.check_output(['tmux', 'show-options', '-pqv', '@t_recovery_ui'], text=True).strip()
 (root/'selected').write_text(kwargs['chosen']['model'])
 (root/'selected-provider').write_text(kwargs['chosen']['provider'])
 return 0
g['supervise'] = cloud
g['RecoveryProbe'] = lambda: nullcontext(SimpleNamespace(ready=threading.Event()))
g['inference'] = lambda: {'helper': lambda name: {'state_root': lambda: root},
 'catalogue': lambda **kw: {'models': [{'model': 'fixture-local', 'provider': 'lmstudio', 'ready': True, 'fit': True},
  {'model': 'fixture-other', 'provider': 'ollama', 'ready': True, 'fit': True},
  {'model': 'unavailable-fixture', 'provider': 'ollama', 'ready': False, 'fit': False, 'reason': 'Not downloaded'}]}, 'lead': lead}
chosen = dict(mode='automatic', provider='lmstudio', model='fixture-local', context_tokens=32768, timeout_seconds=300, on_cloud_failure='ask')
result = m['recover'](['codex'], chosen, str(root))
(root/'result').write_text(str(result))
''')
        def mux(*args, check=True):
            return subprocess.run([str(wrapper), *args], env=env, capture_output=True,
                                  text=True, check=check, timeout=5)
        command = shlex.join([sys.executable, str(driver), str(ROOT/'libexec/t_offline.py'), str(root)])
        try:
            mux('-f', '/dev/null', 'new-session', '-d', '-s', 'dev-fixture-1', '-c', str(root),
                command+'; sleep 20')
            deadline = time.monotonic()+15
            while True:
                frame = mux('capture-pane', '-p', '-t', '=dev-fixture-1:').stdout
                if 'Start locally' in frame:
                    break
                assert time.monotonic() < deadline, frame
                time.sleep(.05)
            script = 'source "$T_HOME/zsh/agent.zsh"; source "$T_HOME/zsh/sessions.zsh"; source "$T_HOME/zsh/lifecycle.zsh"; '+\
                     '_t_lifecycle_effect ready dev-fixture-1 '+shlex.quote(str(root))+' codex ""'
            ready = subprocess.run([zsh, '-fc', script], env=env, capture_output=True, text=True, timeout=22)
            assert ready.returncode == 0, ready.stderr
            assert ready.stdout.strip().endswith('|'+str(root))
            for key in keys[:-1]:
                mux('send-keys', '-t', '=dev-fixture-1:', key)
            assert not (root/'selected').exists()  # Navigation never starts inference.
            mux('send-keys', '-t', '=dev-fixture-1:', keys[-1])
            deadline = time.monotonic()+5
            while not (root/'result').exists():
                assert time.monotonic() < deadline, mux('capture-pane', '-p').stdout
                time.sleep(.05)
            assert (root/'result').read_text() == ('0' if expected else '1')
            assert (root/'selected').exists() == bool(expected)
            if expected:
                assert (root/'selected').read_text() == expected
                assert (root/'selected-provider').read_text() == ('ollama' if expected == 'fixture-other' else 'lmstudio')
            assert not mux('show-options', '-pqv', '@t_recovery_ui').stdout.strip()
        finally:
            mux('kill-server', check=False)


@pytest.mark.parametrize('invalid', [None, 'cwd', 'pane', 'owner-start', 'pane-start', 'dead', 'orphan', 'cycle', 'json', 'missing', 'oversize', 'pid-type', 'probe-error'])
def test_recovery_attachment_requires_live_exact_owner(tmp_path, monkeypatch, invalid):
    m = load_script(ROOT/'libexec/t_offline.py', 'attachment_test')
    value = dict(pid=12, start='owner-start', pane_pid=10, pane_start='pane-start', cwd=str(tmp_path))
    table = {10: (1, 10, 'S', 'pane-start'), 11: (10, 10, 'S', 'shell'), 12: (11, 10, 'S', 'owner-start')}
    if invalid == 'cwd': value['cwd'] = str(tmp_path/'other')
    if invalid == 'pane': value['pane_pid'] = 20
    if invalid == 'owner-start': value['start'] = 'reused'
    if invalid == 'pane-start': value['pane_start'] = 'reused'
    if invalid == 'dead': table[12] = (11, 10, 'Z', 'owner-start')
    if invalid == 'orphan': table[12] = (1, 10, 'S', 'owner-start')
    if invalid == 'cycle': table[11] = (12, 10, 'S', 'shell')
    if invalid == 'missing': table.pop(12)
    if invalid == 'pid-type': value['pid'] = '12'
    monkeypatch.setattr(m, 'process_table', lambda: table)
    if invalid == 'probe-error':
        monkeypatch.setattr(m, 'process_table', lambda: (_ for _ in ()).throw(OSError('unavailable')))
    raw = 'bad' if invalid == 'json' else 'x'*4097 if invalid == 'oversize' else json.dumps(value)
    assert m.recovery_ready(raw, 10, tmp_path) == (invalid is None)


@pytest.mark.parametrize('mode', ['normal', 'cancel', 'replaced', 'foreign-tty', 'foreground', 'missing-pane', 'foreign-tree'])
def test_startup_attachment_is_scoped_to_its_terminal_and_cleared(tmp_path, monkeypatch, mode):
    m = load_script(ROOT/'libexec/t_offline.py', 'attachment_scope_test')
    monkeypatch.setenv('TMUX', '/private/socket,1,0')
    monkeypatch.setenv('TMUX_PANE', '%7')
    if mode == 'foreground': monkeypatch.delenv('TMUX')
    monkeypatch.setattr(m.os, 'getpid', lambda: 12)
    monkeypatch.setattr(m.os, 'ttyname', lambda fd: '/dev/fixture')
    table = {10: (1, 10, 'S', 'pane'), 12: (1 if mode == 'foreign-tree' else 10, 10, 'S', 'owner')}
    if mode == 'missing-pane': table.pop(10)
    monkeypatch.setattr(m, 'process_table', lambda: table)
    current, calls = [''], []
    def run(argv, **kwargs):
        calls.append(argv)
        assert '%7' in argv
        if argv[1] == 'display-message':
            output = ('/dev/other' if mode == 'foreign-tty' else '/dev/fixture')+'\t10'
        elif argv[1] == 'show-options': output = current[0]
        else:
            current[0] = '' if '-u' in argv else argv[-1]
            output = ''
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(m.subprocess, 'run', run)
    def enter():
        with m.startup_attachment(tmp_path):
            if mode == 'foreground':
                assert not calls
                return
            assert json.loads(current[0])['pid'] == 12
            assert m.recovery_ready(current[0], 10, tmp_path)
            if mode == 'cancel': raise KeyboardInterrupt
            if mode == 'replaced': current[0] = 'replacement owner'
    if mode in ('foreign-tty', 'missing-pane', 'foreign-tree'):
        with pytest.raises(ValueError, match='terminal'): enter()
        assert not any(c[1] == 'set-option' for c in calls)
    elif mode == 'cancel':
        with pytest.raises(KeyboardInterrupt): enter()
    else: enter()
    assert current[0] == ('replacement owner' if mode == 'replaced' else '')


@pytest.mark.parametrize('mode', ['ready', 'pipe', 'pane-replaced', 'marker-cleared', 'unverified', 'wrong-agent', 'saved'])
def test_shell_rechecks_recovery_identity_and_keeps_saved_writer_guard(tmp_path, mode):
    zsh = shutil.which('zsh')
    if not zsh: pytest.skip('zsh required')
    if mode == 'pipe':
        tmp_path = tmp_path/'work|tree'
        tmp_path.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(HOME=str(tmp_path), TEST_ROOT=str(ROOT), TEST_DIR=str(tmp_path), TEST_MODE=mode)
    script = r'''
source "$TEST_ROOT/zsh/lifecycle.zsh"
tmux() {
  case $1 in
    display-message)
      if [[ ${@[-1]} == '#{pane_dead}' ]]; then print 0; return; fi
      local n=0 pane='%2'
      [[ ! -f $TEST_DIR/count ]] || n=$(<$TEST_DIR/count)
      (( n++ )); print -r -- "$n" > "$TEST_DIR/count"
      [[ $TEST_MODE != pane-replaced || $n -lt 3 ]] || pane='%3'
      print -r -- "\$1|$TEST_DIR|$pane|10" ;;
    show-options)
      [[ $TEST_MODE != marker-cleared || ! -f $TEST_DIR/probed ]] && print marker ;;
  esac
}
_dev_session_claude_pid() { return 1; }
_t_history_owner() { print strict-writer; return 17; }
python3() { touch "$TEST_DIR/probed"; [[ $TEST_MODE != unverified ]]; }
sleep() { SECONDS=20; }
agent=codex; sid=''
[[ $TEST_MODE != wrong-agent ]] || agent=claude
[[ $TEST_MODE != saved ]] || sid=selected-conversation
_t_lifecycle_effect ready dev-fixture-1 "$TEST_DIR" "$agent" "$sid"
'''
    result = subprocess.run([zsh, '-fc', script], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if mode in ('ready', 'pipe') else 17 if mode == 'saved' else 1), result.stderr
    assert (tmp_path/'probed').exists() == (mode not in ('wrong-agent', 'saved'))
    if mode == 'saved': assert result.stdout.strip() == 'strict-writer'
