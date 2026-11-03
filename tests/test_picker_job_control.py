"""Stopped picker children must not strand interactive zsh command substitutions."""

import os
from pathlib import Path
import shutil
import resource
import signal
import subprocess
import time

import pytest


PICKER = Path(__file__).resolve().parents[1] / 'ui/fzf.sh'


@pytest.mark.skipif(not shutil.which('zsh'), reason='requires zsh')
@pytest.mark.parametrize('stop_scope', ['child', 'group'])
@pytest.mark.parametrize('profile', ['filtered', 'paginated', 'single', 'multi'])
def test_interactive_picker_completes_after_stopped_child(tmp_path, profile, stop_scope):
    stubbin = tmp_path / 'stubbin'
    stubbin.mkdir()
    stub = stubbin / 'fzf'
    stub.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
import time
if '--help' in sys.argv:
    print('--id-nth')
    sys.exit()
pidfile = Path(os.environ['PICKER_PID'])
pidfile.with_suffix('.ready').write_text(f'{os.getpid()} {os.getppid()}')
pidfile.with_suffix('.ready').replace(pidfile)
while not Path(os.environ['PICKER_RELEASE']).exists():
    time.sleep(0.02)
print('chosen')
''')
    stub.chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()
    rows = tmp_path / 'rows'
    rows.write_text('chosen\n')
    page = tmp_path / 'page'
    page.write_text('Page 1\n')
    calls = {
        'filtered': '_t_fzf_filtered chosen chosen open Active Legend ""',
        'paginated': '_t_fzf_paginated "$PAGE" "$ROWS" true "" "" "pick> "',
        'single': '_t_fzf_single open Legend < "$ROWS"',
        'multi': '_t_fzf_multi open Legend < "$ROWS"',
    }
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.pop('T_HOME', None)
    env.update(HOME=str(home), ZDOTDIR=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_DATA_HOME=str(home / '.local/share'), XDG_CACHE_HOME=str(home / '.cache'),
               XDG_STATE_HOME=str(home / '.local/state'),
               TMPDIR=str(tmp_path), PATH=f'{stubbin}{os.pathsep}{env.get("PATH", "")}',
               PICKER_PID=str(tmp_path / 'pid'), PICKER_RELEASE=str(tmp_path / 'release'),
               PICKER=str(PICKER), PAGE=str(page), ROWS=str(rows))
    # -i is essential: noninteractive zsh does not take the stopped-job path.
    command = '. "$PICKER"; selection=$(' + calls[profile] + '); rc=$?; printf "RESULT:%s:%s\\n" "$rc" "$selection"'
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    process = subprocess.Popen(['zsh', '-f', '-i', '-c', command], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        pidfile = tmp_path / 'pid'
        while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists(), 'picker did not start'
        picker_pid, picker_parent = map(int, pidfile.read_text().split())
        time.sleep(0.3)
        if stop_scope == 'group':
            os.killpg(process.pid, signal.SIGSTOP)
        else:
            os.kill(picker_pid, signal.SIGSTOP)
        # Let interactive zsh observe the stopped job before allowing completion.
        time.sleep(0.3)
        if stop_scope == 'group':
            os.killpg(process.pid, signal.SIGCONT)
        else:
            os.kill(picker_pid, signal.SIGCONT)
        time.sleep(1)
        owner = subprocess.check_output(['ps', '-p', str(picker_pid), '-o', 'ppid='], text=True)
        assert int(owner) == picker_parent, 'stopped picker was orphaned before it completed'
        (tmp_path / 'release').touch()
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail('picker command substitution remained stuck after its child resumed')
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds = (cpu_after.ru_utime + cpu_after.ru_stime
                       - cpu_before.ru_utime - cpu_before.ru_stime)
        assert cpu_seconds < 0.6, f'picker used {cpu_seconds:.2f}s CPU while waiting for release'
        assert process.returncode == 0, stderr
        if stop_scope == 'child':
            assert stdout == 'RESULT:0:chosen\n', stderr
        else:
            # Whole-session stop/resume exposes platform-dependent zsh job status:
            # macOS reports SIGCONT; Linux can report unknown status 255. Protect
            # bounded teardown here; stopping fzf alone must still preserve 0.
            statuses = (0, int(signal.SIGCONT), 128 + int(signal.SIGSTOP), 255)
            assert stdout in tuple(f'RESULT:{status}:chosen\n' for status in statuses), stderr
        assert not list(tmp_path.glob('t-picker.*'))
        assert not list(tmp_path.glob('t-paged-picker.*'))
    finally:
        # Only this test's new session is targeted, including a runaway subshell.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=3)


@pytest.mark.skipif(not shutil.which('zsh'), reason='requires zsh')
@pytest.mark.parametrize('status,expected', [(0, 0), (1, 130), (7, 7), (130, 130)])
def test_picker_worker_preserves_status_and_parent_exit_hooks(tmp_path, status, expected):
    stub = tmp_path / 'fzf'
    stub.write_text(f'#!/bin/sh\nprintf "selected\\n"\nexit {status}\n')
    stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.pop('T_HOME', None)
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path), TMPDIR=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / '.config'), XDG_DATA_HOME=str(tmp_path / '.local/share'),
               XDG_STATE_HOME=str(tmp_path / '.local/state'), XDG_CACHE_HOME=str(tmp_path / '.cache'),
               PATH=f'{tmp_path}{os.pathsep}{env.get("PATH", "")}', PICKER=str(PICKER))
    result = subprocess.run(['zsh', '-f', '-i', '-c', '''
      . "$PICKER"
      autoload -Uz add-zsh-hook
      save_session() { print -u2 -- "SAVE:$ZSH_SUBSHELL"; }
      add-zsh-hook zshexit save_session
      TRAPEXIT() { print -u2 -- "EXIT:$ZSH_SUBSHELL"; }
      zshexit() { print -u2 -- "NAMED:$ZSH_SUBSHELL"; }
      selection=$(_t_fzf_single open Legend)
      rc=$?
      print -r -- "RESULT:$rc:$selection"
    '''], env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'RESULT:{expected}:selected\n'
    assert result.stderr.splitlines().count('SAVE:0') == 1, result.stderr
    assert result.stderr.splitlines().count('EXIT:0') == 1, result.stderr
    assert [line for line in result.stderr.splitlines() if line.startswith('NAMED:')] == ['NAMED:0']
    # The caller's own command substitution can run its TRAPEXIT at depth 1.
    # No profile worker may execute it or a zshexit hook at depth 2 or beyond.
    assert [line for line in result.stderr.splitlines() if line.startswith('SAVE:')] == ['SAVE:0']
    assert all(line in ('EXIT:0', 'EXIT:1')
               for line in result.stderr.splitlines() if line.startswith('EXIT:'))


@pytest.mark.skipif(not shutil.which('zsh'), reason='requires zsh')
@pytest.mark.parametrize('filters', ['kill', 'resume', ''])
def test_picker_worker_keeps_shell_local_inputs_and_parent_unchanged(tmp_path, filters):
    import json

    stubbin = tmp_path / 'stubbin'
    stubbin.mkdir()
    worker_home = tmp_path / 'worker home'
    (worker_home / 'libexec').mkdir(parents=True)
    (worker_home / 'libexec/t_picker_filters.py').write_text('''import os
