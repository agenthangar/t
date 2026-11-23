"""Shutdown barriers for the only process t may replace during fresh launch."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_tmux(tmp_path):
    binary = shutil.which('tmux')
    if not binary:
        pytest.skip('tmux is not installed')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('COV_CORE_') and k not in ('TMUX', 'TMUX_PANE')}
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path), TMUX_TMPDIR='/tmp',
               XDG_CONFIG_HOME=str(tmp_path / 'config'), XDG_STATE_HOME=str(tmp_path / 'state'),
               XDG_CACHE_HOME=str(tmp_path / 'cache'), XDG_DATA_HOME=str(tmp_path / 'data'), TERM='xterm')
    command = [binary, '-L', f'launch-receipt-{os.getpid()}-{time.monotonic_ns()}']
    subprocess.run([*command, '-f', '/dev/null', 'new-session', '-d', '-s', 'keeper',
                    'sleep', '86400'], env=env, check=True, capture_output=True)
    try:
        yield env, command
    finally:
        subprocess.run([*command, 'kill-server'], env=env, capture_output=True)


@pytest.mark.parametrize('race', ['none', 'setup', 'placeholder', 'startup'])
def test_resume_launch_pins_created_terminal_when_slot_is_recycled(tmp_path, isolated_tmux, race):
    env, command = isolated_tmux
    env.update(SOURCE=str(ROOT), CASE=race, T_HISTORY_TOKEN='private-test-token')
    script = r'''
source "$SOURCE/zsh/sessions.zsh"
source "$SOURCE/zsh/resume.zsh"
_dev_auto_trust() { :; }
_dev_issue_preparing() { return 1; }
recycle() {
  raw_tmux kill-session -t '=dev-race-1:' || return 1
  local replacement
  replacement=$(raw_tmux new-session -d -P -F '#{session_id}:#{pane_id}:#{pane_pid}' -s dev-race-1 sleep 86400) || return 1
  print -r -- "$replacement" > "$HOME/replacement"
  raw_tmux set-environment -t "${replacement%%:*}:" DEV_AGENT replacement
  raw_tmux set-environment -t "${replacement%%:*}:" CLAUDE_RESUME_ID replacement
  raw_tmux set-option -t "${replacement%%:*}:" window-size manual
}
tmux() {
  print -r -- "$*" >> "$HOME/calls"
  if [[ $1 == new-session ]]; then
    local receipt
    receipt=$(raw_tmux "$@") || return $?
    print -r -- "$receipt" > "$HOME/created"
    [[ $CASE != setup ]] || recycle || return 1
    print -r -- "$receipt"
  else
    raw_tmux "$@"
  fi
}
_dev_agent_resume_cmd() {
  [[ $CASE != placeholder ]] || recycle || return 1
  print -r -- 'sleep 86400'
}
functions[_fixture_startup]=$functions[_dev_launch_startup_check]
_dev_launch_startup_check() {
  [[ $CASE != startup ]] || recycle || return 1
  _fixture_startup "$@"
}
_dev_resume_session_effect dev-race-1 "$HOME" selected codex history
'''
    prefix = f'raw_tmux() {{ command {shlex.join(command)} "$@"; }}\n'
    result = subprocess.run(['zsh', '-fc', prefix + script], env=env, text=True,
                            capture_output=True, timeout=15)
    created = (tmp_path / 'created').read_text().strip()
    if race == 'none':
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == created
        return
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == ''  # Failed launches never publish an attach receipt.
    replacement = (tmp_path / 'replacement').read_text().strip()
    current = subprocess.run([*command, 'display-message', '-p', '-t', '=dev-race-1:',
                              '#{session_id}:#{pane_id}:#{pane_pid}'], env=env, text=True,
                             capture_output=True, check=True).stdout.strip()
    assert current == replacement and not replacement.startswith(created + ':')
    for name in ('DEV_AGENT', 'CLAUDE_RESUME_ID'):
        value = subprocess.run([*command, 'show-environment', '-t', '=dev-race-1:', name],
                               env=env, text=True, capture_output=True, check=True).stdout.strip()
        assert value == f'{name}=replacement'
    assert subprocess.run([*command, 'show-options', '-wv', '-t', '=dev-race-1:', 'window-size'],
                          env=env, text=True, capture_output=True, check=True).stdout.strip() == 'manual'
    calls = (tmp_path / 'calls').read_text()
    assert '-t =dev-race-1' not in calls
    if race == 'setup':
        assert f'kill-session -t {created.split(":")[0]}:' in calls
        assert 'respawn-pane' not in calls
    if race == 'placeholder':
        assert 'respawn-pane' not in calls


@pytest.mark.parametrize('verb', ['attach-session', 'switch-client'])
def test_tmux_attach_and_switch_select_the_exact_receipt_pane(tmp_path, isolated_tmux, verb):
    from pty_helpers import _run_under_pty

    env, command = isolated_tmux
    target = subprocess.run([*command, 'new-session', '-d', '-P', '-F', '#{pane_id}',
                             '-s', 'destination', 'sleep', '86400'], env=env, text=True,
                            capture_output=True, check=True).stdout.strip()
    # A different current window must not override the pane encoded in a receipt.
    subprocess.run([*command, 'new-window', '-t', '=destination:', 'sleep', '86400'],
                   env=env, capture_output=True, check=True)
    args = ([*command, 'attach-session', '-t', '=keeper:', ';', 'switch-client', '-t', target]
            if verb == 'switch-client' else [*command, 'attach-session', '-t', target])
    result = _run_under_pty([*args, ';', 'detach-client'], env, timeout=10)
    assert result.returncode == 0, result.stdout
    selected = subprocess.run([*command, 'display-message', '-p', '-t', '=destination:', '#{pane_id}'],
                              env=env, text=True, capture_output=True, check=True).stdout.strip()
    assert selected == target


@pytest.mark.parametrize('scenario', ['normal', 'zombie', 'changed', 'dead', 'bad_pid', 'wrong_command',
                                      'term_failed', 'stuck', 'fork_failed'])
def test_placeholder_shutdown_barrier(tmp_path, scenario):
    log = tmp_path / 'calls'
    script = r'''
source "$SOURCE/zsh/sessions.zsh"
tmux() {
  print -r -- "$*" >> "$CALLS"
  case "$1" in
    display-message)
      if [[ $* == *pane_start_command* ]]; then
        local pid=424242 command_line='sleep 86400' dead=0
        [[ $CASE == bad_pid ]] && pid=1
        [[ $CASE == wrong_command ]] && command_line=codex
        [[ $CASE == dead ]] && dead=1
        [[ $CASE == changed && $* == *'-t %7 '* ]] && pid=424243
        print -r -- "%7|$pid|$command_line|$dead"
      else
        [[ $CASE == stuck ]] && print 0 || print 1
      fi ;;
    show-options) print failed ;;
    respawn-pane) [[ $CASE != fork_failed ]] || { print -u2 'fork failed: Device not configured'; return 1; } ;;
  esac
  return 0
}
kill() {
  print -r -- "kill $*" >> "$CALLS"
  if [[ $1 == -TERM ]]; then [[ $CASE != term_failed ]]; return; fi
  [[ $CASE == (stuck|zombie) ]]
}
_t_pid_is_zombie() { [[ $CASE == zombie ]]; }
sleep() { :; }
_dev_start_session_command dev-repo-1 'codex resume saved-id' "$HOME/log with spaces"
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_STATE_HOME=str(tmp_path / 'state'), SOURCE=str(ROOT), CASE=scenario, CALLS=str(log))
    result = subprocess.run(['zsh', '-f', '-c', script], env=env, text=True, capture_output=True, timeout=10)
    calls = log.read_text()
    launched = scenario in ('normal', 'zombie', 'fork_failed')
    assert ('respawn-pane' in calls) == launched
    assert 'respawn-pane -k' not in calls
    assert 'kill-server' not in calls
    assert (result.returncode == 0) == (scenario in ('normal', 'zombie'))
    if launched:
        assert calls.index('remain-on-exit on') < calls.index('kill -TERM') < calls.index('respawn-pane') < calls.index('pipe-pane')
        assert 'respawn-pane -t %7 env SHELL_SESSIONS_DISABLE=1 zsh -lic' in calls
        assert calls.count('respawn-pane') == 1
        if scenario != 'fork_failed':
            assert calls.rstrip().endswith('set-option -p -t %7 remain-on-exit failed')
        else:
            assert 'fork failed: Device not configured' in result.stderr
            assert 'run t doctor on this host' in result.stderr
    if scenario in ('changed', 'dead', 'bad_pid', 'wrong_command'):
        assert 'kill -TERM' not in calls
    if scenario == 'stuck':
        assert 'no replacement started' in result.stderr
