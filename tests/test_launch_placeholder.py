"""Shutdown barriers for the only process t may replace during fresh launch."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