from pathlib import Path
# The real helper is already covered separately; retain its input files here.
Path(os.environ['SOURCE_LOG']).write_text(Path(os.environ['T_PICKER_VIEWS'], 'source').read_text())
auth = Path(os.environ['T_PICKER_VIEWS'], 'auth-hosts')
if auth.exists():
    Path(os.environ['AUTH_LOG']).write_text(auth.read_text())
''')
    stub = stubbin / 'fzf'
    stub.write_text('''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
if '--help' in sys.argv:
    print('--id-nth')
    sys.exit()
views = Path(os.environ['T_PICKER_VIEWS'])
files = {p.name: p.read_text() for p in views.iterdir() if p.is_file()}
Path(os.environ['LOCAL_LOG']).write_text(json.dumps({'files': files, 'args': sys.argv[1:],
    'home': os.environ.get('T_HOME'), 'filters': os.environ.get('T_PICKER_SESSION_FILTERS')}))
print('chosen')
''')
    stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COV_CORE_', 'T_PICKER_')) and key not in ('T_HOME', 'NO_COLOR')}
    hostile = "LOCAL COLUMNS ' \"\n$(touch " + str(tmp_path / 'injected') + ") `false` \\ 界面"
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path), TMPDIR=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / '.config'), XDG_DATA_HOME=str(tmp_path / '.local/share'),
               XDG_STATE_HOME=str(tmp_path / '.local/state'), XDG_CACHE_HOME=str(tmp_path / '.cache'),
               HOSTILE=hostile, PATH=f'{stubbin}{os.pathsep}{env.get("PATH", "")}', PICKER=str(PICKER),
               WORKER_HOME=str(worker_home), FILTERS=filters, LOCAL_LOG=str(tmp_path / 'log'),
               SOURCE_LOG=str(tmp_path / 'source'), AUTH_LOG=str(tmp_path / 'auth'))
    result = subprocess.run(['zsh', '-f', '-i', '-c', '''
      . "$PICKER"
      setopt rcquotes
      options_before=$(setopt)
      T_HOME=$WORKER_HOME NO_COLOR=1
      T_PICKER_COLUMNS=$HOSTILE T_PICKER_SOURCE_ROWS=$HOSTILE
      T_PICKER_SESSION_FILTERS=$FILTERS T_PICKER_WITH_ATTACHED=attached
      T_PICKER_WITH_PENDING=pending T_PICKER_ALL_LABEL='Local all'
      T_PICKER_AUTH_HOSTS='local auth'
      COLUMNS=123 LINES=45
      before=$(typeset -p T_HOME NO_COLOR T_PICKER_COLUMNS T_PICKER_SOURCE_ROWS T_PICKER_SESSION_FILTERS T_PICKER_WITH_ATTACHED T_PICKER_WITH_PENDING T_PICKER_ALL_LABEL T_PICKER_AUTH_HOSTS COLUMNS LINES)
      selection=$(_t_fzf_filtered chosen 'chosen\nother' open Active Legend '')
      rc=$?
      after=$(typeset -p T_HOME NO_COLOR T_PICKER_COLUMNS T_PICKER_SOURCE_ROWS T_PICKER_SESSION_FILTERS T_PICKER_WITH_ATTACHED T_PICKER_WITH_PENDING T_PICKER_ALL_LABEL T_PICKER_AUTH_HOSTS COLUMNS LINES)
      [[ $before == $after && $options_before == $(setopt) ]] || print MUTATED
      print -r -- "RESULT:$rc:$selection"
    '''], env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'RESULT:0:chosen\n', result.stderr
    log = json.loads((tmp_path / 'log').read_text())
    assert log['home'] == str(worker_home)
    assert log['filters'] == filters
    assert '--color=bw' in log['args']
    assert hostile in log['files']['header-default']
    assert (tmp_path / 'source').read_text() == hostile + '\n'
    assert not (tmp_path / 'injected').exists()
    if filters == 'kill':
        assert log['files']['rows-1'] == 'attached\n'
        assert log['files']['rows-2'] == 'pending\n'
    elif filters == 'resume':
        assert (tmp_path / 'auth').read_text() == 'local auth\n'
    else:
        assert 'Local all' in log['files']['header-all']


@pytest.mark.skipif(not shutil.which('zsh'), reason='requires zsh')
@pytest.mark.parametrize('large_input', ['arguments', 'source', 'both', 'facets'])
def test_picker_worker_accepts_rows_larger_than_exec_limits(tmp_path, large_input):
    stub = tmp_path / 'fzf'
    stub.write_text('''#!/bin/sh
case "$1" in --help) printf '%s\n' --id-nth; exit;; esac
cat > "$ROWS_LOG"
printf 'chosen\n'
''')
    stub.chmod(0o755)
    rows = tmp_path / 'large rows'
    rows.write_text("row\t'\"\n$(touch " + str(tmp_path / 'injected') + ") `false` \\ 界面\n" + 'x' * 400_000 + '\n')
    (tmp_path / 'libexec').mkdir()
    (tmp_path / 'libexec/t_picker_filters.py').write_text('# No-op rendering helper for the large source input.\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COV_CORE_', 'T_PICKER_')) and key != 'T_HOME'}
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path), TMPDIR=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / '.config'), XDG_DATA_HOME=str(tmp_path / '.local/share'),
               XDG_STATE_HOME=str(tmp_path / '.local/state'), XDG_CACHE_HOME=str(tmp_path / '.cache'),
               PATH=f'{tmp_path}{os.pathsep}{env.get("PATH", "")}', PICKER=str(PICKER),
               ROWS=str(rows), ROWS_LOG=str(tmp_path / 'rows log'), LARGE_INPUT=large_input)
    result = subprocess.run(['zsh', '-f', '-i', '-c', '''
      . "$PICKER"
      large=$(< "$ROWS")
      rows=chosen
      [[ $LARGE_INPUT == source || $LARGE_INPUT == facets ]] || rows=$large
      source_rows=
      [[ $LARGE_INPUT == arguments || $LARGE_INPUT == facets ]] || source_rows=$large
      T_HOME=$HOME
      if [[ $LARGE_INPUT == facets ]]; then
        selection=$(T_PICKER_WITH_ATTACHED=$large T_PICKER_WITH_PENDING=$large T_PICKER_SESSION_FILTERS=kill _t_fzf_filtered "$rows" "$rows" open Active Legend '')
      else
        selection=$(T_PICKER_SOURCE_ROWS=$source_rows _t_fzf_filtered "$rows" "$rows" open Active Legend '')
      fi
      print -r -- "RESULT:$?:$selection"
    '''], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'RESULT:0:chosen\n', result.stderr
    expected = 'chosen\n' if large_input in ('source', 'facets') else rows.read_text()
    assert (tmp_path / 'rows log').read_text() == expected
    assert not (tmp_path / 'injected').exists()
